# Project Overview

This project is an Azure Functions application that receives App Service console logs from Azure Event Hubs and stores them in Azure Blob Storage.

## Architecture

- **Trigger**: Azure Event Hubs (diagnostic log streaming source)
- **Processing**: Python Azure Functions (Event Hub Trigger)
- **Destination**: Azure Blob Storage (NDJSON format in Append Blob)

## Key Features

### 1. Log Retrieval and Normalization
- Normalize payloads received from Event Hub
- Support multiple formats:
  - `[{"records": [...]}]` format
  - `{"records": [...]}` format
  - Single record `{...}` format
  - Non-JSON text format

### 2. FQDN Extraction and Container Isolation
- Preferentially extract `X-Forwarded-Host` header from log messages
  - When App Service is deployed as a backend for Azure Front Door, header rewrite causes the original Host header to be overwritten
  - The `X-Forwarded-Host` header preserves the original hostname, so it is preferentially referenced
- Fallback: Match `Host:` pattern using regular expression
- Store in separate Blob containers per FQDN (e.g., `logs-example-com`)
- Container names comply with Azure naming conventions (lowercase, hyphens, 3-63 characters)

### 3. Daily Blob Append
- Date-based directory structure: `YYYY/MM/DD/console.ndjson`
- Efficiently append multiple events using Append Blob
- Optional: Sharding by partition ID available (for high-throughput environments)

### 4. Authentication Methods
Support two authentication methods:
- **Connection String** (for development/testing): `LOG_STORAGE_CONNECTION`
- **Managed Identity** (recommended for production): `LOG_STORAGE_ACCOUNT_NAME` + `DefaultAzureCredential`

## Environment Variables

### Event Hubs Related
- `EVENTHUB_NAME`: Event Hub name (optional if EntityPath is in connection string)
- `EVENTHUB_CONNECTION_SETTING`: App Settings key name (default: `EventHubConnection`)

App Settings example when using Managed Identity:
```
EventHubConnection__fullyQualifiedNamespace = <namespace>.servicebus.windows.net
EventHubConnection__credential = managedidentity
EventHubConnection__clientId = <optional-UAMI-clientId>
```

### Blob Storage Related
**Method A: Connection String (Simple)**
- `LOG_STORAGE_CONNECTION`: Storage account connection string

**Method B: Managed Identity (Recommended)**
- `LOG_STORAGE_ACCOUNT_NAME`: Storage account name
- `LOG_STORAGE_MANAGED_IDENTITY_CLIENT_ID`: (Optional) UAMI client ID

### Others
- `LOG_CONTAINER_PREFIX`: Container name prefix (default: `logs-`)
- `LOG_BLOB_BASENAME`: Blob file name (default: `console.ndjson`)
- `LOG_BLOB_SHARD_BY_PARTITION`: Enable partition sharding (`1`/`true`/`yes`)

## Coding Conventions

### Design Patterns
- **Singleton Pattern**: `BlobServiceClient` is cached in a global variable
- **Lazy Initialization**: Clients are created on first use
- **Caching**: Cache containers and blob clients within the same execution to reduce duplicate creation/existence checks

### Error Handling
- Container creation: Ignore `ResourceExistsError` (idempotency)
- FQDN extraction: Fallback to `"unknown"` on parse failure
- Partition ID retrieval: Defensive programming (try multiple key names)

### Data Format
- Output format: NDJSON (newline-delimited JSON)
- Encoding: UTF-8
- Append Blob limit: Split writes into 4MiB chunks

### Type Hints
- Use type hints for all functions
- Import `Any`, `Dict`, `List`, `Optional`, `Tuple` from `typing` module

### Logging
- Use `logging` module
- Output statistics on processing completion

## Implementation Notes

### Blob Container Naming
- Follow Azure's strict naming rules:
  - Lowercase only
  - Letters, numbers, hyphens
  - 3-63 characters
  - No consecutive hyphens or leading/trailing hyphens
- Normalize using `NON_ALLOWED` and `MULTI_HYPHEN` regular expressions

### Regular Expression Patterns
- `HOST_REGEX`: Case-insensitive match for `host:` or `host =`
- Remove port numbers (e.g., `example.com:443` → `example.com`)

### Append Blob
- Guard with `exists()` because `create_append_blob()` is not idempotent
- Split into chunks to not exceed 4MiB limit

### Performance Optimization
- Cache containers and blob clients within function execution
- Enable partition sharding in high-throughput environments

## Deployment

1. Deploy to Azure Functions
2. Enable Managed Identity
3. Grant access to Event Hubs and Blob Storage:
   - Event Hubs: `Azure Event Hubs Data Receiver`
   - Blob Storage: `Storage Blob Data Contributor`
4. Configure environment variables (see above)

## Testing and Debugging

### Local Testing
- Set environment variables in `local.settings.json`
- Test with connection string using `LOG_STORAGE_CONNECTION`

### Sample Payload
- Place sample payload in `event-hub-sample.json` (for reference)

## Future Extensibility

- Add custom message field extraction logic
- Record filtering functionality
- Routing to different destinations (Table Storage, Cosmos DB, etc.)
- Metrics and alert integration
