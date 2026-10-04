"""Lambda Function URL / SQS adapter behind an owner-authenticated Site proxy."""
import base64
import hmac
import json
import os
import time

from bridge import Config, MAX_BODY, canonical
from aws.state import Store, DynamoBridge

_runtime = None
_secret_time = 0


def response(status, value, headers=None):
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store", **(headers or {})}, "body": canonical(value)}


def load_runtime():
    global _runtime, _secret_time
    if _runtime and time.time() - _secret_time < 60:
        return _runtime
    import boto3
    secret = json.loads(boto3.client("ssm").get_parameter(Name=os.environ["CONFIG_PARAMETER_NAME"], WithDecryption=True)["Parameter"]["Value"])
    settings = dict(secret)
    proxy = secret["bridge_proxy_secret"]
    if not isinstance(proxy, str) or len(proxy) < 32 or proxy in (secret["zulip_api_key"], secret["zulip_webhook_token"]):
        raise ValueError("Proxy secret must be strong and distinct")
    c = Config(realm=secret["zulip_realm"], bot_email=secret["zulip_bot_email"], bot_id=int(secret["zulip_bot_id"]),
               api_key=secret["zulip_api_key"],
               bearer=secret["bridge_proxy_secret"], owner=secret["bridge_owner"],
               callback_hosts=frozenset({"authenticated-callback.invalid"}), database=":memory:")
    sqs = boto3.client("sqs")
    def enqueue(job, delay=0):
        sqs.send_message(QueueUrl=os.environ["WORK_QUEUE_URL"], MessageBody=canonical(job), DelaySeconds=max(0, min(900, delay)))
    table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    _runtime = (settings, DynamoBridge(c, Store(table), enqueue))
    _secret_time = time.time()
    return _runtime


def authenticate(headers, settings, method=None):
    """Service-to-service auth only; ChatGPT OAuth is enforced by the Site."""
    expected = settings.get("bridge_proxy_secret", "")
    if not isinstance(expected, str) or len(expected) < 32:
        return False
    return hmac.compare_digest(headers.get("authorization", "").encode(), ("Bearer " + expected).encode())


def handler(event, context, runtime=None):
    # No request bodies, tokens, content or exception details are logged.
    if event.get("requestContext", {}).get("http", {}).get("method") == "GET" and event.get("rawPath") == "/healthz":
        return response(200, {"ok": True, "meaning": "process alive; configuration and Zulip not checked"})
    try:
        settings, bridge = runtime or load_runtime()
        if "Records" in event:
            failures = []
            for item in event["Records"]:
                try:
                    job = json.loads(item["body"])
                    if job["kind"] == "ingest":
                        bridge.ingest_message(job["message_id"])
                    elif job["kind"] == "deliver":
                        bridge.deliver(job["key"])
                    else:
                        raise ValueError("Unknown job")
                except Exception:
                    failures.append({"itemIdentifier": item["messageId"]})
            return {"batchItemFailures": failures}
        if event.get("source") == "aws.events":
            bridge.recover()
            return {"ok": True}
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        method = event.get("requestContext", {}).get("http", {}).get("method", "")
        path = event.get("rawPath", "")
        if path == "/status" and method == "GET":
            if not authenticate(headers, settings):
                return response(401, {"error": "Unauthorized"})
            own = bridge.zulip.call("GET", "users/me", {})
            identity_ok = own.get("user_id") == bridge.c.bot_id and own.get("email") == bridge.c.bot_email and own.get("is_bot") is True
            if not identity_ok:
                return response(503, {"error":"Configured bot identity mismatch"})
            channels = bridge.public_channels()
            if not channels:
                return response(503, {"configured":True,"public_read_preflight_ok":False,"error":"no_public_channels"})
            bridge.tools_call("read_discussion", {"channel_id":next(iter(channels)), "limit":1})
            return response(200, {"configured": True, "zulip_realm": bridge.c.realm,
                                  "public_read_preflight_ok": True,
                                  "subscriptions_enabled": settings.get("subscriptions_enabled", False),
                                  "webhook_enabled": settings.get("webhook_ready", False),
                                  "callback_policy": "authenticated subscription; public HTTPS only"})
        if path != "/mcp":
            return response(404, {"error": "Not found"})
        if method != "POST":
            return response(405, {"error": "POST required"}, {"allow": "POST"})
        if headers.get("origin"):
            return response(403, {"error": "Browser-origin requests disabled"})
        if headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return response(415, {"error": "JSON required"})
        raw = event.get("body", "")
        raw = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
        if not 0 < len(raw) <= MAX_BODY:
            return response(413, {"error": "Invalid body size"})
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return response(400, {"error": "Object required"})
        if not authenticate(headers, settings):
            return response(401, {"error": "Unauthorized"})
        if payload.get("method") == "events/subscribe" and not settings.get("subscriptions_enabled", False):
            return response(200, {"jsonrpc":"2.0", "id":payload.get("id"), "error":{"code":-32000, "message":"Subscriptions not enabled by operator"}})
        return response(200, bridge.rpc(payload))
    except (ValueError, TypeError, KeyError):
        if "Records" in event or event.get("source") == "aws.events":
            raise RuntimeError("Bridge worker configuration invalid") from None
        return response(400, {"error": "Invalid payload or incomplete configuration"})
    except Exception:
        # SQS must retry initialization/recovery failures instead of acknowledging data loss.
        if "Records" in event or event.get("source") == "aws.events":
            raise RuntimeError("Bridge worker unavailable") from None
        return response(503, {"error": "Bridge unavailable"})
