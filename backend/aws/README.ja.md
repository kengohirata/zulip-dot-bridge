# オプション: 既存 dot と Zulip をつなぐ AWS ブリッジ

AWS を使わない場合は [通常サーバー向けガイド](../../docs/SELF_HOSTING.md) を参照してください。
このガイドは AWS + 既存の非公開 Site を選んだ場合だけに必要です。通常サーバーと AWS で同じ bot のイベントを二重に処理しないでください。

## この構成について

既存の非公開 Site プラグインを再利用します。ChatGPT の認証は Site の管理 OAuth が担当し、Site から AWS への通信だけを専用の共有シークレットで保護します。Lambda の URL を ChatGPT に直接登録する構成ではありません。

- OpenAI API キー、別の API 用 AI ボット、Cognito、Auth0 は不要です。
- dot が公開チャンネルを読み、公開トピック内で新しい bot を明示的にメンションした人に、同じトピックで返信できます。
- DM、private channel、他の bot の発言は応答対象外です。
- AWS が自動受信・保存するのは明示的メンションです。通常の公開メッセージは dot の read/search 時に取得し、全公開ログの常時収集・過去分バックフィルはしません。
- AWS 使用料は発生し得ます。無料を保証しません。DynamoDB の保存・アクセス、CloudWatch logs、S3、Lambda、SQS などが対象です。固定料金を減らすため Standard SecureString と AWS 管理キーを使い、Secrets Manager・customer-managed KMS key・PITR・有料 alarm はデフォルトで作りません。
- EC2、NAT Gateway、API Gateway は作りません。
- このソースのローカルテストは完了していますが、リポジトリの検証では AWS と Zulip への実デプロイや実メンションから返信までの live 通しテストは行いません。実際の構成ごとに確認してください。

経路:

```
ChatGPT / 既存 dot
  → 既存の非公開 Site（管理 OAuth・所有者確認）
  → AWS Lambda /mcp（専用 proxy secret）

Zulip Outgoing webhook bot
  → 専用 Ingress Lambda /zulip/events（Zulip webhook token）
  → SQS → Lambda → DynamoDB outbox
  → ChatGPT が登録した MCP Events callback → 既存 dot
```

## 最初に用意するもの

1. 非公開 Site プラグインが同じ ChatGPT アカウントにインストール済みであること。
2. 自分でリソースを作成・変更できる AWS アカウントと、Zulip の bot 作成権限。
3. このフォルダーを含むソース一式。`bridge.py` と `aws/` の配置を変えないでください。
4. 本番認証情報を含まないパッケージであること。API キーをソースに書き込んだ状態でアップロードしないでください。

実行と IAM/リソース変更はすべてあなた自身が行います。手順は確認してから進めてください。
既存の `generic-bot@your-organization.zulipchat.com` Generic bot は削除しません。常時接続しない構成では新規の Outgoing webhook bot を使います。種類の変更・アカウント名の流用は前提にしません。

## 1. AWS CloudShell でファイルを準備する

AWS コンソールで使用するリージョンを選び、CloudShell を開きます。AWS CLI と SAM CLI の利用可能性を以下のコマンドで確認してください。
公開リポジトリのソースを取得し、`bridge.py` がある `backend/` へ移動します。

```sh
git clone https://github.com/kengohirata/zulip-dot-bridge.git
cd zulip-dot-bridge/backend
sam --version
aws sts get-caller-identity
python3 -m unittest discover -s tests -v
python3 -m unittest aws.test_checkpoint aws.test_state -v
sam validate --lint --template-file aws/template.yaml
```

`CodeUri` は `backend/` 全体を取り込むため、秘密値・実設定・データベースのないクリーンな checkout から進めてください。`.gitignore` は SAM のパッケージ内容を除外しません。

最後の検証が失敗した場合は、デプロイしないでください。AWS アカウント・リージョンが想定どおりかも確認します。
このコードには追加インストールが必要な Python ランタイム依存はありません。AWS Lambda の boto3 を使い、`sam build` は不要です。

CloudShell の公式説明: https://docs.aws.amazon.com/cloudshell/latest/userguide/vm-specs.html

## 2. 処理を止めた状態でスタックを作る

```sh
sam deploy --guided --template-file aws/template.yaml
```

推奨入力:

- Stack Name: `zulip-dot-bridge`
- AWS Region: 自分が選んだリージョン
- Parameter ConfigParameterName: `/zulip-dot-bridge/config`
- Parameter ProcessingEnabled: `false`
- Confirm changes before deploy: `Y`
- Allow SAM CLI IAM role creation: テンプレートの権限を確認してから `Y`
- Save arguments to configuration file: `Y`

この実行で 2 つの Lambda と Function URL、DynamoDB、SQS/DLQ、IAM role、CloudWatch logs、EventBridge が作成されます。SSM の Standard SecureString は後の手順で自分で作成します。生成された change set を確認して実行します。

Function URL は `AuthType: NONE` ですが、Bridge URL の `/mcp` と `/status` は専用共有シークレット、別の Ingress URL の `/zulip/events` は webhook token を必須にしています。公開 URL のため、認証失敗のアクセスでも Lambda の呼び出し費用・負荷は生じ得ます。

