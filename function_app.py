import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import azure.functions as func
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential

app = func.FunctionApp()

# =============================================================================
# Configuration (Function App settings / environment variables)
# =============================================================================
#
# Event Hubs trigger:
# - The decorator's "connection" value is the *setting name* (not a connection string).
# - For Managed Identity, configure identity-based connection settings in App Settings:
#     EventHubConnection__fullyQualifiedNamespace = "<ns>.servicebus.windows.net"
#     EventHubConnection__credential = "managedidentity"
#     (optional) EventHubConnection__clientId = "<UAMI clientId>"
#
EVENTHUB_NAME = os.getenv("EVENTHUB_NAME", "")  # Optional if EntityPath is in connection string
EVENTHUB_CONNECTION_SETTING = os.getenv("EVENTHUB_CONNECTION_SETTING", "EventHubConnection")

# Blob destination:
# Choose ONE of the following approaches:
#
# A) Connection string (simple / local-friendly):
#   LOG_STORAGE_CONNECTION = "<storage connection string>"
#
# B) Managed Identity (recommended for production):
#   LOG_STORAGE_ACCOUNT_NAME = "<storage account name>"
#   (optional) LOG_STORAGE_MANAGED_IDENTITY_CLIENT_ID = "<UAMI clientId>"
#
LOG_STORAGE_CONNECTION = os.getenv("LOG_STORAGE_CONNECTION")
LOG_STORAGE_ACCOUNT_NAME = os.getenv("LOG_STORAGE_ACCOUNT_NAME")
LOG_STORAGE_MI_CLIENT_ID = os.getenv("LOG_STORAGE_MANAGED_IDENTITY_CLIENT_ID")

# Container name prefix (e.g., logs-)
CONTAINER_PREFIX = os.getenv("LOG_CONTAINER_PREFIX", "logs-")

# Append into a daily blob path
BLOB_BASENAME = os.getenv("LOG_BLOB_BASENAME", "console.ndjson")

# Option: shard blobs by Event Hub partition ID to reduce contention (recommended at higher throughput)
# Set to "1" or "true" to enable.
SHARD_BY_PARTITION = os.getenv("LOG_BLOB_SHARD_BY_PARTITION", "0").lower() in ("1", "true", "yes")

# Host extraction pattern:
# Assumes your App Service writes a dedicated line such as:
#   Host: example.com
# or
#   host = example.com:443
HOST_REGEX = re.compile(r"(?i)\bhost\b\s*[:=]\s*\"?([^\"\s,;]+)")

# Container naming constraints (Azure Blob container rules are strict):
# - lower case only
# - letters, numbers, hyphen
# - 3-63 chars
NON_ALLOWED = re.compile(r"[^a-z0-9-]+")
MULTI_HYPHEN = re.compile(r"-{2,}")

# AppendBlock size limit: keep chunks within ~4MiB
APPEND_CHUNK_BYTES = 4 * 1024 * 1024

# Cached clients
_blob_svc: Optional[BlobServiceClient] = None


# =============================================================================
# Client factory
# =============================================================================
def blob_service_client() -> BlobServiceClient:
    """
    Create (and cache) BlobServiceClient.

    If LOG_STORAGE_CONNECTION is set:
      - Use it directly (best for local testing)

    Else if LOG_STORAGE_ACCOUNT_NAME is set:
      - Use Managed Identity via DefaultAzureCredential
    """
    global _blob_svc
    if _blob_svc is not None:
        return _blob_svc

    if LOG_STORAGE_CONNECTION:
        _blob_svc = BlobServiceClient.from_connection_string(LOG_STORAGE_CONNECTION)
        return _blob_svc

    if not LOG_STORAGE_ACCOUNT_NAME:
        raise RuntimeError(
            "Blob destination is not configured. Set LOG_STORAGE_CONNECTION (connection string) "
            "or LOG_STORAGE_ACCOUNT_NAME (Managed Identity)."
        )

    # Managed Identity / workload identity (DefaultAzureCredential supports multiple auth flows)
    if LOG_STORAGE_MI_CLIENT_ID:
        credential = DefaultAzureCredential(managed_identity_client_id=LOG_STORAGE_MI_CLIENT_ID)
    else:
        credential = DefaultAzureCredential()

    account_url = f"https://{LOG_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
    _blob_svc = BlobServiceClient(account_url=account_url, credential=credential)
    return _blob_svc


