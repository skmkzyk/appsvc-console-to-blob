# プロジェクト概要

このプロジェクトは、Azure Event Hubsから受信したApp Serviceのコンソールログを、Azure Blob Storageに保存するAzure Functionsアプリケーションです。

## アーキテクチャ

- **トリガー**: Azure Event Hubs（診断ログのストリーミング元）
- **処理**: Python Azure Functions（Event Hub Trigger）
- **保存先**: Azure Blob Storage（Append Blob形式でNDJSON）

## 主要な機能

### 1. ログの取得と正規化
- Event Hubから受信したペイロードを正規化
- 複数の形式に対応:
  - `[{"records": [...]}]` 形式
  - `{"records": [...]}` 形式
  - 単一レコード `{...}` 形式
  - 非JSON テキスト形式

### 2. FQDN抽出とコンテナ分離
- ログメッセージから `X-Forwarded-Host` ヘッダーを優先的に抽出
  - App ServiceがAzure Front Doorのバックエンドとして配置されている場合、Header rewriteにより元のHostヘッダーが書き換えられる
  - `X-Forwarded-Host` ヘッダーにオリジナルのホスト名が保持されるため、これを優先的に参照
- フォールバック: 正規表現で `Host:` パターンをマッチング
- FQDNごとに別のBlobコンテナに保存（例: `logs-example-com`）
- コンテナ名はAzureの命名規則に準拠（小文字、ハイフン、3-63文字）

### 3. 日次Blobへの追記
- 日付ベースのディレクトリ構造: `YYYY/MM/DD/console.ndjson`
- Append Blobを使用して複数のイベントを効率的に追記
- オプション: パーティションIDでシャーディング可能（高スループット環境向け）

### 4. 認証方式
以下の2つの認証方式をサポート:
- **接続文字列** (開発・テスト向け): `LOG_STORAGE_CONNECTION`
- **Managed Identity** (本番推奨): `LOG_STORAGE_ACCOUNT_NAME` + `DefaultAzureCredential`

## 環境変数

### Event Hubs関連
- `EVENTHUB_NAME`: Event Hub名（接続文字列にEntityPathがある場合は不要）
- `EVENTHUB_CONNECTION_SETTING`: App Settingsのキー名（デフォルト: `EventHubConnection`）

Managed Identityを使用する場合のApp Settings設定例:
```
EventHubConnection__fullyQualifiedNamespace = <namespace>.servicebus.windows.net
EventHubConnection__credential = managedidentity
EventHubConnection__clientId = <optional-UAMI-clientId>
```

### Blob Storage関連
**方法A: 接続文字列（シンプル）**
- `LOG_STORAGE_CONNECTION`: ストレージアカウントの接続文字列

**方法B: Managed Identity（推奨）**
- `LOG_STORAGE_ACCOUNT_NAME`: ストレージアカウント名
- `LOG_STORAGE_MANAGED_IDENTITY_CLIENT_ID`: （オプション）UAMIのクライアントID

### その他
- `LOG_CONTAINER_PREFIX`: コンテナ名のプレフィックス（デフォルト: `logs-`）
- `LOG_BLOB_BASENAME`: Blobファイル名（デフォルト: `console.ndjson`）
- `LOG_BLOB_SHARD_BY_PARTITION`: パーティションシャーディング有効化（`1`/`true`/`yes`）

## コーディング規約

### 設計パターン
- **シングルトンパターン**: `BlobServiceClient` はグローバル変数でキャッシュ
- **遅延初期化**: クライアントは最初の使用時に作成
- **キャッシング**: 同一実行内でコンテナとBlobクライアントをキャッシュして重複する作成/存在チェックを削減

### エラーハンドリング
- コンテナ作成: `ResourceExistsError` は無視（冪等性）
- FQDN抽出: パース失敗時は `"unknown"` にフォールバック
- パーティションID取得: 防御的プログラミング（複数のキー名を試行）

### データ形式
- 出力形式: NDJSON（改行区切りJSON）
- エンコーディング: UTF-8
- Append Blob制限: 4MiB チャンクで分割書き込み

### 型ヒント
- すべての関数に型ヒントを使用
- `typing` モジュールから `Any`, `Dict`, `List`, `Optional`, `Tuple` をインポート

### ロギング
- `logging` モジュールを使用
- 処理完了時に統計情報をログ出力

## 実装時の注意点

### Blobコンテナ命名
- Azureの厳格な命名規則に従う:
  - 小文字のみ
  - 文字、数字、ハイフン
  - 3〜63文字
  - 連続ハイフンや先頭/末尾ハイフンは不可
- `NON_ALLOWED` と `MULTI_HYPHEN` 正規表現で正規化

### 正規表現パターン
- `HOST_REGEX`: ケースインセンシティブで `host:` または `host =` をマッチング
- ポート番号は除去（例: `example.com:443` → `example.com`）

### Append Blob
- `create_append_blob()` は冪等ではないため、`exists()` でガード
- 4MiB制限を超えないようチャンク分割

### パフォーマンス最適化
- コンテナとBlobクライアントを関数実行内でキャッシュ
- 高スループット環境ではパーティションシャーディングを有効化

## デプロイ

1. Azure Functionsにデプロイ
2. Managed Identityを有効化
3. Event HubsとBlob Storageへのアクセス権を付与:
   - Event Hubs: `Azure Event Hubs Data Receiver`
   - Blob Storage: `Storage Blob Data Contributor`
4. 環境変数を設定（上記参照）

## テストとデバッグ

### ローカルテスト
- `local.settings.json` に環境変数を設定
- `LOG_STORAGE_CONNECTION` を使用して接続文字列でテスト

### サンプルペイロード
- `event-hub-sample.json` にサンプルペイロードを配置（参照用）

## 今後の拡張可能性

- カスタムメッセージフィールドの抽出ロジック追加
- レコードフィルタリング機能
- 異なる保存先（Table Storage、Cosmos DBなど）へのルーティング
- メトリクスとアラート統合
