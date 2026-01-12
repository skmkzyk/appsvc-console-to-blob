import os
import re
import json
import gzip
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
def blob_name_with_offsets(
    now_utc: datetime, 
    partition_id: Optional[str], 
    start_offset: Optional[str],
    end_offset: Optional[str]
) -> str:
    """
    Generate a blob path with Hive-style partitioning and offset-based naming.
    Format: y=YYYY/m=MM/d=DD/h=HH/m=00/p=<partition>/part-o<startOffset>-o<endOffset>.ndjson.gz
    
    Args:
        now_utc: Current UTC datetime
        partition_id: Event Hub partition ID
        start_offset: Starting offset of the batch (for single events, same as end_offset)
        end_offset: Ending offset of the batch (for single events, same as start_offset)
    
    Returns:
        Blob path string
    
    Notes:
        - The minute directory is fixed at '00' to aggregate all events within the same hour
        - For single Event Hub events, start_offset and end_offset will be identical
        - Using 'unknown' for missing offsets will fall back to timestamp-based identification
    """
    # Hive-style partitioning
    year = now_utc.strftime("%Y")
    month = now_utc.strftime("%m")
    day = now_utc.strftime("%d")
    hour = now_utc.strftime("%H")
    
    # Fixed minute directory (aggregates all events within the hour)
    minute = "00"
    
    # Partition ID (use "unknown" if not available)
    partition = partition_id if partition_id else "unknown"
    
    # Offset-based filename with timestamp fallback for uniqueness
    if start_offset and end_offset:
        start = start_offset
        end = end_offset
    else:
        # Fallback: use timestamp for uniqueness when offset is unavailable
        timestamp_id = now_utc.strftime("%Y%m%d%H%M%S%f")
        start = timestamp_id
        end = timestamp_id
    
    filename = f"part-o{start}-o{end}.ndjson.gz"
    
    # Build the full path
    path = f"y={year}/m={month}/d={day}/h={hour}/m={minute}/p={partition}/{filename}"
    
    return path


def ensure_container(container_name: str):
    """Create a container if it does not exist."""
    cc = blob_service_client().get_container_client(container_name)
    try:
        cc.create_container()
    except ResourceExistsError:
        pass
    return cc


def upload_compressed_blob(container_name: str, blob_name: str, content: str) -> None:
    """
    Upload gzip-compressed content to a Block Blob.
    
    Args:
        container_name: Target container name
        blob_name: Target blob path
        content: Text content to compress and upload
    
    Notes:
        - Uses overwrite=True for idempotency in case of function retries
        - Each FQDN uses a separate container, so blob_name collisions only occur
          for the same FQDN with the same offset, which indicates a retry
    """
    # Compress the content
    compressed_data = gzip.compress(content.encode("utf-8"))
    
    # Upload as block blob (overwrite for idempotency)
    bc = blob_service_client().get_blob_client(container=container_name, blob=blob_name)
    bc.upload_blob(compressed_data, overwrite=True)


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


def get_offset_info(evt: func.EventHubEvent) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract offset and sequence number from EventHubEvent metadata.
    Returns a tuple of (offset, sequence_number).
    """
    try:
        md = evt.metadata  # type: ignore[attr-defined]
    except Exception:
        return None, None

    if not isinstance(md, dict):
        return None, None

    offset = None
    sequence = None

    # Extract offset
    for k in ("offset", "x-opt-offset", "Offset"):
        v = md.get(k)
        if v is not None:
            offset = str(v)
            break

    # Extract sequence number
    for k in ("sequence_number", "x-opt-sequence-number", "SequenceNumber"):
        v = md.get(k)
        if v is not None:
            sequence = str(v)
            break

    return offset, sequence


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
      - Upload as compressed NDJSON to Block Blob with Hive-style partitioning
    """
    raw = evt.get_body().decode("utf-8", errors="replace")
    records = extract_records(raw)

    now_utc = datetime.now(timezone.utc)
    partition_id = get_partition_id(evt)
    offset, sequence_number = get_offset_info(evt)
    
    # Use offset and sequence for file naming
    # For batch processing, use the first offset as start and last as end
    # In Event Hub triggers, each event typically has a single offset
    start_offset = offset
    end_offset = offset
    
    blob_name = blob_name_with_offsets(now_utc, partition_id, start_offset, end_offset)

    # Group records by FQDN for efficient batch uploads
    records_by_fqdn: Dict[str, List[Dict[str, Any]]] = {}
    
    for r in records:
        msg = pick_message(r)
        fqdn = extract_fqdn(r, msg)
        
        if fqdn not in records_by_fqdn:
            records_by_fqdn[fqdn] = []
        
        # Build line object
        line_obj = {
            "time_utc": now_utc.isoformat(),
            "fqdn": fqdn,
            "partition_id": partition_id,
            "offset": offset,
            "sequence_number": sequence_number,
            "message": msg,
            "record": r,  # Remove if you want smaller output
        }
        records_by_fqdn[fqdn].append(line_obj)

    # Upload one blob per FQDN
    for fqdn, fqdn_records in records_by_fqdn.items():
        container = to_container_name(fqdn)
        ensure_container(container)
        
        # Build NDJSON content
        lines = [json.dumps(line_obj, ensure_ascii=False) for line_obj in fqdn_records]
        content = "\n".join(lines) + "\n"
        
        # Upload compressed blob
        upload_compressed_blob(container, blob_name, content)

    logging.info(
        "Processed %d record(s) across %d FQDN(s). partition=%s offset=%s",
        len(records),
        len(records_by_fqdn),
        partition_id,
        offset,
    )
