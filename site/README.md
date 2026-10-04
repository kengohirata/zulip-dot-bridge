> Optional AWS adapter: this gateway deliberately accepts only Lambda Function URLs.
> The primary cloud-independent server is `../backend/portable.py`; see [self-hosting](../docs/SELF_HOSTING.md).
> A normal hosted MCP endpoint needs its own compatible authentication/event client connection. Do not remove the gateway's URL restrictions to make it accept arbitrary hosts: the runtime does not expose the connection-level DNS pinning required by this implementation's security policy.

# Private Zulip gateway for dot

This existing private Site uses Sites-managed OAuth and its original plugin identity. It performs no model inference and has no OpenAI API key.

## Manual setup

Open `/setup` as the Site owner. Enter the Lambda Function URL, expected Zulip realm, and the separate AWS proxy secret. Explicitly authorize persistent use and submit. No upstream request runs before submission. The backend must pass authenticated `/status`, expected-realm matching, bot identity, and public-channel-read checks before configuration is saved.

The secret is stored server-side in private D1 and bound to the first authenticated owner; application routes never return it. This uses platform storage protection, not additional application-level encryption. Keep the Zulip bot API key in AWS only. Do not put credentials into chat, URL parameters, source, logs, or examples.

## Authorization and proxy

The hosting access policy must remain owner-private. Every data-bearing RPC requires a trusted Sites user ID matching the stored owner. New collaborators must not be granted backend access implicitly. Static discovery contains no source data.

Only HTTPS Lambda Function URLs matching `<id>.lambda-url.<region>.on.aws` are accepted. No paths, credentials, query parameters, or fragments; no redirects. Outbound Authorization uses the separate proxy secret. Caller identity and authorization headers are never forwarded. Response size and time are bounded; configured secrets are redacted if accidentally reflected upstream.

The status tool remains available before configuration. After configuration, allowlisted backend tools are exposed: list_public_channels, read_discussion, search_discussion, reply_to_message. Each source operation rechecks authenticated public-read readiness. Subscriptions require public-read readiness and separate subscription enablement. Webhook intake can stay disabled while a callback is registered; delivery is ready only after intake is enabled. Unsubscribe can still run when source-read readiness is lost.

`zulip.mention` subscriptions use exactly `{ "scope": "all_public" }`. Bot identity and realm are pinned in AWS, never overridden by event arguments. No subscription is created by this Site setup flow. AWS implements callback verification, public-address restrictions, event persistence, signing and delivery.

## Local checks

- `node scripts/test-probe.mjs`: unconfigured discovery, MCP2 and legacy envelopes, identity gates.
- `node scripts/test-bridge.mjs`: real in-memory SQLite migration, ownership, setup consent and CSRF, URL restrictions, mocked preflight/proxy, secret non-disclosure and subscription gates.
- `npm run db:generate`: append Drizzle migrations after schema edits. Applied migration files are immutable.

Tests use local fixtures and fake upstream responses only. They do not prove AWS deployment or actual Zulip access. No production config is seeded.

## Protocol

MCP2 responses include resultType, server identity metadata, and catalog cache hints. Legacy initialize/tool envelopes are preserved. Diagnostic logs include only allowlisted method names and protocol/authentication booleans, never request arguments, user IDs or secrets.

References:
- https://developers.openai.com/plugins/build/mcp-events
- https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx
- https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html
