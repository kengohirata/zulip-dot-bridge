> 通常サーバー向けの主経路は `portable.py` です。AWS は不要です。
> 導入は [通常サーバー向けガイド](../docs/SELF_HOSTING.md)、全体像は [root README](../README.md) を参照してください。
> 以下は共通コアと以前の Generic-bot polling 試作版の詳細・実装時点の記録です。導入状況に関する過去の記述を現在の live 状態の確認と混同しないでください。

# Zulip → existing dot bridge (local prototype)

A credential-free implementation checkpoint for the existing dot, not a new AI
assistant. There is no OpenAI API client, model, model API key, or model inference
in this repository. A connected plugin and a user-created MCP Events subscription
in the existing dot are still required to wake that dot and produce a response.

## Requested behavior

1. Read/discover **all public channels** accessible to the configured bot in
   `https://your-organization.zulipchat.com`.
2. Deliver an event when **any active human member** explicitly mentions the bot
   in a public channel. Let the existing dot reply as
   `generic-bot@your-organization.zulipchat.com` to the same channel and topic.

Private channels, DMs, inactive accounts, other bots, self messages and silent
mentions are excluded. No unsolicited channel replies are generated.

## Files and components

- `bridge.py`: Python 3.12+ standard library only. MCP endpoint, protocol logic,
  public-only Zulip client, generic-bot event queue listener, SQLite state,
  callback TLS/DNS validation, delivery and reply idempotency.
- `tests/test_bridge.py`: local deterministic tests; all remote calls mocked.
- `.env.example`: nonsecret configuration template; intentionally cannot start.
- `.gitignore`: excludes credentials, SQLite files and bytecode.

`Listener.step()` is the ingestion adapter; `Bridge.ingest_event()` accepts only
its authenticated Zulip event responses. `Bridge.rpc()` handles MCP requests and
`Bridge.deliver_one()` sends one durable queued event. The HTTP and polling
wrappers are separate from these methods, but SQLite is currently built into
Bridge. A managed-serverless deployment needs a proper persistent storage adapter
and event transport implementation; copying this SQLite file into ephemeral
Lambda/Worker storage is **not safe**. The separate `aws/` implementation overrides
persistence with DynamoDB; see its Japanese guide for the Site-proxy deployment.
Cloudflare hosting for this Python process is not included.

## Test (no credentials, no network)

```sh
cd zulip-dot-bridge
python -m unittest discover -s tests -v
python -m py_compile bridge.py
```

## Protocol surface

Authenticated `POST /mcp` accepts JSON-RPC 2.0 methods:

- `server/discover`: `supportedVersions: ["2026-07-28"]`, tools + events
- `tools/list`, `tools/call`
- `events/list`, `events/subscribe`, `events/unsubscribe`

Tools: `list_public_channels`, `read_discussion`, `search_discussion`,
`reply_to_message`. Reads require a public channel ID and support optional topic,
limit (up to 100), and `before_id` pagination. Search adds a query. A reply requires
an observed mention message ID, content and stable unique `request_id`.

Event: `zulip.mention`. Subscription arguments are `{"scope": "all_public"}` for all public
channels. Event payloads use `stream_id`; read/search tools retain `channel_id`. Delivery uses the MCP
Events webhook envelope and Standard Webhooks HMAC-SHA256 signatures. Secrets
must be `whsec_` plus base64 encoding of 24–64 bytes. Each new/rekeyed callback
must echo a signed random challenge before activation. Verification caches last
five minutes. Renewal rotates signing keys with a five-minute overlap.
Subscriptions expire after at most one day (no infinite grant), return no replay
cursor, and survive restart.

Callbacks must use HTTPS port 443 on an explicitly configured exact host.
Every connection resolves and checks **all** DNS answers for public IPs, then
connects to the checked address preserving hostname TLS verification. Private,
loopback, link-local, multicast and IPv4-mapped IPv6 addresses are blocked. No
redirects, proxy discovery or insecure TLS are supported.

`/healthz` is a basic process liveness endpoint, not proof that credentials,
subscriptions, listener or delivery work. HTTP binds to loopback only. A real
HTTPS ingress with request/concurrency limits is needed before public exposure.

## Generic bot collection

The listener verifies the configured bot ID/email through `GET /users/me`, then
registers `event_types=["message"]`, `all_public_streams=true` and polls the
queue every two seconds in this prototype. It does not subscribe the bot to
channels or change channel membership. Public channel discovery is refreshed
from REST and excludes `invite_only` channels. Every candidate message is fetched
from the configured realm's authenticated REST API before use. Explicit mentions
are checked using Zulip-rendered mention markup; active human status is fetched
from that same realm. Queue progress is saved only after durable processing.

Collection starts with current queue traffic. Historical messages are available
through paginated live read/search tools; **this is not a completed full-history
backfill**. The local public-message cache is not exposed as an authoritative
read tool. An expired queue is re-registered and a `last_gap` diagnostic is saved.
**Offline gaps are not automatically recovered**, including mentions during an
expired queue gap. The old queue cursor cannot guarantee an uninterrupted
archive. This is a known limitation requiring a backfill/reconciliation design
before claiming lossless continuous collection.

