"""DynamoDB state. Payloads are JSON strings to preserve number types.

Outbox records survive queue-send failures. A scheduled recovery pass re-enqueues
pending records. Single Lambda reserved concurrency serializes unsubscribe/send.
"""
import hashlib
import json
import hmac
import secrets
import http.client
from urllib.parse import urlsplit
import threading
import time

from bridge import Bridge, MentionParser, RpcError, canonical, positive, text, utc, signed_headers, signing_key, EVENT, PublicHTTPS, Zulip


class Store:
    def __init__(self, table):
        self.table = table

    def get(self, key):
        item = self.table.get_item(Key={"pk": key}, ConsistentRead=True).get("Item")
        return json.loads(item["payload"]) if item else None

    def put(self, key, value, absent=False):
        args = {"Item": {"pk": key, "payload": canonical(value)}}
        if key.startswith("delivery#") and value.get("state") == "pending":
            args["Item"].update(bucket="pending", due=int(value["due"]))
        elif key.startswith("msg#") and not value.get("outbox_complete", False):
            args["Item"].update(bucket="pending", due=int(value["timestamp"]))
        if len(args["Item"]["payload"].encode()) > 350000:
            raise ValueError("State item too large")
        if absent:
            args["ConditionExpression"] = "attribute_not_exists(pk)"
        try:
            self.table.put_item(**args)
            return True
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def delete(self, key):
        self.table.delete_item(Key={"pk": key})

    def pending(self, now):
        kwargs = {"IndexName":"pending-work-index", "KeyConditionExpression":"#b = :b AND #d <= :now",
                  "ExpressionAttributeNames":{"#b":"bucket", "#d":"due"},
                  "ExpressionAttributeValues":{":b":"pending", ":now":int(now)}, "ProjectionExpression":"pk"}
        while True:
            result = self.table.query(**kwargs)
            for item in result.get("Items", []):
                value = self.get(item["pk"])
                if value is not None:
                    yield item["pk"], value
            if "LastEvaluatedKey" not in result:
                break
            kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]

    def subscriptions(self):
        for sid in self.get("state#subscriptions") or []:
            sub = self.get("sub#" + sid)
            if sub:
                yield sub

    def prefix(self, prefix):
        # Deliberately simple single-owner prototype; not intended for a large realm.
        kwargs = {"ConsistentRead": True, "FilterExpression": "begins_with(pk, :p)",
                  "ExpressionAttributeValues": {":p": prefix}}
        while True:
            result = self.table.scan(**kwargs)
            for item in result.get("Items", []):
                yield item["pk"], json.loads(item["payload"])
            if "LastEvaluatedKey" not in result:
                break
            kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]


class AuthenticatedCallbackHTTPS:
    """Destination comes only from an authenticated subscription, never webhook input."""
    def request(self, method, url, body, headers):
        host = urlsplit(url).hostname
        if not host:
            raise ValueError("Invalid callback URL")
        return PublicHTTPS({host.lower()}).request(method, url, body, headers)


