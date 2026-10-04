# Import validation — 2026-10-04

## Passed on the source checkpoint

Environment: Python 3.12.14 and Node.js 24.19.0.

- 27 portable Python core unit tests, with external service calls mocked.
- 27 AWS adapter/state unit tests, using fake DynamoDB and mocked service calls.
- 31 portable-server tests: real loopback HTTP intake/MCP and callback receiver, HMAC signature checks, duplicate and concurrent intake/reply handling, restart persistence, ID binding, auth/gates, public-only exclusions, queue capacity/write failures, bounded retries, lease recovery, unsubscribe races, and uncertain replies. Zulip was mocked.
- Independent real-loopback HTTP smoke test of the new cloud-independent adapter: authenticated status, subscription challenge, durable ID-only duplicate-safe intake, canonical message checks, signed callback, and idempotent same-topic reply. External Zulip and callback endpoints were mocked.
- Site discovery/protocol/identity test script.
- Site bridge test script, using real in-memory SQLite migrations and mocked HTTP responses.
- Python source syntax parsing and Node worker syntax check.
- Site build in an isolated temporary directory with a non-deployable placeholder hosting manifest.
- Built artifact ESM import and exported `fetch` handler validation.
- Source import hash comparison (original hashes and deployment provenance are kept out of this public snapshot). The original shared core and AWS/Site implementation remain unchanged; a new portable adapter and its tests were added. Documentation and configuration examples were updated, original realm/bot addresses were generalized, and the real hosting configuration was replaced by an example.
- Allowlisted export inventory; omitted credentials, caches, original Git metadata, dependencies, build output, databases, message logs, private research, and deployment-specific configuration.

These checks can be rerun with `bash scripts/check.sh`.

## Verification limits

- SAM CLI and cfn-lint were unavailable, so `sam validate --lint` was not run. It remains required before a real AWS deployment.
- No real AWS, Zulip, D1, OAuth, or MCP Events callback was contacted in these checks.
- No cloud infrastructure changes, subscription creation, bot replies, or deployment were performed.
- The imported documentation records an earlier implementation checkpoint; it is not evidence of current live deployment status.
- File inspection and pattern scanning reduce accidental exposure risk but are not a guarantee against all secrets. Review staged changes before every future push.
- This snapshot does not include a third-party security audit, load test, dependency vulnerability audit, or live end-to-end production test.

## Preserved known limitations

See `backend/README.md`, `backend/aws/README.ja.md`, and `backend/aws/VALIDATION.md` for implementation-specific constraints. In particular, history backfill and retention are not implemented, the AWS bridge requires reserved concurrency 1, source intake is gated by configuration, and uncertain external sends are not automatically repeated.

## Deployment examples

The nginx and systemd files are reviewable configuration skeletons. They were not installed or exercised on a live host. The Site gateway remains Lambda-only; generic-host TLS/OAuth/MCP-client integration is a separate deployment requirement.