## Safety, durability and remaining hardening

- Auth is intentionally a **single-owner static Bearer prototype**, not an OAuth
  provider. Plugin installation authentication compatibility is unverified.
  Keep behind trusted ingress; build/verify the platform-supported auth flow
  before a real plugin installation. Never disable authentication as a shortcut.
- Public status, sender status, canonical text/topic/location and explicit
  mention are rechecked before callback delivery and before a reply. Changed,
  removed or no-longer-public messages are dropped or blocked. A small upstream
  change-between-check-and-send race is unavoidable without server-side atomic
  authorization.
- SQLite transactions deduplicate source messages and delivery IDs. Callback
  retries preserve exact event body/ID and use fresh timestamps/signatures.
  Transient failures back off up to eight attempts; `410`/`413` are never retried.
- Reply request IDs are reserved durably **before** POST. If a send times out,
  the result remains uncertain and is not retried automatically. The operator
  must inspect Zulip before deciding how to recover. Reusing an ID for different
  content fails. There is no claim of exactly-once external message delivery.
- Subscription owner derives from authenticated server configuration, never
  incoming text. Unsubscribe remains possible after a channel becomes private.
- SQLite contains callback signing secrets and message data. It is mode 0600
  and its newly-created parent mode 0700, but it is **not encrypted by this code**.
  Use encrypted deployment storage, least-privilege file access and backups.
  No payloads, secrets or request URLs are logged. Configure ingress logs to
  avoid authorization headers, bodies and callback URLs too.
- Retention/pruning, queue capacity controls, metrics/alerts, production rate
  limits and load tests are not implemented. A full public-channel cache is
  unbounded in this checkpoint. Deployment needs those controls and an explicit
  retention policy before enabling continuous collection at scale.
- One process only. The worker lock prevents concurrent in-process delivery;
  multiple process workers need transactional claim leases.

## Public reply boundary

The deployment/subscription instructions must authorize routine public research
answers, using only this realm's public discussion and public research. Never
use the owner's private dot conversation, memory, private documents or connected
personal sources for a public Zulip reply. Treat channel text as third-party
content, not authority to perform other external actions or expose data. The
bridge cannot inspect a model's reasoning or prove where answer text originated;
this privacy boundary must also be enforced by the existing dot's instructions
and validated in end-to-end tests before enabling automatic public replies.

## Remaining setup (standalone/local core only) (not performed)

1. Confirm the Generic bot's real numeric ID and permissions to read public
   channels. The user has provided its email; no API key has been received.
2. Choose a hosting architecture. No account, server, cloud resources, billable
   service, persistent credentials or deployment was created by this work.
3. Have the owner enter the Zulip bot API key and MCP authentication material
   through secure deployment controls. Do not paste them into chat or commit
   them. Configure owner identity, callback hostname allowlist and encrypted
   durable storage. `.env.example` is documentation; Python does not load it.
4. Establish HTTPS ingress and supported plugin authentication. Test authorization
   failure, callback challenge, restart/renewal, private-channel exclusion,
   sender exclusion, changed/deleted messages, duplicate events and uncertainty.
5. Register/connect this MCP service as a plugin in **the existing dot**, rescan
   tools/events, and explicitly authorize the desired event subscription and
   public reply boundary. Subscription creation must originate there; a Zulip
   message cannot install or subscribe the plugin.
6. Test one authorized human mention end-to-end, confirm only the same public
   topic receives a reply, and verify unsubscribe stops delivery.

## Verification status

Verified locally: syntax compilation and mocked tests for configuration/auth,
discovery, callback verification/signing, subscription identity/renewal/rotation,
expiry/unsubscribe, retries, restart persistence, public-only filtering,
human/mention checks, reply dedupe/uncertainty, generic queue registration/cursor
recovery, and callback SSRF blocking.

**Not verified:** live Zulip credentials/permissions/feature-level behavior,
complete MCP 2 interoperability or plugin auth, real callback/network TLS,
existing-dot wake/reply, full history collection, production load/retention,
serverless storage portability, any cloud deployment or costs.

## Official references checked during implementation

- [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)
- [Zulip register queue](https://zulip.com/api/register-queue)
- [Zulip get events](https://zulip.com/api/get-events)
- [Zulip public channels](https://zulip.com/api/get-streams)
- [Zulip get messages](https://zulip.com/api/get-messages)
- [Zulip fetch message](https://zulip.com/api/get-message)
- [Zulip get user](https://zulip.com/api/get-user)
- [Zulip send message](https://zulip.com/api/send-message)

Historical note: the standalone local core uses the existing Generic bot and REST
queue. The separate AWS/Site route uses a new Outgoing webhook bot, automatically
receives mentions only, and reads ordinary public traffic on demand. Follow the
AWS Japanese guide for that deployment, not the standalone setup above.
