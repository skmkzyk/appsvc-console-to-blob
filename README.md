# App Service Console to Blob

Azure App Service のコンソールログを Azure Event Hubs 経由で受信し、Azure Blob Storage に保存する Azure Functions アプリケーションです。

## 概要

このプロジェクトは、Azure App Service の診断ログを自動的に収集し、FQDN（完全修飾ドメイン名）ごとに分離して Blob Storage に保存します。日付ベースのディレクトリ構造で整理され、NDJSON 形式で効率的に記録されます。

### 主な機能

- **Event Hubs トリガー**: App Service の診断ログストリーミングを自動受信
- **FQDN 抽出と分離**: ログメッセージから FQDN を抽出し、コンテナごとに分離保存
- **日次 Append Blob**: `YYYY/MM/DD/console.ndjson` の形式で日付ごとにログを追記
- **複数認証方式**: 接続文字列またはマネージド ID に対応
- **高スループット対応**: パーティション ID によるシャーディングをサポート

## アーキテクチャ

```
Azure App Service (診断ログ)
    ↓
Azure Event Hubs
    ↓
Azure Functions (Event Hub Trigger)
    ↓
Azure Blob Storage (NDJSON)
```

## 必要な環境

- Python 3.9 以上
- Azure Functions Core Tools v4
- Azure サブスクリプション
- uv (Python パッケージマネージャー) または pip

## 開発環境のセットアップ

### 1. リポジトリのクローンと環境構築

```bash
cd appsvc-console-to-blob
uv venv
source .venv/bin/activate
```

### 2. 依存パッケージのインストール

```bash
uv pip install -r requirements.txt
```

### 3. ローカル設定ファイルの作成

`local.settings.json` を作成（このファイルは `.gitignore` に含まれています）:

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "EventHubConnection": "<Event Hub 接続文字列>",
    "EVENTHUB_NAME": "<Event Hub 名>",
    "LOG_STORAGE_CONNECTION": "<Blob Storage 接続文字列>"
  }
}
```

### 4. ローカルでの実行

```bash
func start
```

## プログラムの変更手順

### コードの編集

主要なロジックは `function_app.py` に実装されています：

1. **ログの正規化処理を変更**: `normalize_payload()` 関数を編集
2. **FQDN 抽出ロジックを変更**: `extract_fqdn()` 関数を編集
3. **Blob 保存形式を変更**: `write_to_blob()` 関数を編集

### テスト

サンプルペイロードを使用してテスト:

```bash
# event-hub-sample.json にサンプルデータを配置
func start
```

### コードフォーマット

```bash
# black を使用する場合
black function_app.py

# ruff を使用する場合
ruff format function_app.py
```

## デプロイ手順

### 前提条件

- Azure Functions リソースが作成済み
- Azure CLI がインストール済み
- Azure にログイン済み (`az login`)

### 1. Function App へのデプロイ

```bash
func azure functionapp publish "<YourFunctionAppName>" --python
```

例:
```bash
func azure functionapp publish "my-log-processor-func" --python
```

### 2. マネージド ID の有効化

```bash
az functionapp identity assign \
  --name "<YourFunctionAppName>" \
  --resource-group "<YourResourceGroup>"
```

### 3. Event Hubs へのアクセス権付与

```bash
az role assignment create \
  --assignee "<マネージド ID のプリンシパル ID>" \
  --role "Azure Event Hubs Data Receiver" \
  --scope "/subscriptions/<サブスクリプション ID>/resourceGroups/<リソースグループ>/providers/Microsoft.EventHub/namespaces/<名前空間>/eventhubs/<Event Hub 名>"
```

### 4. Blob Storage へのアクセス権付与

```bash
az role assignment create \
  --assignee "<マネージド ID のプリンシパル ID>" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/<サブスクリプション ID>/resourceGroups/<リソースグループ>/providers/Microsoft.Storage/storageAccounts/<ストレージアカウント名>"
```

### 5. 環境変数の設定

Azure Portal または Azure CLI で環境変数を設定:

```bash
az functionapp config appsettings set \
  --name "<YourFunctionAppName>" \
  --resource-group "<YourResourceGroup>" \
  --settings \
    "EventHubConnection__fullyQualifiedNamespace=<namespace>.servicebus.windows.net" \
    "EventHubConnection__credential=managedidentity" \
    "EVENTHUB_NAME=<Event Hub 名>" \
    "LOG_STORAGE_ACCOUNT_NAME=<ストレージアカウント名>"
```

### 6. デプロイの確認

```bash
# ログストリームを確認
func azure functionapp logstream "<YourFunctionAppName>"
```

または Azure Portal の「ログストリーム」から確認できます。

## 環境変数

### Azure App Settings（運用時に現在使っているもの）

- `APPLICATIONINSIGHTS_CONNECTION_STRING`: App Insights 用（監視が不要なら削除可）
- `AzureWebJobsStorage__accountName`: Functions システム用ストレージアカウント名
- `AzureWebJobsStorage__credential`: `managedidentity` を指定
- `DEPLOYMENT_STORAGE_CONNECTION_STRING`: デプロイ時に使用する場合のみ（不要なら削除可）
- `EVENTHUB_NAME`: Event Hub 名（接続文字列に EntityPath が無い場合に必須）
- `EventHubConnection__fullyQualifiedNamespace`: `<namespace>.servicebus.windows.net`
- `EventHubConnection__credential`: `managedidentity`
- `LOG_STORAGE_ACCOUNT_NAME`: ログ書き込み先のストレージアカウント名

### ローカル実行時のみ（例）

`local.settings.json` にローカル用の接続文字列を置く場合:

```
EventHubConnection = <Event Hub 接続文字列>
LOG_STORAGE_CONNECTION = <Blob Storage 接続文字列>
AzureWebJobsStorage = UseDevelopmentStorage=true
```

※ 本番では上記接続文字列は不要で、マネージド ID を使う構成に寄せています。

## 出力形式

### Blob Storage 構造

```
コンテナ: logs-example-com
  └── 2026/
      └── 01/
          └── 10/
              └── console.ndjson
```

### NDJSON 形式

各行が1つの JSON オブジェクト:

```json
{"time": "2026-01-10T12:34:56Z", "level": "INFO", "message": "Request received", "host": "example.com"}
{"time": "2026-01-10T12:34:57Z", "level": "ERROR", "message": "Connection timeout", "host": "example.com"}
```

## トラブルシューティング

### ログが保存されない

1. Function App のログを確認: `func azure functionapp logstream "<YourFunctionAppName>"`
2. マネージド ID の権限を確認
3. Event Hub への接続を確認

### FQDN が "unknown" になる

- ログメッセージに `Host:` または `X-Forwarded-Host:` ヘッダーが含まれているか確認
- `extract_fqdn()` 関数のパターンマッチングを調整

### Blob 書き込みエラー

- Storage Account の権限（Storage Blob Data Contributor）を確認
- 環境変数 `LOG_STORAGE_ACCOUNT_NAME` または `LOG_STORAGE_CONNECTION` が正しく設定されているか確認

## ライセンス

MIT License

## 参考資料

- [Azure Functions Python Developer Guide](https://learn.microsoft.com/azure/azure-functions/functions-reference-python)
- [Azure Event Hubs bindings for Azure Functions](https://learn.microsoft.com/azure/azure-functions/functions-bindings-event-hubs)
- [Azure Blob Storage bindings for Azure Functions](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-blob)
