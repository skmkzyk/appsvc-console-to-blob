# App Service Console to Blob

Azure App Service のコンソールログを Azure Event Hubs 経由で受信し、Azure Blob Storage に保存する Azure Functions アプリケーションです。

## 概要

このプロジェクトは、Azure App Service の診断ログを自動的に収集し、FQDN（完全修飾ドメイン名）ごとに分離して Blob Storage に保存します。日付ベースのディレクトリ構造で整理され、NDJSON 形式で効率的に記録されます。

### 主な機能

- **Event Hubs トリガー**: App Service の診断ログストリーミングを自動受信
- **FQDN 抽出と分離**: ログメッセージから FQDN を抽出し、コンテナごとに分離保存
- **Hive スタイルパーティショニング**: `y=YYYY/m=MM/d=DD/h=HH/m=00/p=<partition>` の形式で時間ごとにログを整理
- **gzip 圧縮**: ストレージコストを削減するための自動圧縮
- **オフセットトラッキング**: Event Hub のオフセット情報をファイル名に含めて追跡
- **複数認証方式**: 接続文字列またはマネージド ID に対応

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

### Blob 出力バインディングを使わない理由

この関数では、Blob 出力バインディングではなく Azure Blob Storage SDK を直接使用しています。その理由は以下の通りです：

1. **動的なコンテナ選択**: ターゲットコンテナ名は、各ログメッセージから抽出された FQDN に基づいて実行時に決定されます。出力バインディングでは、コンテナ名を関数定義時に指定する必要があります。

2. **複雑な Blob パス生成**: `y=2026/m=01/d=10/h=06/m=00/p=0/part-o1234567890.ndjson.gz` のような Hive スタイルのパーティショニングを使用した動的なパスを生成します。出力バインディングは限定的なパステンプレートのみをサポートしており、この構造を生成できません。

3. **圧縮**: ストレージコストを削減するために gzip 圧縮を適用します。出力バインディングは圧縮をサポートしていないため、アップロード前にデータを圧縮するには SDK が必要です。

4. **バッチ処理とグループ化**: FQDN と時間ごとにレコードをグループ化してから書き込みます。1回の関数呼び出しで複数のコンテナと複数の Blob に書き込む可能性があり、出力バインディングではこれを適切にサポートできません。

5. **高度な機能**: 冪等性のために上書き機能を持つブロック Blob を使用します。出力バインディングでは、Blob タイプやアップロード動作に対する制御が制限されています。

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

1. **ログの正規化処理を変更**: `extract_records()` 関数を編集
2. **FQDN 抽出ロジックを変更**: `extract_fqdn()` 関数を編集
3. **Blob パス生成を変更**: `blob_name_with_offsets()` 関数を編集
4. **Blob アップロード処理を変更**: `upload_compressed_blob()` 関数を編集

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

## Azure Functions リソースの作成

関数コードをデプロイする前に、Azure Functions リソースを作成する必要があります。このセクションでは、Flex Consumption SKU の関数アプリを作成する方法を説明します。

### 前提条件

- Azure CLI がインストール済み
- Azure にログイン済み (`az login`)
- リソースグループが作成済み
- ホスティング用のストレージアカウントが作成済み

### Function App の作成（Flex Consumption SKU）

```bash
az functionapp create \
  --resource-group "$RG" \
  --name "$FUNCAPP" \
  --storage-account "$HOSTSA" \
  --flexconsumption-location "$LOC" \
  --runtime python \
  --runtime-version 3.11
```

変数を実際の値に置き換えてください：
- `$RG`: リソースグループ名（例: `my-resource-group`）
- `$FUNCAPP`: 関数アプリ名（例: `my-log-processor-func`）
- `$HOSTSA`: ホスティング用ストレージアカウント名（例: `myhostingstorageacct`）
- `$LOC`: Azure リージョン（例: `eastus`、`westus2`、`japaneast`）

例:
```bash
az functionapp create \
  --resource-group "my-resource-group" \
  --name "my-log-processor-func" \
  --storage-account "myhostingstorageacct" \
  --flexconsumption-location "japaneast" \
  --runtime python \
  --runtime-version 3.11
```

## デプロイ手順

### 前提条件

- Azure Functions リソースが作成済み（上記の「Azure Functions リソースの作成」セクションを参照）
- Azure CLI がインストール済み
- Azure にログイン済み (`az login`)

**注意**: 以下のコマンドではシェル変数を使用します。環境に応じて設定してください：
- `$RG`: リソースグループ名
- `$FUNCAPP`: 関数アプリ名
- `$HOSTSA`: Function App のホスティング用ストレージアカウント名
- `$LOC`: Azure リージョン（例: `eastus`、`westus2`、`japaneast`）
- `$PRINCIPAL_ID`: マネージド ID のプリンシパル ID（手順 2 で取得）
- `$SUBSCRIPTION_ID`: Azure サブスクリプション ID
- `$EVENTHUB_NAMESPACE`: Event Hub 名前空間名
- `$EVENTHUB_NAME`: Event Hub 名
- `$LOG_STORAGE_ACCOUNT`: ログ保存用ストレージアカウント名

### 1. Function App へのデプロイ

```bash
func azure functionapp publish "$FUNCAPP" --python
```

例:
```bash
func azure functionapp publish "my-log-processor-func" --python
```

### 2. マネージド ID の有効化

```bash
az functionapp identity assign \
  --name "$FUNCAPP" \
  --resource-group "$RG"
```

### 3. Event Hubs へのアクセス権付与

```bash
az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role "Azure Event Hubs Data Receiver" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.EventHub/namespaces/$EVENTHUB_NAMESPACE/eventhubs/$EVENTHUB_NAME"
```

### 4. Blob Storage へのアクセス権付与

```bash
az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$LOG_STORAGE_ACCOUNT"
```

### 5. 環境変数の設定

Azure Portal または Azure CLI で環境変数を設定:

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

### 6. デプロイの確認

```bash
# ログストリームを確認
func azure functionapp logstream "$FUNCAPP"
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

Hive スタイルのパーティショニングと gzip 圧縮を使用した新しい形式:

```
コンテナ: logs-example-com
  └── y=2026/
      └── m=01/
          └── d=10/
              └── h=06/
                  └── m=00/
                      └── p=0/
                          └── part-o1234567890-o1234567900.ndjson.gz
```

パス形式: `y=<YYYY>/m=<MM>/d=<DD>/h=<HH>/m=00/p=<partition_id>/part-o<startOffset>-o<endOffset>.ndjson.gz`

- `y=YYYY`: 年
- `m=MM`: 月
- `d=DD`: 日
- `h=HH`: 時間（UTC）
- `m=00`: 分（固定値）
- `p=<partition_id>`: Event Hub パーティション ID
- `part-o<startOffset>-o<endOffset>.ndjson.gz`: オフセット範囲を含む圧縮ファイル

### NDJSON 形式

各行が1つの JSON オブジェクト（gzip 圧縮済み）:

```json
{"time_utc": "2026-01-10T12:34:56.123456+00:00", "fqdn": "example.com", "partition_id": "0", "offset": "1234567890", "sequence_number": "12345", "message": "Request received", "record": {...}}
{"time_utc": "2026-01-10T12:34:57.234567+00:00", "fqdn": "example.com", "partition_id": "0", "offset": "1234567891", "sequence_number": "12346", "message": "Connection timeout", "record": {...}}
```

## トラブルシューティング

### ログが保存されない

1. Function App のログを確認: `func azure functionapp logstream "$FUNCAPP"`
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
