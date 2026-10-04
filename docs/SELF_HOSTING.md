# 通常のサーバーで動かす

この手順は `backend/portable.py` 用です。AWS と Sites は使いません。
Python 3.12 以上、ローカル永続ディスク、公開 HTTPS、対応する認証付き MCP クライアントが必要です。
コード・設定例はローカルで検証していますが、実際のサーバーへのインストール、TLS、Zulip、MCP クライアントをつないだ live 通しテストは未実施です。

## 1. 事前に決めること

- 常時稼働するホストと管理者。ここでは Linux を例に説明します
- ブリッジ専用の公開 DNS 名と、信頼された CA による証明書
- SQLite とキューを保存する永続ディスク、バックアップ、保持期間
- Outgoing webhook bot を作成できる Zulip の権限
- 接続する MCP クライアントの認証方法と MCP Events 対応
- クライアントの callback の正確な DNS ホスト名

このサーバーは単一所有者・単一プロセス向けです。横に複数起動する構成や共有ネットワークファイルシステムを前提にしません。
`bridge.py` の Generic polling プロセスとは同じ状態ファイルを使わず、同時起動しないでください。

## 2. コードとテスト

```sh
git clone https://github.com/kengohirata/zulip-dot-bridge.git
cd zulip-dot-bridge
bash scripts/check.sh
```

サーバー本体は Python 標準ライブラリのみです。Python 用の追加パッケージ、AWS SDK、npm は本体の起動に不要です。
Node.js 24 はオプションの Site を含む一括テストに使います。
コードをサービス用の読取専用パスに配置し、書込可能なデータディレクトリとは分けてください。

## 3. Zulip bot

Zulip の bot 管理画面で、**Outgoing webhook** 型の bot を新しく作ります。
既存の Generic bot を使い回したり、型を変更できると仮定したりしないでください。

- Format: Zulip native format
- Endpoint: 自分のサーバーの `https://bridge.example.com/zulip/events` に相当する URL
- API key、Webhook token、bot email、数値 user ID を確認

この URL は例です。自分で管理するホストに置き換えてください。
API key と webhook token は役割が異なります。管理画面の正しい値を使い、同一だと推測しません。
Zulip の公式仕様では、Outgoing webhook は指定 URL への HTTP POST です。AWS は必要ありません。
メンションと DM が Webhook を起動できますが、この実装では公開チャンネルの明示的メンションだけを処理します。