Outputs に出た `FunctionUrl`（Site 用）、`ZulipWebhookUrl`（Zulip 用）、`ConfigParameterName`、`FunctionName`、キュー・テーブル名を控えてください。URL・リソース名は秘密値ではありません。

注意: Bridge Lambda（MCP/ワーカー）の reserved concurrency は **1 が必須** です。専用 Ingress Lambda はこれと分離し、予約並列数を設定しません。アカウント quota の都合で設定できない場合は制限緩和を依頼し、1 を削除して回避しないでください。同時実行が増えると、この単一所有者向け実装の配信停止保証が崩れます。

## 3. 新規 Outgoing webhook bot を準備する

Zulip の bot 管理で **Outgoing webhook** 型の bot を新しく作ります。表示名・email は既存 bot と区別してください。

- Interface: Zulip native format
- Target URL: Outputs の `ZulipWebhookUrl` をそのまま使う。Site 用の `FunctionUrl` と混同しない
- 例: `https://ID.lambda-url.REGION.on.aws/zulip/events`

この段階では Lambda 側は webhook を受け付けません。公開チャンネルで bot をメンションするテストは後にします。
bot の API key、webhook token、bot ID、email を管理画面またはダウンロードした zuliprc 等で確認してください。**bot ID は数字の user ID** であり、email ではありません。bot ID が不明なら管理画面や Zulip API の `users/me` で確認します。キーをチャットに貼らないでください。

Outgoing webhook token と API key は別の用途です。管理画面に出た正しい値を使い、同一だと推測しないでください。
公式: https://zulip.com/help/outgoing-webhooks

## 4. 秘密値をコンソールへ直接入力する

専用 proxy secret をパスワードマネージャーの生成機能で作ります。最低 32 バイト相当のランダム値、例として 43 文字以上のランダムな base64url 値を使います。
Zulip API key や webhook token を流用しません。生成した値をチャット・コマンド引数・ソース・ログに貼らないでください。

AWS Systems Manager → Parameter Store → Create parameter で、Outputs の `ConfigParameterName` と同じ名前（標準は `/zulip-dot-bridge/config`）のパラメーターを作ります。Tier は **Standard**、Type は **SecureString**、KMS key は AWS 管理の **aws/ssm**。Value に `aws/config.example.json` の JSON を直接入力し、例の値を置き換えます。Advanced tier、高スループット、カスタマー管理 KMS key は選びません。

- `zulip_realm`: `https://your-organization.zulipchat.com`
- `zulip_bot_email`, `zulip_bot_id`: 新規 Outgoing webhook bot の値
- `zulip_api_key`, `zulip_webhook_token`: bot の秘密値
- `bridge_proxy_secret`: 生成した専用シークレット
- `bridge_owner`: `private-site-owner` など、この接続用の固定ラベル。接続後は不用意に変えない
- `subscriptions_enabled`: `false`
- `webhook_ready`: `false`

callback URL は認証済みの MCP 購読リクエストから受け取り、その都度 public DNS の HTTPS/443 であることを検証し、接続先 IP を固定してアクセスします。リダイレクト、private/link-local IP は許可しません。事前のホスト名入力は不要です。
パラメーターの変更は最大約 60 秒キャッシュされます。Standard の値サイズ上限 4 KB を超えないようにしてください。

## 5. 既存の非公開 Site で AWS 接続を保存する

実際に所有している同じ非公開 Site の `/setup` を開き、以下を直接入力します。サンプルの Site URL はありません。既存 Site の URL・ID を確認し、別の Site を誤って設定しないでください。

- AWS endpoint: Outputs の FunctionUrl（画面の案内どおりに入力）
- Proxy secret: SSM Parameter Store と同じ専用シークレット
- Expected realm: `https://your-organization.zulipchat.com`

保存後のツール一覧更新は同じ dot で再確認します。プラグインを重複インストールする必要はありません。

この保存操作により、Site へ secret を保管し、AWS 経由でこの組織の公開チャンネルを読み取り、メンション返信を扱える接続を設定します。画面の説明と確認を読み、あなた自身で保存してください。

Site は `GET /status` にアクセスし、新規 bot の ID/email/is_bot と公開チャンネル取得を照合し、実際に 1 件までの公開メッセージ読み取りを試します（空の公開チャンネルで正常な空結果でも可）。公開チャンネルが 0 件なら `no_public_channels` で停止します。
成功時の主要値は `configured: true`、`public_read_preflight_ok: true` です。まだ `subscriptions_enabled: false` で正しい状態です。
`/healthz` が 200 でも bot の設定・認証・イベントは確認済みとは限りません。

## 6. 購読とワーカーを有効にする

read preflight が成功した後に進みます。

1. SSM Parameter Store の `subscriptions_enabled` を `true` にする。`webhook_ready` はまだ `false`。
2. 約 60 秒後、Site から接続状態を再確認する。
3. 同じ dot に、次のように範囲を明示して購読を依頼する。
   「この Zulip 接続の zulip.mention を scope: all_public で購読して。新規 bot に明示的にメンションした人には、公開チャンネルの研究の議論に関係する内容だけを、元の公開トピックで返信して。私の非公開の dot 会話、個人情報、他の接続先の情報は公開しないで。」
   実際の希望する範囲と一致することを確認してから送る。購読は公開チャンネルの新規 bot への明示的メンションだけが対象。