class DynamoBridge(Bridge):
    def __init__(self, config, store, enqueue, callbacks=None, zulip=None, now=time.time):
        self.c = config.validate()
        self.now, self.store, self.enqueue = now, store, enqueue
        self.callbacks = callbacks or AuthenticatedCallbackHTTPS()
        self.zulip = zulip or Zulip(config)
        self.lock = threading.RLock()
        self.delivery_lock = threading.Lock()

    def identity(self, params, live=True):
        if params.get("name") != EVENT:
            raise ValueError("Unknown event")
        args = self.filters(params.get("arguments"), live=live)
        delivery = params.get("delivery", {})
        if delivery.get("mode") != "webhook":
            raise ValueError("Only webhook delivery supported")
        url = text(delivery.get("url"), 4096)
        host = urlsplit(url).hostname
        if not host:
            raise ValueError("Invalid callback URL")
        PublicHTTPS({host.lower()}).validate_url(url)
        digest = hashlib.sha256(canonical([self.c.owner, url, EVENT, args]).encode()).hexdigest()
        return "sub_" + digest, args, url

    def subscribe(self, params):
        sid, args, url = self.identity(params)
        secret = params["delivery"].get("secret")
        signing_key(secret)
        ttl = params.get("ttlMs", 86400000)
        ttl = 86400000 if ttl is None else ttl
        if type(ttl) is not int or ttl <= 0 or params.get("cursor") is not None:
            raise ValueError("Invalid TTL or unsupported replay cursor")
        old = self.store.get("sub#" + sid)
        expires = self.now() + min(ttl / 1000, 86400)
        if not old or old["verified"] + 300 < self.now() or old["secret"] != secret:
            challenge = secrets.token_urlsafe(32)
            body = canonical({"type": "verification", "challenge": challenge}).encode()
            try:
                status, raw = self.callbacks.request("POST", url, body, signed_headers({"id":sid,"secret":secret}, body, "verify_" + secrets.token_hex(16), self.now()))
                echoed = json.loads(raw).get("challenge")
                if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed.encode(), challenge.encode()):
                    raise ValueError("challenge")
            except (OSError, ValueError, http.client.HTTPException):
                raise RpcError(-32015, "Callback verification failed", {"reason":"challenge_failed"}) from None
            verified = self.now()
        else:
            verified = old["verified"]
        rotating = old and old["secret"] != secret
        sub = {"id":sid, "owner":self.c.owner, "args":canonical(args), "url":url, "secret":secret,
               "expires":expires, "verified":verified,
               "old_secret": old["secret"] if rotating else (old.get("old_secret") if old else None),
               "rotate_until": self.now()+300 if rotating else (old.get("rotate_until",0) if old else 0),
               "generation": old["generation"] if old else secrets.token_hex(16)}
        self.store.put("sub#" + sid, sub)
        registry = self.store.get("state#subscriptions") or []
        if sid not in registry:
            self.store.put("state#subscriptions", registry + [sid])
        return {"id":sid, "refreshBefore":utc(expires), "cursor":None, "truncated":False}

    def unsubscribe(self, params):
        sid, _, _ = self.identity(params, live=False)
        # The deleted generation can never deliver again, including after renewal.
        self.store.delete("sub#" + sid)
        registry = self.store.get("state#subscriptions") or []
        self.store.put("state#subscriptions", [s for s in registry if s != sid])
        return {}

    def ingest_message(self, mid):
        source = self.fetch_public_message(positive(mid))
        if source.get("sender_id") == self.c.bot_id:
            return
        rendered = self.zulip.call("GET", "messages/" + str(mid), {"apply_markdown": True})["message"]
        parser = MentionParser(self.c.bot_id)
        parser.feed(rendered.get("content", ""))
        if not parser.mentioned:
            return
        m = {"message_id": mid, "sender_id": positive(source.get("sender_id")),
             "stream_id": positive(source.get("stream_id")), "topic": text(source.get("subject", ""), 1000),
             "text": text(source.get("content"), 50000), "kind": "mention",
             "url": self.c.realm.rstrip("/") + "/#narrow/near/" + str(mid)}
        if not self.eligible(m):
            return
        stamp = source.get("timestamp")
        if type(stamp) not in (int, float) or not 0 < stamp <= self.now() + 300:
            raise ValueError("Invalid occurrence timestamp")
        # Save target subscriptions at first receipt. Retries never replay to new subscribers.
        record = self.store.get("msg#" + str(mid))
        if not record:
            subscriptions = [{"id":s["id"], "generation":s["generation"]} for s in self.store.subscriptions() if s["owner"] == self.c.owner and s["expires"] > self.now() and json.loads(s["args"]) == {"scope":"all_public"}]
            record = {"data": m, "timestamp": stamp, "subscriptions": subscriptions, "outbox_complete":False}
            self.store.put("msg#" + str(mid), record, absent=True)
            record = self.store.get("msg#" + str(mid))
        outgoing = {"eventId": "zulip_" + str(mid), "name": EVENT, "timestamp": utc(record["timestamp"]), "data": record["data"], "cursor": None}
        for target in record["subscriptions"]:
            sid = target["id"]
            key = "delivery#" + sid + "#" + str(mid)
            self.store.put(key, {"sub_id": sid, "generation": target["generation"], "body": canonical(outgoing), "event_id": outgoing["eventId"], "attempts": 0, "due": self.now(), "state": "pending"}, absent=True)
            self.enqueue({"kind": "deliver", "key": key})
        record["outbox_complete"] = True
        self.store.put("msg#" + str(mid), record)

    def deliver(self, key):
        row = self.store.get(key)
        if not row or row["state"] != "pending":
            return
        if row["due"] > self.now():
            self.enqueue({"kind": "deliver", "key": key}, min(900, int(row["due"] - self.now()) + 1))
            return
        sub = self.store.get("sub#" + row["sub_id"])
        valid = sub and sub["owner"] == self.c.owner and sub["expires"] > self.now() and sub["generation"] == row["generation"]
        if valid:
            try:
                self.filters(json.loads(sub["args"]))
                valid = self.live_trigger(json.loads(row["body"])["data"])
            except ValueError:
                valid = False
        if not valid:
            row["state"] = "revoked"
        else:
            body = row["body"].encode()
            try:
                status, _ = self.callbacks.request("POST", sub["url"], body, signed_headers(sub, body, row["event_id"], self.now()))
            except Exception:
                status = 503
            row["attempts"] += 1
            transient = status in (408, 429) or status >= 500
            row["state"] = "delivered" if 200 <= status < 300 else ("pending" if transient and row["attempts"] < 8 else "failed")
            row["due"] = self.now() + min(900, 2 ** row["attempts"])
        self.store.put(key, row)
        if row["state"] == "pending":
            self.enqueue({"kind": "deliver", "key": key}, max(0, int(row["due"] - self.now())))

    def recover(self):
        # Sparse index holds only unfinished ingest and pending deliveries.
        # GSI visibility is eventual; the next scheduled pass repairs any lag.
        for key, record in self.store.pending(self.now()):
            if key.startswith("msg#") and not record.get("outbox_complete", False):
                for target in record["subscriptions"]:
                    sid = target["id"]
                    delivery = "delivery#" + sid + "#" + str(record["data"]["message_id"])
                    outgoing = {"eventId":"zulip_" + str(record["data"]["message_id"]), "name":EVENT,
                                "timestamp":utc(record["timestamp"]), "data":record["data"], "cursor":None}
                    self.store.put(delivery, {"sub_id":sid,"generation":target["generation"],"body":canonical(outgoing),
                                             "event_id":outgoing["eventId"],"attempts":0,"due":self.now(),"state":"pending"}, absent=True)
                    self.enqueue({"kind":"deliver", "key":delivery})
                record["outbox_complete"] = True
                self.store.put(key, record)
            elif key.startswith("delivery#") and record["state"] == "pending" and record["due"] <= self.now():
                self.enqueue({"kind":"deliver", "key":key})

    def tools_call(self, name, args):
        if name != "reply_to_message":
            return super().tools_call(name, args)
        if not isinstance(args, dict) or set(args) != {"message_id", "content", "request_id"}:
            raise ValueError("Invalid tool arguments")
        mid = positive(args["message_id"])
        content, request_id = text(args["content"], 10000), text(args["request_id"], 128)
        if not content.strip() or not request_id.strip():
            raise ValueError("Content and request_id required")
        fingerprint = hashlib.sha256(canonical([mid, content]).encode()).hexdigest()
        record = self.store.get("msg#" + str(mid))
        if not record or not self.live_trigger(record["data"]):
            raise ValueError("Unknown trigger")
        m = record["data"]
        latest = self.fetch_public_message(mid)
        if latest["stream_id"] != m["stream_id"] or latest["subject"] != m["topic"]:
            raise ValueError("Original message moved")
        key = "reply#" + request_id
        previous = self.store.get(key)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("request_id conflict")
            if previous["state"] != "sent":
                raise RpcError(-32000, "Reply outcome uncertain; inspect Zulip before another send")
            return previous["result"]
        if not self.store.put(key, {"fingerprint": fingerprint, "state": "uncertain"}, absent=True):
            raise RpcError(-32000, "Reply request already claimed")
        result = self.zulip.call("POST", "messages", {"content": content, "type": "stream", "to": m["stream_id"], "topic": m["topic"]})
        self.store.put(key, {"fingerprint": fingerprint, "state": "sent", "result": result})
        return result