公式: [Outgoing webhooks](https://zulip.com/help/outgoing-webhooks)

## 4. 秘密値をホストに設定する

`backend/.env.example` を参考に、実設定を **Git 作業ツリーの外** の保護されたファイルや secret 機能へ入力します。
本物の token をコマンドライン引数、シェル履歴、チャットに貼り付けないでください。

推奨配置例:

- 設定: `/etc/zulip-mcp/bridge.env`、管理者だけが読める権限
- 状態: `/var/lib/zulip-mcp/bridge.sqlite3`、サービスユーザーだけが利用できるディレクトリ
- コード: `/opt/zulip-dot-bridge/backend/`

環境変数:

- `ZULIP_REALM`: `https://your-organization.zulipchat.com` に相当する組織 origin
- `ZULIP_BOT_EMAIL`、`ZULIP_BOT_ID`: 作成した bot の実値
- `ZULIP_API_KEY`: Zulip REST API 用
- `ZULIP_WEBHOOK_TOKEN`: Webhook 受信認証用
- `MCP_BEARER_TOKEN`: 別途生成した強いランダムなクライアント認証用 token。Zulip の秘密値と流用しない
- `BRIDGE_OWNER`: この接続の固定ラベル
- `CALLBACK_HOSTS`: 受信側 MCP サービスが提供した正確なホスト名のカンマ区切り。URL、port、ワイルドカードは不可
- `DATABASE`: 永続 SQLite ファイルへの絶対パス
- `PORT`: loopback で待つポート。例は 8080
- `SUBSCRIPTIONS_ENABLED=false`
- `WEBHOOK_ENABLED=false`
- `INGRESS_MAX_ATTEMPTS=8`、`INGRESS_MAX_PENDING=10000`: 受信キューの再試行回数・上限。必要性を確認して変更

Python は `.env` を自動的には読みません。サービスマネージャーまたはホストの secret 機能からプロセス環境へ渡します。
callback ホストがまだ分からない場合、適当なホスト名や `*` を入れて先へ進めないでください。MCP 受信側の接続手順で確認します。

## 5. HTTPS とプロセス管理

`portable.py` の HTTP は `127.0.0.1` に限定されます。前段に HTTPS reverse proxy を置いてください。

- 公開 DNS の正しい証明書。自己署名証明書や検証の無効化を使わない
- 受信本文の上限、ヘッダー・本文・上流の timeout、同時接続・rate 制限
- `/mcp` と `/status` の Authorization header を保持
- `/zulip/events` は Zulip 本文の token で認証。MCP Bearer を要求する設定を一括で掛けない
- Authorization、Webhook 本文、メッセージ本文、secret、callback URL をログに残さない
- ローカル 8080 を外部へ直接公開しない
- 外向き HTTPS と DNS を許可。callback は HTTPS/443、公開 IP のみ

設定の骨組みは [`deploy/nginx.conf.example`](../deploy/nginx.conf.example)、[`deploy/zulip-mcp.service.example`](../deploy/zulip-mcp.service.example) にあります。
これは自動インストーラーではありません。ホストの OS、証明書配置、ユーザー、既存 proxy と競合しないよう確認してから使ってください。
TLS 発行・更新、OAuth ゲートウェイ、監視設定を自動で構築するものでもありません。

プロセスに環境が安全に設定された状態での実行コマンド:

```sh
cd /opt/zulip-dot-bridge/backend
python3 portable.py
```

`/healthz` はプロセスの生存確認です。設定や Zulip の利用可能性を保証しません。
認証付き `/status` は bot の ID/email/is_bot、公開チャンネル一覧、公開メッセージの読取を確認します。
認証はクライアントの安全な入力欄・設定を使って渡し、URL query に token を付けないでください。

## 6. MCP 接続、購読、受信を順に有効化

1. `SUBSCRIPTIONS_ENABLED=false`、`WEBHOOK_ENABLED=false` で起動し、認証付き `/status` の preflight 成功を確認
2. 接続先クライアントで、HTTPS `/mcp` と Bearer 認証の互換性を確認
3. `SUBSCRIPTIONS_ENABLED=true` にして再起動。Webhook はまだ `false`
4. 対応クライアントから `zulip.mention`、`{ "scope": "all_public" }` の購読を作成。署名付き callback challenge が成功することを確認
5. `WEBHOOK_ENABLED=true` にして再起動。購読は永続ディスクに保存される
6. 公開テストトピックで、権限のある人間のユーザーが bot を明示的にメンション
7. 同じトピックにだけ返信されることを確認

購読は最大 1 日で期限が切れます。受信側が返された期限に従って更新する必要があります。
この実装はその renewal をクライアントの代わりに予約しません。

MCP ツールを使えるクライアントでも、MCP Events に対応しなければメンション起動はできません。
この dot の現在の `site/` アダプターは Lambda URL 専用です。通常サーバーの URL をその画面へ入れても接続できません。
独自 OAuth サーバーは同梱していないので、OAuth 必須のクライアントでは信頼できる認証ゲートウェイが必要です。
この条件が整う前に「dot と接続済み」「自動返信が稼働中」と扱わないでください。

## 7. 動作・除外条件の確認

- 同じ Webhook が重複しても、受信キュー・イベントを追加で作らない
- callback は at-least-once 配信なので、受信側が eventId で重複排除し、意図した同じ返信には同じ request_id を使う。別の request_id は別の返信として扱われる
- DM、private channel、他 bot、通常メッセージ、silent mention を除外
- 待機中に対象メッセージを削除・編集・非公開化した場合、古い内容の配信・返信が止まる
- 再起動後も未処理の受信・配信キューと返信重複防止状態が残る
- 購読解除後に新たな callback 配信が開始されない
- token 誤り、認証なし、別 Origin、過大・不正な本文が拒否される
- 返信 POST がタイムアウトしたら、Zulip の実投稿を点検してから判断。新しい request ID で即再送しない

テストで使うデータにも秘密情報や private な研究内容を含めないでください。

## 8. 停止と復旧

計画停止:

1. クライアントからイベント購読を解除
2. `WEBHOOK_ENABLED=false` と `SUBSCRIPTIONS_ENABLED=false` にしてサービスを再起動、または停止
3. 未処理・失敗したキュー、ディスク容量、データ保持を点検

緊急時はサービス停止または前段 proxy の route 停止で受信・処理を止めます。
再開時は preflight と有効な購読を確認し、保存された処理が再開し得ることを確認してください。
Webhook のタイムアウトや受信拒否は、Zulip が確実に再送すると仮定できません。必要なら障害表示と対象メッセージを確認し、復旧後に許可された再メンションを依頼します。
再試行上限を超えた処理は無限に再送しません。キューを消して直すのではなく、原因と実際の配信・投稿結果を先に調べてください。
認証付き `/status` の `ingress`・`deliveries` の状態別件数、`active_subscriptions`、`worker_error` を監視に使えます。本文や secret は返しません。
SQLite の `-wal`・`-shm` と、同じパスの `.portable.lock` は実行時に作成されます。稼働中の lock file を削除しないでください。

## 9. 運用上の制約

- 一般的な Python HTTP サーバーを trusted reverse proxy の内側で使う構成。高負荷・マルチテナント向けではない
- キューと SQLite は永続ディスクが必要。容量監視と暗号化・バックアップはホスト側の責任
- 完了履歴・本文・secret の自動保持期限は未実装
- 複数プロセスで同じデータベースを開かない。起動ロックは運用上の補助で、legacy プロセスとの競合まで保証しない
- callback・Zulip 接続、OAuth 連携、サーバーの本番負荷は別途検証が必要

停止処理は実行中の処理を待ちます。外向き通信には無通信 timeout がありますが、DNS を含む厳密な処理全体の期限は保証しません。運用側にも停止時間・リソース監視を用意してください。
