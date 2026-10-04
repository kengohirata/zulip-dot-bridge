"""Fast, independently-scaled Zulip ingress. No Zulip API or DynamoDB calls."""
import base64
import hmac
import json
import os
import time
from bridge import MAX_BODY, canonical
def response(status, value):
    return {"statusCode":status,"headers":{"content-type":"application/json","cache-control":"no-store"},"body":canonical(value)}

_runtime = None
_loaded = 0


def load_runtime():
    global _runtime, _loaded
    if _runtime and time.time() - _loaded < 60:
        return _runtime
    import boto3
    from botocore.config import Config
    options = Config(connect_timeout=1, read_timeout=2, retries={"total_max_attempts":1})
    settings = json.loads(boto3.client("ssm", config=options).get_parameter(
        Name=os.environ["CONFIG_PARAMETER_NAME"], WithDecryption=True)["Parameter"]["Value"])
    sqs = boto3.client("sqs", config=options)
    def enqueue(job):
        sqs.send_message(QueueUrl=os.environ["WORK_QUEUE_URL"], MessageBody=canonical(job))
    _runtime, _loaded = (settings, enqueue), time.time()
    return _runtime


def handler(event, context, runtime=None):
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method")
        if event.get("rawPath") != "/zulip/events":
            return response(404, {"error":"Not found"})
        if method != "POST":
            return response(405, {"error":"POST required"})
        headers = {k.lower():v for k,v in event.get("headers",{}).items()}
        if headers.get("origin"):
            return response(403, {"error":"Browser-origin requests disabled"})
        if headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return response(415, {"error":"JSON required"})
        raw = event.get("body", "")
        raw = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
        if not 0 < len(raw) <= MAX_BODY:
            return response(413, {"error":"Invalid body size"})
        payload = json.loads(raw)
        if not isinstance(payload,dict):
            raise ValueError("Object required")
        settings, enqueue = runtime or load_runtime()
        if not settings.get("webhook_ready", False):
            return response(503, {"error":"Webhook not enabled"})
        token, expected = payload.get("token", ""), settings.get("zulip_webhook_token", "")
        if not isinstance(token,str) or not isinstance(expected,str) or len(expected)<24 or not hmac.compare_digest(token.encode(),expected.encode()):
            return response(401, {"error":"Unauthorized"})
        message = payload.get("message", {})
        if payload.get("trigger") != "mention" or message.get("type") != "stream":
            return response(200, {"response_not_required":True})
        mid = message.get("id")
        if type(mid) is not int or mid <= 0:
            raise ValueError("Invalid message id")
        enqueue({"kind":"ingest", "message_id":mid})
        # Acknowledge only after SQS confirms persistence.
        return response(200, {"response_not_required":True})
    except (ValueError, TypeError, KeyError):
        return response(400, {"error":"Invalid payload or configuration"})
    except Exception:
        return response(503, {"error":"Ingress unavailable"})
