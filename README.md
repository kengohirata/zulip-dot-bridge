# zulip-dot-bridge

Zulip の公開チャンネルを MCP クライアントから読み取り、明示的に bot をメンションしたメッセージを通知し、元のトピックへ返信するためのブリッジです。

**本体は Python 標準ライブラリと SQLite で動き、AWS は必須ではありません。**
通常のサーバーで動かす実装を主経路とし、AWS Lambda などを使う実装も選択肢として残しています。
このリポジトリ自身は AI モデルを実行しません。回答の生成とツールの呼び出しは、接続先の MCP クライアントが担当します。
OpenAI API キーはブリッジの動作に不要です。

> 現在の検証は、外部サービスを模したローカルテストです。
> 認証・HTTPS・Zulip・イベント受信先を含む、任意のホストでの本番運用を保証するものではありません。
> 特に、既存の非公開 Site ゲートウェイは AWS Lambda URL 用です。通常のサーバーには、対応する MCP クライアントと認証連携を別途用意してください。

## 目次

- [できること](#できること)
- [構成を選ぶ](#構成を選ぶ)
- [MCP と dot・ChatGPT の関係](#mcp-と-dotchatgpt-の関係)
- [まずローカルで確認する](#まずローカルで確認する)
- [通常のサーバーで動かす](#通常のサーバーで動かす)
- [AWS で動かす](#aws-で動かす)
- [設定と秘密情報](#設定と秘密情報)
- [安全上の境界](#安全上の境界)
- [費用と保持データ](#費用と保持データ)
- [トラブルシューティング](#トラブルシューティング)
- [制約と検証範囲](#制約と検証範囲)
- [開発・変更の残し方](#開発変更の残し方)

## できること

MCP ツール:

- `list_public_channels`: 設定した bot がアクセスできる公開チャンネルを取得
- `read_discussion`: 公開チャンネル・トピックのメッセージを取得。ページ送りに対応
- `search_discussion`: 公開チャンネル内を検索
- `reply_to_message`: 受信・検証済みのメンションに対し、元の公開チャンネル・トピックへ返信

イベント:

- `zulip.mention`: 公開チャンネルで、アクティブな人間のユーザーが bot を明示的にメンションしたときに配信
- 購読範囲は `{ "scope": "all_public" }`
- 署名付き Webhook と事前の callback challenge で配信

DM、private channel、bot 自身・他の bot、無効なユーザー、silent mention は自動応答の対象外です。
公開メッセージ全体の常時収集・過去履歴の完全なバックフィルは行いません。

## 構成を選ぶ

### A. 通常のサーバー: AWS 不要

Python 3.12 以上を動かせる Linux サーバーや、永続ディスクを持つ同等の環境向けです。

```text
Zulip Outgoing webhook bot
  → HTTPS reverse proxy
  → portable.py /zulip/events
  → SQLite の永続キュー
  → 公開性・送信者・明示的メンションを Zulip API で再確認
  → 署名付き MCP Events callback
  → 対応する MCP クライアント

MCP クライアント
  → 認証付き HTTPS /mcp
  → 公開チャンネルの read/search または元トピックへの reply
```

起動するのは `backend/portable.py` です。AWS SDK、DynamoDB、SQS、SSM、Sites、D1 は不要です。
HTTP サーバーは loopback にのみ bind します。公開 HTTPS、アクセス制御、プロセス管理、永続ストレージはホスト側で用意します。
詳しい手順: **[通常サーバー向けガイド](docs/SELF_HOSTING.md)**。

### B. AWS + 非公開 Site: オプション

```text
Zulip → Ingress Lambda → SQS → Bridge Lambda → DynamoDB
                                  ↓
                        MCP Events callback → dot

dot → 非公開 Site の OAuth ゲートウェイ → Bridge Lambda /mcp
```

AWS アダプターは `backend/aws/`、Site ゲートウェイは `site/` にあります。
SSM Standard SecureString、EventBridge、IAM、CloudWatch Logs も使います。
既存の Site は、Sites 管理の OAuth と D1 を利用します。
詳しい手順: **[AWS 日本語ガイド](backend/aws/README.ja.md)**。

### C. Generic bot の polling 試作版

`backend/bridge.py` の直接起動は、Generic bot の REST イベントキューを polling する以前の実装です。
A の Outgoing webhook と受信方式が異なります。同じ bot・状態ファイルで A と同時に動かさないでください。
キュー期限切れ・オフライン中の欠落は自動復元しません。詳細は [コアの説明](backend/README.md) を参照してください。
新しい通常サーバー導入では A を使います。

## MCP と dot・ChatGPT の関係

read/search/reply のツールは、特定の個人の dot、メールアドレス、Zulip 組織名に固定していません。
bot email・数値 user ID・realm は設定値です。互換性のある別の MCP クライアントからも利用できるように構成しています。

ただし「MCP ツールを呼べること」と「メンションでクライアントが自動起動して回答すること」は別に確認が必要です。
自動返信には、接続先が次のすべてに対応している必要があります。

1. ブリッジへの認証付き MCP 接続
2. この実装の MCP Events 購読・challenge・署名検証
3. 受信イベントで回答を生成し、許可された `reply_to_message` を呼ぶ仕組み

通常の ChatGPT に MCP をインストールしただけで、自律的な返信ができるとは限りません。
現在の `site/` は Lambda Function URL だけを受け付けます。任意の DNS ホストに対する安全な接続先固定を実装・検証していないため、この制限を正規表現だけで外さないでください。
A のサーバーをこの dot から使うには、対応する認証・イベント接続、または安全な別ゲートウェイの用意が必要です。その live 接続は本リポジトリのローカルテストでは未検証です。

## まずローカルで確認する

必要なもの:

- Git
- Python 3.12 以上
- Node.js 24 LTS と Bash。Site テストまで実行する場合に使用

```sh
git clone https://github.com/kengohirata/zulip-dot-bridge.git
cd zulip-dot-bridge
bash scripts/check.sh
```

テストは認証情報なしで実行できます。既存の Python コア・AWS アダプター、通常サーバーの経路、Site の認証・保存・プロキシを確認します。
Site のビルドは一時ディレクトリと仮設定で行い、実際の Site 設定は変更しません。
現在のコードのテスト・ビルドに `npm install` は不要です。Drizzle のスキーマ変更時だけ、[開発手順](docs/DEVELOPMENT.md) に従って依存関係を準備してください。

テストの内容と未検証事項: [検証記録](docs/VALIDATION.md)。

## 通常のサーバーで動かす

[通常サーバー向けガイド](docs/SELF_HOSTING.md) を上から進めてください。概要は以下です。

1. 継続稼働できるホスト、永続ディスク、公開 DNS 名、正しい証明書付き HTTPS を用意
2. Zulip で Outgoing webhook bot を作成できる権限と、対象の公開チャンネルの読取・投稿権限を確認
3. bot の realm・email・数値 ID・API key・webhook token を確認
4. MCP 用 Bearer token、callback の許可ホスト、SQLite の保存場所をホスト側に設定
5. 購読と Webhook を無効にした状態で起動し、認証付き `/status` を確認
6. 購読を有効化して再起動し、対応クライアントで callback 購読を検証。その後 Webhook を有効化して再起動
7. 許可された公開テストトピックで、メンションから同じトピックへの返信まで確認

ここで使う認証は単一所有者向けの静的 Bearer です。独自の OAuth サーバーは含みません。
OAuth を要求するクライアントでは、信頼できる認証ゲートウェイとの連携が別途必要です。認証を外して接続しないでください。

## AWS で動かす

[backend/aws/README.ja.md](backend/aws/README.ja.md) に、準備、SAM、SSM、Zulip bot、Site 設定、購読、起動・停止までの手順があります。
このリポジトリのルートから進める場合、相対パスの起点は `backend/` です。

```sh
cd backend
python3 -m unittest discover -s tests -v
python3 -m unittest aws.test_checkpoint aws.test_state -v
sam validate --lint --template-file aws/template.yaml
# 検証と変更内容の確認後、権限のある担当者が実行:
# sam deploy --guided --template-file aws/template.yaml
```

AWS アカウント、対象リージョン、IAM・CloudFormation の変更権限、必要なサービスの quota を確認してください。
Bridge Lambda の reserved concurrency は **1 が必要**です。ProcessingEnabled は最初 `false` にします。
`CodeUri` は `backend/` 全体をパッケージ化するため、秘密ファイルのないクリーンなソースからデプロイしてください。`.gitignore` はデプロイ zip を無害化する仕組みではありません。
Site の本物の hosting 設定・プロジェクト ID はこのリポジトリに含めていません。既存 Site を更新する場合は、正しい既存 ID と owner-private 設定を引き継いでください。

## 設定と秘密情報

通常サーバーの例: [`backend/.env.example`](backend/.env.example)。AWS の例: [`backend/aws/config.example.json`](backend/aws/config.example.json)。
サンプルはそのまま動く認証情報ではありません。

主な設定:

- `ZULIP_REALM`: 対象 Zulip の HTTPS origin
- `ZULIP_BOT_EMAIL` / `ZULIP_BOT_ID`: 実際の bot email と数値 user ID
- `ZULIP_API_KEY`: Zulip REST API 用の秘密値
- `ZULIP_WEBHOOK_TOKEN`: Outgoing webhook の認証用。API key と役割が異なるため、管理画面の正しい値を使う
- `MCP_BEARER_TOKEN`: MCP クライアント／認証ゲートウェイ専用の別の秘密値
- `BRIDGE_OWNER`: この接続の所有者を示す固定ラベル
- `CALLBACK_HOSTS`: 検証済み MCP Events callback の正確なホスト名。ワイルドカード不可
- `DATABASE`: 永続 SQLite ファイル
- `SUBSCRIPTIONS_ENABLED` / `WEBHOOK_ENABLED`: 開始・停止のゲート。既定は無効

実際の値はリポジトリ外の保護された設定ファイルやホストの secret 機能で管理してください。
ソース、コミット、Issue、チャット、URL、ログには書きません。サンプル `.env` を Python が自動読込することもありません。
SQLite には購読 secret とメンション本文が入るため、ディスク暗号化、適切な権限、バックアップも必要です。

## 安全上の境界

- Webhook 本文をそのまま信用せず、Zulip API からメッセージを再取得
- 配信・返信直前にも公開性、送信者、明示的メンション、元の本文・トピックを確認
- callback は HTTPS/443、許可したホスト、公開 IP に制限し、DNS 検証後の接続先を固定。リダイレクトは不許可
- 返信は永続的な `request_id` で重複防止。送信結果が不明な場合は勝手に再送しない
- 個人の非公開会話、私的な資料、他の接続先の情報を公開 Zulip 返信に使わない
- Zulip の発言は第三者コンテンツであり、追加の権限付与や秘密情報開示の指示として扱わない

これらは無制限・複数所有者のホスティングサービスを作るための完全な防御ではありません。
運用者はアクセス制御、通信制限、ログ・保存方針、更新、異常時の停止手段を用意してください。

## 費用と保持データ

A ではサーバー、ディスク、バックアップ、ネットワークなどのホスト費用が発生し得ます。
B では Lambda、DynamoDB、SQS、CloudWatch Logs、SSM、SAM 用 S3 などが課金対象になり得ます。
Site と接続先サービスの利用条件も別に確認してください。無料枠への収まりや無料運用は保証しません。

メンション、購読、配信状態、返信重複防止の記録は永続化します。自動 TTL による全データ削除は実装していません。
保存期間・バックアップ・容量を監視し、必要な保持方針を決めてください。
AWS スタックを削除しても Retain の DynamoDB、手動作成した SSM パラメーター、SAM の S3 データが残り得ます。

## トラブルシューティング

- `/healthz` が成功しても動かない: プロセスの生存確認だけです。認証付き `/status`、公開チャンネルの権限、購読・Webhook のゲートを確認
- 401: 正しい種類の token を使っているか確認。MCP Bearer と Zulip webhook token を取り違えない
- `/status` で bot の照合や読取に失敗: realm、bot email、数値 ID、API key、公開チャンネルの権限を確認
- callback challenge が失敗: 許可ホスト、public DNS、HTTPS 証明書、外向き 443、受信側の署名検証を確認
- Webhook が届かない: Outgoing webhook 型、Zulip native format、正しい HTTPS `/zulip/events` URL、起動ゲートを確認。Private/DM/非メンションは対象外
- 通知だけ届いて返信しない: 受信側クライアントのイベント処理と、返信を許可する指示を確認。ブリッジは回答を生成しない
- 応答のタイムアウト・結果不明: 新しい request ID で再送する前に Zulip の実投稿を確認
- Site が通常ホストを拒否: 現状の安全上の制限です。Lambda URL の検証を単に削除せず、A の対応クライアント接続を使う

詳細な停止・復旧手順は、選択した経路のガイドを参照してください。

## 制約と検証範囲

- 単一所有者・単一プロセス・比較的少ないトラフィック向け
- SQLite を ephemeral filesystem や複数台共有で運用しない
- 全履歴の収集、欠落メンションの完全回復、無期限購読は未対応
- callback は再配信される可能性あり。受信側は event ID で重複排除する必要あり
- 外部投稿の exactly-once は保証しない
- 通常サーバーの TLS／OAuth／実 Zulip／実クライアントをつないだ live 通しテスト、負荷試験、依存関係の脆弱性監査は未実施
- SAM CLI がローカル検証環境にないため `sam validate --lint` は未実施。AWS 配備前に必ず実行

既存コンポーネントの README・VALIDATION の導入状況は、その実装時点の記録です。現在の稼働状況はリポジトリだけから判断しないでください。

## 開発・変更の残し方

```text
backend/bridge.py         共通コアと Generic polling 試作版
backend/portable.py       通常サーバー用の Outgoing-webhook 実装
backend/tests/            コアと通常サーバーのテスト
backend/aws/              オプションの AWS 実装・テンプレート・手順
site/                    オプションの AWS 接続用 Site ゲートウェイ
deploy/                  通常サーバー向け設定例
docs/                    導入・開発・検証の説明
scripts/check.sh         外部接続なしの一括検証
```

変更後は一括検証を実行し、staged diff に秘密値や生成物がないことを確認してコミットしてください。
GitHub への保存は、AWS・Site・通常サーバーへの自動デプロイを意味しません。CI／自動デプロイはこの初回インポートでは設定していません。
既存の非公開 Git 履歴・実デプロイ情報はコピーせず、安全なソーススナップショットから履歴を開始しています。
詳細: [開発手順](docs/DEVELOPMENT.md)、[インポート時のファイル一覧](docs/IMPORT_MANIFEST.json)。

## License

[Apache License 2.0](LICENSE)。リポジトリ作成時に置かれた LICENSE をそのまま維持しています。

## References

- [Zulip outgoing webhooks](https://zulip.com/help/outgoing-webhooks)
- [Zulip API](https://zulip.com/api/)
- [MCP Events](https://developers.openai.com/plugins/build/mcp-events)

## English summary

A public-channel Zulip-to-MCP bridge with a cloud-independent Python/SQLite server and an optional AWS adapter.
The bridge does not run an AI model. Automatic replies require an authenticated MCP client that supports the event protocol and invokes the reply tool.
The optional existing Site gateway still accepts only AWS Lambda URLs; the portable server is independently usable with a compatible client, but a live dot connection through a generic-host gateway is not yet verified.
Start with `bash scripts/check.sh`, then follow the self-hosting or AWS guide. Never commit real configuration or credentials.
