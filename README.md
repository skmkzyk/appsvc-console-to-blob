# App Service Console to Blob

An Azure Functions application that receives console logs from Azure App Service via Azure Event Hubs and stores them in Azure Blob Storage.

## Overview

This project automatically collects diagnostic logs from Azure App Service, separates them by FQDN (Fully Qualified Domain Name), and stores them in Blob Storage. Logs are organized with a date-based directory structure and efficiently recorded in NDJSON format.

### Key Features

- **Event Hubs Trigger**: Automatically receives diagnostic log streaming from App Service
- **FQDN Extraction and Isolation**: Extracts FQDN from log messages and stores them separately per container
- **Hive-style Partitioning**: Organizes logs hourly in the format `y=YYYY/m=MM/d=DD/h=HH/m=00/p=<partition>`
- **gzip Compression**: Automatic compression to reduce storage costs
- **Offset Tracking**: Tracks Event Hub offset information in file names
- **Multiple Authentication Methods**: Supports connection strings or managed identity

## Architecture

```
Azure App Service (Diagnostic Logs)
    ↓
Azure Event Hubs
    ↓
Azure Functions (Event Hub Trigger)
    ↓
Azure Blob Storage (NDJSON)
```

### Why Not Use Blob Output Bindings?

This function uses the Azure Blob Storage SDK directly instead of blob output bindings for the following reasons:

1. **Dynamic Container Selection**: The target container name is determined at runtime based on the FQDN extracted from each log message. Output bindings require the container name to be known at function definition time.

2. **Complex Blob Path Generation**: Uses Hive-style partitioning with dynamic paths like `y=2026/m=01/d=10/h=06/m=00/p=0/part-o1234567890.ndjson.gz`. Output bindings support limited path templating and cannot generate this structure.

3. **Compression**: Applies gzip compression to reduce storage costs. Output bindings don't support compression, so the SDK is needed to compress data before upload.

4. **Batching and Grouping**: Groups records by FQDN and hour before writing. A single function invocation may write to multiple containers and multiple blobs, which output bindings don't support well.

5. **Advanced Features**: Uses Block Blobs with overwrite for idempotency. Output bindings have limited control over blob type and upload behavior.

## Requirements

- Python 3.9 or higher
- Azure Functions Core Tools v4
- Azure subscription
- uv (Python package manager) or pip

## Development Environment Setup

### 1. Clone Repository and Setup Environment

```bash
cd appsvc-console-to-blob
uv venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
uv pip install -r requirements.txt
```

### 3. Create Local Settings File

Create `local.settings.json` (this file is included in `.gitignore`):

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "EventHubConnection": "<Event Hub connection string>",
    "EVENTHUB_NAME": "<Event Hub name>",
    "LOG_STORAGE_CONNECTION": "<Blob Storage connection string>"
  }
}
```

### 4. Run Locally

```bash
func start
```

## Modifying the Program

### Editing Code

The main logic is implemented in `function_app.py`:

1. **Change log normalization processing**: Edit the `extract_records()` function
2. **Change FQDN extraction logic**: Edit the `extract_fqdn()` function
3. **Change Blob path generation**: Edit the `blob_name_with_offsets()` function
4. **Change Blob upload processing**: Edit the `upload_compressed_blob()` function

### Testing

Test using sample payloads:

```bash
# Place sample data in event-hub-sample.json
func start
```

### Code Formatting

```bash
# Using black
black function_app.py

# Using ruff
ruff format function_app.py
```

## Creating Azure Functions Resource

Before deploying the function code, you need to create an Azure Functions resource. This section describes how to create a Flex Consumption SKU function app.

### Prerequisites

- Azure CLI installed
- Logged in to Azure (`az login`)
- Resource group already created
- Storage account for hosting already created

### Create Function App (Flex Consumption SKU)

```bash
az functionapp create \
  --resource-group "$RG" \
  --name "$FUNCAPP" \
  --storage-account "$HOSTSA" \
  --flexconsumption-location "$LOC" \
  --runtime python \
  --runtime-version 3.11
```

Replace the variables with your values:
- `$RG`: Your resource group name (e.g., `my-resource-group`)
- `$FUNCAPP`: Your function app name (e.g., `my-log-processor-func`)
- `$HOSTSA`: Your storage account name for hosting (e.g., `myhostingstorageacct`)
- `$LOC`: Your Azure region (e.g., `eastus`, `westus2`, `japaneast`)

Example:
```bash
az functionapp create \
  --resource-group "my-resource-group" \
  --name "my-log-processor-func" \
  --storage-account "myhostingstorageacct" \
  --flexconsumption-location "japaneast" \
  --runtime python \
  --runtime-version 3.11
