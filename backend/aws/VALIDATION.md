# Validation record — 2026-10-03

## Passed locally

- 27 portable core unit tests, all external calls mocked.
- 27 AWS adapter/state unit tests, fake DynamoDB and no external calls.
- Python compilation of `bridge.py` and `aws/`.
- YAML parsing and structural checks: reserved concurrency 1, SQS visibility >= 6 × Lambda timeout, processing disabled by default, no Secrets Manager resource, separate fast ingress function/URL, DynamoDB provisioned 1/1 plus sparse pending index 1/1 with AWS-owned encryption.

Covered: proxy authentication and distinct webhook token, webhook-disabled startup, scope gate, status preflight, no browser-origin requests, malformed input, queue failure handling, configuration errors not acknowledged as SQS success, isolated ingress rejects MCP/worker events, enqueue failure not acknowledged, DynamoDB conditional reply claims before send, uncertain-send refusal, successful reply deduplication, callback challenge, generation fencing after unsubscribe/re-subscribe, delivery deduplication, transient callback retry, durable-outbox recovery, edited/private content checks (core), archived public reads allowed but replies blocked, and completed items removed from pending index.

## Not run

- `sam validate --lint`: SAM CLI is unavailable in this build environment. Run it in AWS CloudShell before deployment; it is a mandatory setup gate.
- CloudFormation deployment or real IAM policy execution.
- Real SSM SecureString decrypt, DynamoDB, SQS/DLQ, EventBridge, Function URL.
- Outgoing-webhook Zulip API with real bot credentials.
- End-to-end live ChatGPT Events callback/subscription and dot reply.
- Fault injection against real AWS, load tests, billing/free-tier verification.

Local tests do not establish production readiness. The Japanese guide enables source intake only after authenticated Site preflight and verified event subscription.

No AWS resources or external credentials were created or accessed by the implementation work.