# =============================================================================
# Payload normalization helpers
# =============================================================================
def extract_records(payload_text: str) -> List[Dict[str, Any]]:
    """
    Normalize incoming payloads to a list of record dicts.

    Diagnostic logs sent to Event Hubs may arrive as:
      1) [ { "records": [ ... ] }, ... ]
      2) { "records": [ ... ] }
      3) { ... } (single record)
      4) non-JSON text (treated as one record with "_raw")
    """
    try:
        obj = json.loads(payload_text)
    except json.JSONDecodeError:
        return [{"_raw": payload_text}]

    # Case 1: list
    if isinstance(obj, list):
        out: List[Dict[str, Any]] = []
        for item in obj:
            if isinstance(item, dict) and isinstance(item.get("records"), list):
                out.extend([r for r in item["records"] if isinstance(r, dict)])
            elif isinstance(item, dict):
                out.append(item)
        return out

    # Case 2: dict with records
    if isinstance(obj, dict) and isinstance(obj.get("records"), list):
        return [r for r in obj["records"] if isinstance(r, dict)]

    # Case 3: dict single record
    if isinstance(obj, dict):
        return [obj]

    # Fallback
    return [{"_raw": payload_text}]


def pick_message(record: Dict[str, Any]) -> str:
    """
    Extract a human-friendly message field if available.
    Adjust keys based on what you actually observe in your Event Hub payload.
    """
    for k in ("resultDescription", "ResultDescription", "message", "Message", "_raw"):
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


# =============================================================================
# FQDN -> container naming
# =============================================================================
def extract_fqdn(record: Dict[str, Any], msg: str) -> str:
    """
    Prefer X-Forwarded-Host header if present inside the payload.
    Fallback to regex-based extraction from the message text.
    Return "unknown" when nothing is found.
    """

    # Attempt to parse trailing JSON embedded in the message
    json_start = msg.find("{")
    if json_start != -1:
        try:
            parsed = json.loads(msg[json_start:])
            headers = parsed.get("headers") if isinstance(parsed, dict) else None
            if isinstance(headers, dict):
                for k in ("X-Forwarded-Host", "x-forwarded-host", "X-FORWARDED-HOST"):
                    v = headers.get(k)
                    if isinstance(v, str) and v.strip():
                        host = v.strip().lower()
                        if ":" in host:
                            host = host.split(":", 1)[0]
                        return host or "unknown"
        except Exception:
            pass  # Best-effort; ignore parsing errors

    # Fallback: previous HOST_REGEX approach
    m = HOST_REGEX.search(msg)
    if not m:
        return "unknown"

    host = m.group(1).strip().lower()

    # Drop port part if present (example.com:443)
    if ":" in host:
        host = host.split(":", 1)[0]

    return host or "unknown"


def to_container_name(fqdn: str) -> str:
    """
    Convert FQDN into a valid container name.
    Example: example.com -> logs-example-com
    """
    base = fqdn.lower().replace(".", "-")
    base = NON_ALLOWED.sub("-", base).strip("-")
    base = MULTI_HYPHEN.sub("-", base)

    if len(base) < 3:
        base = f"unk-{base}" if base else "unknown"

    name = (CONTAINER_PREFIX + base).lower()
    name = NON_ALLOWED.sub("-", name).strip("-")
    name = MULTI_HYPHEN.sub("-", name)

    # Enforce 3-63
    if len(name) > 63:
        name = name[:63].rstrip("-")
    name = name.strip("-")
    if len(name) < 3:
        name = "logs-unknown"

    return name