```

## Deployment Steps

### Prerequisites

- Azure Functions resource created (see "Creating Azure Functions Resource" section above)
- Azure CLI installed
- Logged in to Azure (`az login`)

**Note**: The following commands use shell variables. Set them according to your environment:
- `$RG`: Resource group name
- `$FUNCAPP`: Function app name
- `$HOSTSA`: Storage account name for hosting the Function App
- `$LOC`: Azure region (e.g., `eastus`, `westus2`, `japaneast`)
- `$PRINCIPAL_ID`: Managed identity principal ID (obtained from step 2)
- `$SUBSCRIPTION_ID`: Your Azure subscription ID
- `$EVENTHUB_NAMESPACE`: Event Hub namespace name
- `$EVENTHUB_NAME`: Event Hub name
- `$LOG_STORAGE_ACCOUNT`: Storage account name for log storage

### 1. Deploy to Function App

```bash
func azure functionapp publish "$FUNCAPP" --python
```

Example:
```bash
func azure functionapp publish "my-log-processor-func" --python
```

### 2. Enable Managed Identity

```bash
az functionapp identity assign \
  --name "$FUNCAPP" \
  --resource-group "$RG"
```

### 3. Grant Access to Event Hubs

```bash
az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role "Azure Event Hubs Data Receiver" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.EventHub/namespaces/$EVENTHUB_NAMESPACE/eventhubs/$EVENTHUB_NAME"
```

### 4. Grant Access to Blob Storage

```bash
az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$LOG_STORAGE_ACCOUNT"
```

### 5. Configure Environment Variables

Set environment variables using Azure Portal or Azure CLI:

```bash
az functionapp config appsettings set \
  --name "$FUNCAPP" \
  --resource-group "$RG" \
  --settings \
    "EventHubConnection__fullyQualifiedNamespace=${EVENTHUB_NAMESPACE}.servicebus.windows.net" \
    "EventHubConnection__credential=managedidentity" \
    "EVENTHUB_NAME=$EVENTHUB_NAME" \
    "LOG_STORAGE_ACCOUNT_NAME=$LOG_STORAGE_ACCOUNT"
```

### 6. Verify Deployment

```bash
# Check log stream
func azure functionapp logstream "$FUNCAPP"
```

Or check from the "Log stream" in Azure Portal.

## Environment Variables

### Azure App Settings (Currently Used in Production)

- `APPLICATIONINSIGHTS_CONNECTION_STRING`: For App Insights (can be removed if monitoring is not needed)
- `AzureWebJobsStorage__accountName`: Storage account name for Functions system
- `AzureWebJobsStorage__credential`: Specify `managedidentity`
- `DEPLOYMENT_STORAGE_CONNECTION_STRING`: Only if used during deployment (can be removed if not needed)
- `EVENTHUB_NAME`: Event Hub name (required if EntityPath is not in connection string)
- `EventHubConnection__fullyQualifiedNamespace`: `<namespace>.servicebus.windows.net`
- `EventHubConnection__credential`: `managedidentity`
- `LOG_STORAGE_ACCOUNT_NAME`: Storage account name for log writing destination

### Local Execution Only (Example)

When placing local connection strings in `local.settings.json`:

```
EventHubConnection = <Event Hub connection string>
LOG_STORAGE_CONNECTION = <Blob Storage connection string>
AzureWebJobsStorage = UseDevelopmentStorage=true
```

※ In production, the above connection strings are not needed; we use a managed identity configuration.

## Output Format

### Blob Storage Structure

New format using Hive-style partitioning and gzip compression:

```
Container: logs-example-com
  └── y=2026/
      └── m=01/
          └── d=10/
              └── h=06/
                  └── m=00/
                      └── p=0/
                          └── part-o1234567890-o1234567900.ndjson.gz
```

Path format: `y=<YYYY>/m=<MM>/d=<DD>/h=<HH>/m=00/p=<partition_id>/part-o<startOffset>-o<endOffset>.ndjson.gz`

- `y=YYYY`: Year
- `m=MM`: Month
- `d=DD`: Day
- `h=HH`: Hour (UTC)
- `m=00`: Minute (fixed value)
- `p=<partition_id>`: Event Hub partition ID
- `part-o<startOffset>-o<endOffset>.ndjson.gz`: Compressed file with offset range

### NDJSON Format

Each line is a single JSON object (gzip compressed):

```json
{"time_utc": "2026-01-10T12:34:56.123456+00:00", "fqdn": "example.com", "partition_id": "0", "offset": "1234567890", "sequence_number": "12345", "message": "Request received", "record": {...}}
{"time_utc": "2026-01-10T12:34:57.234567+00:00", "fqdn": "example.com", "partition_id": "0", "offset": "1234567891", "sequence_number": "12346", "message": "Connection timeout", "record": {...}}
```

## Troubleshooting

### Logs Are Not Being Saved

1. Check Function App logs: `func azure functionapp logstream "$FUNCAPP"`
2. Verify managed identity permissions
3. Verify Event Hub connection

### FQDN Becomes "unknown"

- Check if log messages contain `Host:` or `X-Forwarded-Host:` headers
- Adjust pattern matching in the `extract_fqdn()` function

### Blob Write Errors

- Verify Storage Account permissions (Storage Blob Data Contributor)
- Verify that `LOG_STORAGE_ACCOUNT_NAME` or `LOG_STORAGE_CONNECTION` environment variables are correctly set

## License

MIT License

## References

- [Azure Functions Python Developer Guide](https://learn.microsoft.com/azure/azure-functions/functions-reference-python)
- [Azure Event Hubs bindings for Azure Functions](https://learn.microsoft.com/azure/azure-functions/functions-bindings-event-hubs)
- [Azure Blob Storage bindings for Azure Functions](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-blob)