4. callback challenge が成功し、購読が作成されたことを確認する。失敗した場合は次へ進まない。
5. 次のコマンドを再実行し、ProcessingEnabled を `true` に変える。

```sh
sam deploy --guided --template-file aws/template.yaml
```

同じ stack/region を選び、change set を確認します。これにより SQS ワーカーと 5 分ごとの outbox 回復が有効になります。
最後に SSM Parameter Store の `webhook_ready` を `true` にし、約 60 秒待ちます。

## 7. 公開テストトピックで確認する

1. bot がアクセスできる公開チャンネルのテスト用トピックで、新規 bot を明示的にメンションして短いメッセージを送る。
2. 同じ dot に届くこと、返信が元の公開トピックに新規 bot 名義で返ることを確認する。
3. メンションなしの発言、別 bot の発言、DM、private channel に反応しないことを確認する。
4. 元のメッセージを削除・編集した後、古いイベントに基づく返信が止まることを確認する。
5. イベント購読解除後に新しい callback が開始されないことを確認する。

この通しテストが成功するまでは本番稼働と扱わないでください。
返信 API のタイムアウトが出たときは新しい request_id で即座にやり直さず、Zulip に既に投稿されていないか確認します。

## 障害対応と停止

- `/status` 401: Site と AWS の proxy secret が不一致、未設定、またはキャッシュ反映前。
- `/status` 503: bot ID/email/key の誤り、Zulip の接続障害・権限不足など。秘密値を含むログ出力を追加しない。
- 購読 challenge 失敗: callback の到達性、public DNS、外向き HTTPS、署名検証を確認。
- webhook 503: `webhook_ready=false` または構成未完了。
- MCP/ワーカーの 429/遅延: Bridge Lambda の直列利用と DynamoDB の低い provisioned capacity により、負荷時に起こり得ます。並列数を勝手に上げない。
- Ingress は SSM/SQS だけに短時間でアクセスし、Zulip API や callback を待ちません。それでも AWS アカウント全体の制限や障害で 429/503 が出た場合、Zulip は HTTP エラーを再送しないことがあります。Zulip の失敗表示を確認して、復旧後に再メンションする必要があります。
- キューが滞留: Lambda Errors/Throttles、SQS oldest message、DLQ を確認。DLQ は 14 日保持、通常キューは 4 日保持です。
- 無料枠重視のため alarm・通知先は作りません。運用時はキュー/エラーを定期確認してください。通知が必要なら料金を確認して CloudWatch/SNS 等を自分で追加します。
- 計画停止: 先に dot の購読を解除し、SSM Parameter Store の `webhook_ready` と `subscriptions_enabled` を `false` にする。次に ProcessingEnabled を `false` で更新する。
- 緊急停止: Lambda コンソールで Bridge と Ingress 両方の reserved concurrency を `0` にすると全呼び出しを止められます。再開時は Bridge を `1`、Ingress は予約設定を解除する状態に戻し、未配信イベントを点検します。

## 保持・費用・制約

この実装は低頻度・単一所有者向けです。DynamoDB 本体と未完了処理用 GSI はそれぞれ PROVISIONED の 1 RCU / 1 WCU（合計 2 RCU / 2 WCU）と AWS-owned encryption key を使います。無料枠はアカウント条件・他の使用量に依存し、保証しません。バースト時のスロットリングは SDK/SQS の再試行と outbox 回復で扱います。大量の会話向けには容量・設計の見直しが必要です。回復処理は sparse GSI にある未完了の ingest/配信だけを照会し、完了済みの全履歴を scan しません。active 購読は小さな永続 ID 一覧から取得します。それでも大規模組織向けの無制限運用は意図していません。
DynamoDB にはメンション本文、callback 登録情報・署名 secret、返信重複防止状態が保存されます。TTL による自動削除はありません。保存が増え続けるため、テーブル容量・アクセス費用・回復処理時間を監視し、運用期間に応じたデータ削除を別途検討してください。削除は重複防止・障害回復にも影響するので一括 purge はしません。

CloudFormation でスタックを削除しても、DynamoDB は Retain により残ります。手動作成した SSM Standard SecureString も残ります。DynamoDB 保存データの課金も残り得ます。S3 の SAM アップロードファイルや SAM 管理バケットも残り得るため、使用終了時に内容・保持要否を確認してください。

callback は at-least-once 配信です。受信側は event ID で重複排除します。最大 8 回の配信試行後は `failed` として状態に残り、自動的に無限再送しません。Zulip 投稿は別で、結果が不明なときは自動再送しません。

## 実装者向け確認

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest aws.test_checkpoint aws.test_state -v
python3 -m compileall -q bridge.py aws
sam validate --lint --template-file aws/template.yaml
```

実施済み/未実施の詳細は `VALIDATION.md` を参照してください。