# =============================================================================
# Blob naming and append helpers
# =============================================================================
def daily_blob_name(now_utc: datetime, partition_id: Optional[str]) -> str:
    """
    Generate a daily blob path.
    - Default:  YYYY/MM/DD/console.ndjson
    - Sharded:  YYYY/MM/DD/p<partition>/console.ndjson
    """
    if SHARD_BY_PARTITION and partition_id:
        return f"{now_utc:%Y/%m/%d}/p{partition_id}/{BLOB_BASENAME}"
    return f"{now_utc:%Y/%m/%d}/{BLOB_BASENAME}"


def ensure_container(container_name: str):
    """Create a container if it does not exist."""
    cc = blob_service_client().get_container_client(container_name)
    try:
        cc.create_container()
    except ResourceExistsError:
        pass
    return cc


def ensure_append_blob(container_name: str, blob_name: str):
    """
    Ensure the target blob exists as an Append Blob.
    Guard with exists() because create_append_blob is not idempotent.
    """
    bc = blob_service_client().get_blob_client(container=container_name, blob=blob_name)
    if not bc.exists():
        bc.create_append_blob()
    return bc


def append_text(append_blob_client, text: str) -> None:
    """
    Append UTF-8 text to an Append Blob.
    Split into <= ~4MiB chunks to stay within append block limits.
    """
    data = text.encode("utf-8")
    for i in range(0, len(data), APPEND_CHUNK_BYTES):
        append_blob_client.append_block(data[i : i + APPEND_CHUNK_BYTES])


def get_partition_id(evt: func.EventHubEvent) -> Optional[str]:
    """
    Best-effort extraction of partition ID from EventHubEvent metadata.
    Exact keys can vary by runtime; handle defensively.
    """
    try:
        md = evt.metadata  # type: ignore[attr-defined]
    except Exception:
        return None

    if not isinstance(md, dict):
        return None

    # Common candidates (defensive)
    for k in ("PartitionId", "partitionId", "x-opt-partition-id", "partition_id"):
        v = md.get(k)
        if v is not None:
            return str(v)
    return None


# =============================================================================
# Function entrypoint
# =============================================================================
@app.function_name(name="AppServiceConsoleToBlob")
@app.event_hub_message_trigger(
    arg_name="evt",
    event_hub_name=EVENTHUB_NAME if EVENTHUB_NAME else None,
    connection=EVENTHUB_CONNECTION_SETTING,
)
def main(evt: func.EventHubEvent):
    """
    Event Hub Trigger:
      - Parse incoming payload to records
      - Extract FQDN from message ("Host: ...")
      - Append as NDJSON lines into an Append Blob per day and per FQDN container
    """
    raw = evt.get_body().decode("utf-8", errors="replace")
    records = extract_records(raw)

    now_utc = datetime.now(timezone.utc)
    partition_id = get_partition_id(evt)
    blob_name = daily_blob_name(now_utc, partition_id)

    # Cache within one invocation to reduce repeated exists()/create calls
    container_ready: Dict[str, bool] = {}
    blob_client_cache: Dict[Tuple[str, str], Any] = {}

    for r in records:
        msg = pick_message(r)
        fqdn = extract_fqdn(r, msg)
        container = to_container_name(fqdn)

        # One NDJSON line per record
        line_obj = {
            "time_utc": now_utc.isoformat(),
            "fqdn": fqdn,
            "partition_id": partition_id,
            "message": msg,
            "record": r,  # Remove if you want smaller output
        }
        line = json.dumps(line_obj, ensure_ascii=False) + "\n"

        if container not in container_ready:
            ensure_container(container)
            container_ready[container] = True

        key = (container, blob_name)
        if key not in blob_client_cache:
            blob_client_cache[key] = ensure_append_blob(container, blob_name)

        append_text(blob_client_cache[key], line)

    logging.info(
        "Processed %d record(s). container_count=%d shard_by_partition=%s",
        len(records),
        len(container_ready),
        SHARD_BY_PARTITION,
    )
