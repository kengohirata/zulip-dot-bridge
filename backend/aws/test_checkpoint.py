"""Offline AWS adapter checks. No actual AWS or Zulip calls."""
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from aws.app import handler, authenticate
from aws.ingress import handler as ingress_handler
from aws.state import Store

class FakeTable:
    def __init__(self): self.items = {}
    def get_item(self, Key, **kw): return {"Item": self.items[Key["pk"]]} if Key["pk"] in self.items else {}
    def put_item(self, Item, **kw):
        if kw and Item["pk"] in self.items:
            error = RuntimeError()
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise error
        self.items[Item["pk"]] = Item
    def delete_item(self, Key): self.items.pop(Key["pk"], None)
    def query(self, **kw):
        return {"Items":[{"pk":k} for k,v in list(self.items.items()) if v.get("bucket")==kw["ExpressionAttributeValues"][":b"] and v["due"]<=kw["ExpressionAttributeValues"][":now"]]}
    def scan(self, **kw):
        return {"Items": [v for k,v in self.items.items() if k.startswith(kw["ExpressionAttributeValues"][":p"])]}

class CheckpointTests(unittest.TestCase):
    def request(self, path, data, method="POST"):
        return {"rawPath": path, "headers": {"content-type": "application/json"}, "requestContext": {"http": {"method": method}}, "body": json.dumps(data)}
    def test_health_does_not_require_credentials(self):
        self.assertEqual(handler(self.request("/healthz", {}, "GET"), None)["statusCode"], 200)
    def test_store_idempotence(self):
        store = Store(FakeTable())
        self.assertTrue(store.put("item#1", {"id": 1, "stamp": 1.5}, absent=True))
        self.assertFalse(store.put("item#1", {"id": 2}, absent=True))
        self.assertEqual(store.get("item#1"), {"id": 1, "stamp": 1.5})
        self.assertEqual(len(list(store.prefix("item#"))), 1)
        store.delete("item#1")
        self.assertIsNone(store.get("item#1"))
    def test_store_size_limit(self):
        with self.assertRaises(ValueError): Store(FakeTable()).put("x", "a" * 350001)
    def test_proxy_fails_closed(self):
        result = handler(self.request("/mcp", {"jsonrpc":"2.0", "id":1,"method":"tools/list"}), None, ({"resource_url":"https://example.com/mcp"}, SimpleNamespace()))
        self.assertEqual(result["statusCode"], 401)
    def test_webhook_disabled(self):
        result = ingress_handler(self.request("/zulip/events", {}), None, ({}, lambda job:None))
        self.assertEqual(result["statusCode"], 503)
    def test_webhook_queues_only_identifier(self):
        queued = []
        bridge = SimpleNamespace(enqueue=lambda job: queued.append(job))
        settings = {"webhook_ready":True, "zulip_webhook_token":"t"*32}
        payload = {"token":"t"*32,"trigger":"mention","message":{"id":5,"type":"stream","content":"untrusted"}}
        result = ingress_handler(self.request("/zulip/events", payload), None, (settings, bridge.enqueue))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(queued, [{"kind":"ingest", "message_id":5}])
    def test_webhook_bad_token(self):
        result = ingress_handler(self.request("/zulip/events", {"token":"x"}), None, ({"webhook_ready":True,"zulip_webhook_token":"t"*32}, lambda job:None))
        self.assertEqual(result["statusCode"], 401)
    def test_proxy_secret_separation(self):
        settings = {"bridge_proxy_secret":"s"*43, "zulip_webhook_token":"z"*43}
        self.assertTrue(authenticate({"authorization":"Bearer " + "s"*43}, settings))
        self.assertFalse(authenticate({"authorization":"Bearer " + "z"*43}, settings))
        self.assertFalse(authenticate({"authorization":"s"*43}, settings))
    def test_proxy_success(self):
        event = self.request("/mcp", {"jsonrpc":"2.0","id":1,"method":"tools/list"})
        event["headers"]["authorization"] = "Bearer " + "s"*43
        result = handler(event, None, ({"bridge_proxy_secret":"s"*43},SimpleNamespace(rpc=lambda p: {"jsonrpc":"2.0","id":p["id"],"result":{}})))
        self.assertEqual(result["statusCode"],200)
    def test_subscription_gate(self):
        event = self.request("/mcp", {"jsonrpc":"2.0","id":1,"method":"events/subscribe"})
        event["headers"]["authorization"] = "Bearer " + "s"*43
        result = handler(event, None, ({"bridge_proxy_secret":"s"*43},SimpleNamespace()))
        self.assertEqual(json.loads(result["body"])["error"]["code"],-32000)
    def test_origin_and_bad_body_rejected(self):
        event = self.request("/mcp", {})
        event["headers"]["origin"] = "https://evil.example"
        self.assertEqual(handler(event,None,({},SimpleNamespace()))["statusCode"],403)
        del event["headers"]["origin"]
        event["body"] = "not json"
        self.assertEqual(handler(event,None,({},SimpleNamespace()))["statusCode"],400)
    def test_worker_configuration_error_is_not_acknowledged(self):
        with patch("aws.app.load_runtime", side_effect=ValueError("bad config")):
            with self.assertRaises(RuntimeError):
                handler({"Records": [{"messageId":"one", "body":"{}"}]}, None)
    def test_status_preflight(self):
        from aws.test_state import fixtures
        bridge = SimpleNamespace(c=fixtures.config(), zulip=fixtures.ZulipMock(), public_channels=lambda:{7:{}}, tools_call=lambda name,args:{"messages":[]})
        event = self.request("/status", {}, "GET")
        event["headers"]["authorization"] = "Bearer " + "s"*43
        result = handler(event,None,({"bridge_proxy_secret":"s"*43},bridge))
        payload = json.loads(result["body"])
        self.assertEqual(result["statusCode"],200)
        self.assertTrue(payload["public_read_preflight_ok"])
        self.assertFalse(payload["subscriptions_enabled"])
        self.assertNotIn("secret",result["body"])
    def test_ingress_cannot_serve_mcp_or_worker(self):
        self.assertEqual(ingress_handler(self.request("/mcp",{}),None)["statusCode"],404)
        self.assertEqual(ingress_handler({"Records":[{"body":"{}"}]},None)["statusCode"],404)
        self.assertEqual(handler(self.request("/zulip/events",{}),None,({},SimpleNamespace()))["statusCode"],404)
    def test_ingress_ignores_dm_and_queue_failure_is_not_ack(self):
        def fail(job): raise OSError("queue failed")
        settings={"webhook_ready":True,"zulip_webhook_token":"t"*32}
        dm={"token":"t"*32,"trigger":"direct_message","message":{"id":5,"type":"private"}}
        self.assertEqual(ingress_handler(self.request("/zulip/events",dm),None,(settings,fail))["statusCode"],200)
        mention={"token":"t"*32,"trigger":"mention","message":{"id":5,"type":"stream"}}
        self.assertEqual(ingress_handler(self.request("/zulip/events",mention),None,(settings,fail))["statusCode"],503)
    def test_worker_partial_failure(self):
        def ingest(mid): raise OSError("network")
        result = handler({"Records":[{"messageId":"one","body":'{"kind":"ingest","message_id":5}'}]}, None, ({},SimpleNamespace(ingest_message=ingest)))
        self.assertEqual(result, {"batchItemFailures":[{"itemIdentifier":"one"}]})

if __name__ == "__main__": unittest.main()
