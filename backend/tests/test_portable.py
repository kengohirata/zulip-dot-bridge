"""No credentials or public networking: HTTP intake and callbacks use loopback."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge import Bridge, PROTOCOL, RpcError, canonical, signing_key
from portable import (ACK, CATALOG_METHODS, LEGACY_VERSIONS, IngressQueue, PortableApp,
                      Settings, handler_for, single_instance, strict_json)
from test_bridge import Callback, SECRET, ZulipMock, config

TOKEN = "webhook-test-only-distinct-xxxxxxxxxx"


class RunningHTTP:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    @property
    def address(self):
        return self.server.server_address

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class LoopbackCallback:
    """Test-only transport: a configured HTTPS URL routes to a real local fixture.

    Production still uses DNS-pinned PublicHTTPS and cannot reach loopback.
    """
    def __init__(self):
        self.calls = []
        self.arrived = threading.Event()
        owner = self
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                owner.calls.append((body, dict(self.headers)))
                if body.get("type") == "verification":
                    value = {"challenge": body["challenge"]}
                else:
                    value = {}
                    owner.arrived.set()
                encoded = canonical(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
        self.http = RunningHTTP(Receiver)

    def request(self, method, url, body, headers):
        connection = http.client.HTTPConnection(*self.http.address, timeout=2)
        try:
            connection.request(method, "/events", body, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def close(self):
        self.http.close()


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [1000]
        self.z = ZulipMock()
        self.cb = Callback()
        self.c = config(self.tmp.name + "/state.sqlite3")
        self.settings = Settings(TOKEN, True, True)
        self.app = self.make_app()
        self.http = RunningHTTP(handler_for(self.app))
        self.params = {"name": "zulip.mention", "arguments": {"scope": "all_public"},
                       "delivery": {"mode": "webhook", "url": "https://callback.example/events", "secret": SECRET}}

    def make_app(self, background=False):
        bridge = Bridge(self.c, self.cb, self.z, now=lambda: self.clock[0])
        app = PortableApp(bridge, self.settings)
        app.start(background=background)
        return app

    def tearDown(self):
        self.http.close()
        self.app.close()
        self.tmp.cleanup()

    def restart(self, background=False):
        self.http.close()
        self.app.close()
        self.app = self.make_app(background)
        self.http = RunningHTTP(handler_for(self.app))

    def request(self, path="/mcp", value=None, method="POST", auth=True, headers=None, raw=None):
        headers = dict(headers or {})
        headers.setdefault("Content-Type", "application/json")
        if auth:
            headers.setdefault("Authorization", "Bearer " + self.c.bearer)
        if raw is None and value is not None:
            raw = canonical(value).encode()
        connection = http.client.HTTPConnection(*self.http.address, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else None
        finally:
            connection.close()

    def rpc(self, method, params=None, **kwargs):
        return self.request(value={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kwargs)

    def payload(self, **overrides):
        return {"token": TOKEN, "trigger": "mention", "message": copy.deepcopy(self.z.message), **overrides}

    def intake(self, value=None):
        return self.request("/zulip/events", value or self.payload(), auth=False)

    def subscribe(self):
        status, response = self.rpc("events/subscribe", self.params)
        self.assertEqual(status, 200)
        self.assertNotIn("error", response)
        return response["result"]["id"]

    def ingest(self):
        self.subscribe()
        self.assertEqual(self.intake(), (200, ACK))
        self.assertTrue(self.app.ingest_one())

    def row(self):
        return dict(self.app.queue.db.execute("SELECT * FROM webhook_jobs").fetchone())

    def test_real_loopback_webhook_callback_and_mcp_reply(self):
        callback = LoopbackCallback()
        self.addCleanup(callback.close)
        self.cb = callback
        self.restart(background=True)
        self.subscribe()
        status, response = self.intake()
        self.assertEqual((status, response), (200, ACK))
        self.assertTrue(callback.arrived.wait(3), "signed callback not received")
        event, headers = callback.calls[-1]
        self.assertEqual(event["data"]["text"], self.z.message["content"])
        self.assertEqual(event["eventId"], "zulip_123")
        raw = (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode() + canonical(event).encode()
        expected = "v1," + base64.b64encode(hmac.digest(signing_key(SECRET), raw, hashlib.sha256)).decode()
        self.assertTrue(hmac.compare_digest(expected, headers["webhook-signature"]))
        args = {"name": "reply_to_message", "arguments": {"message_id": 123, "content": "Answer", "request_id": "route-reply"}}
        result = self.rpc("tools/call", args, headers={"MCP-Protocol-Version": PROTOCOL})
        self.assertEqual(result[0], 200)
        self.assertFalse(result[1]["result"]["isError"])
        self.assertEqual(result[1]["result"]["resultType"], "complete")
        self.assertEqual(self.rpc("tools/call", args)[1]["result"]["content"], result[1]["result"]["content"])
        sends = [c for c in self.z.calls if c[:2] == ("POST", "messages")]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][2], {"type": "stream", "to": 7, "topic": "Research", "content": "Answer"})
        self.assertFalse(any(c[1] in ("register", "events") for c in self.z.calls))

    def test_ack_commits_id_only_without_remote_io(self):
        self.z.calls.clear()
        untrusted = self.payload()
        untrusted["message"]["content"] = "private-forged-body-not-to-persist"
        self.assertEqual(self.intake(untrusted), (200, ACK))
        self.assertEqual(self.z.calls, [])
        other = sqlite3.connect(self.c.database)
        try:
            row = other.execute("SELECT * FROM webhook_jobs").fetchone()
            self.assertEqual(row[0], 123)
            dump = "\n".join(other.iterdump())
            self.assertNotIn(TOKEN, dump)
            self.assertNotIn(untrusted["message"]["content"], dump)
        finally:
            other.close()
        self.assertEqual(self.app.b.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_forged_payload_fields_do_not_override_canonical_data(self):
        self.subscribe()
        forged = self.payload()
        forged["message"].update(sender_id=123456, stream_id=900, subject="forged", content="forged")
        self.assertEqual(self.intake(forged), (200, ACK))
        self.app.ingest_one()
        stored = json.loads(self.app.b.db.execute("SELECT data FROM messages").fetchone()[0])
        self.assertEqual(stored["sender_id"], 8)
        self.assertEqual(stored["stream_id"], 7)
        self.assertEqual(stored["topic"], "Research")

    def test_auth_and_origin_fail_closed(self):
        for path, method in (("/mcp", "POST"), ("/status", "GET")):
            self.assertEqual(self.request(path, {}, method, auth=False)[0], 401)
            self.assertEqual(self.request(path, {}, method, headers={"Authorization": "Bearer wrong"})[0], 401)
            self.assertEqual(self.request(path, {}, method, headers={"Origin": "https://browser.example"})[0], 403)
        self.assertEqual(self.intake(self.payload(token="wrong"))[0], 401)
        self.assertEqual(self.request("/zulip/events", self.payload(), headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.app.queue.counts(), {})

    def test_duplicate_headers_and_request_smuggling_rejected(self):
        for path, extra, expected in (("/mcp", "Authorization: Bearer other\r\n", 401),
                                      ("/zulip/events", "Content-Length: 2\r\n", 400),
                                      ("/zulip/events", "Transfer-Encoding: chunked\r\n", 415)):
            body = canonical(self.payload()).encode()
            request = ("POST " + path + " HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer " + self.c.bearer +
                       "\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)) + "\r\n" + extra + "Connection: close\r\n\r\n").encode() + body
            with socket.create_connection(self.http.address, timeout=2) as sock:
                sock.sendall(request)
                self.assertIn((" " + str(expected) + " ").encode(), sock.recv(4096).split(b"\r\n")[0])
        self.assertEqual(self.app.queue.counts(), {})

    def test_malformed_json_and_message_ids(self):
        for raw in (b"[]", b"{", b'{"token":1,"token":2}', b'{"x":NaN}', b'{"x":1e309}'):
            self.assertEqual(self.request("/zulip/events", raw=raw, auth=False)[0], 400)
        for message in (None, [], "bad"):
            self.assertEqual(self.intake(self.payload(message=message))[0], 400)
        for mid in (0, -1, True, "123", 1.2, 2 ** 100):
            message = dict(self.z.message, id=mid)
            self.assertEqual(self.intake(self.payload(message=message))[0], 400)
        self.assertEqual(self.request("/zulip/events", self.payload(), headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/zulip/events", raw=b"x" * 262145)[0], 413)

    def test_dm_and_nonmention_webhooks_are_never_queued(self):
        for payload in (self.payload(trigger="direct_message"), self.payload(message={"type": "private", "id": 123}), self.payload(trigger="unknown")):
            self.assertEqual(self.intake(payload), (200, ACK))
        self.assertEqual(self.app.queue.counts(), {})
        self.assertFalse(self.app.ingest_one())

    def test_canonical_private_dm_bot_nonmention_archived_inactive_excluded(self):
        scenarios = (("public", False), ("mention", False), ("is_bot", True), ("active", False), ("archived", True), ("sender_id", 99), ("type", "private"))
        for field, value in scenarios:
            with self.subTest(field=field):
                original = copy.deepcopy(self.z.__dict__)
                if field in ("sender_id", "type"):
                    self.z.message[field] = value
                else:
                    setattr(self.z, field, value)
                self.app.queue.enqueue(123)
                self.assertTrue(self.app.ingest_one())
                self.assertEqual(self.row()["state"], "done")
                self.assertEqual(self.app.b.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
                self.assertEqual(self.app.b.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
                self.z.__dict__ = original
                with self.app.queue.db:
                    self.app.queue.db.execute("DELETE FROM webhook_jobs")

    def test_privacy_or_mention_revocation_blocks_callback_and_reply(self):
        self.ingest()
        self.z.public = False
        self.assertTrue(self.app.deliver_one())
        self.assertEqual(len(self.cb.calls), 1)
        args = {"name": "reply_to_message", "arguments": {"message_id": 123, "content": "No", "request_id": "revoked"}}
        self.assertIn("error", self.rpc("tools/call", args)[1])
        self.z.public = True
        self.z.mention = False
        self.assertIn("error", self.rpc("tools/call", args)[1])

    def test_failed_durable_enqueue_never_acknowledged(self):
        with patch.object(self.app.queue, "enqueue", side_effect=sqlite3.OperationalError("sensitive-error")):
            status, body = self.intake()
        self.assertEqual(status, 503)
        self.assertNotIn("sensitive", canonical(body))
        self.assertEqual(self.app.queue.counts(), {})

    def test_locked_database_returns_backpressure_not_false_success(self):
        blocker = sqlite3.connect(self.c.database)
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            self.assertEqual(self.intake()[0], 503)
            self.assertLess(time.monotonic() - started, 2.5)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self.app.queue.counts(), {})
        self.assertEqual(self.intake(), (200, ACK))

    def test_duplicate_intake_races_and_capacity_are_atomic(self):
        self.app.queue.s = replace(self.settings, ingress_max_pending=1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.intake(), range(8)))
        self.assertEqual(responses, [(200, ACK)] * 8)
        self.assertEqual(self.app.queue.counts(), {"pending": 1})
        other = self.payload(message=dict(self.z.message, id=124))
        self.assertEqual(self.intake(other)[0], 503)
        self.assertEqual(self.intake(), (200, ACK))

    def test_ingress_does_not_wait_for_slow_core_lock(self):
        acquired, release = threading.Event(), threading.Event()
        def slow_core():
            with self.app.b.lock:
                acquired.set()
                release.wait(4)
        thread = threading.Thread(target=slow_core)
        thread.start()
        self.assertTrue(acquired.wait(1))
        started = time.monotonic()
        try:
            self.assertEqual(self.intake(), (200, ACK))
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            release.set()
            thread.join()

    def test_intake_and_callback_restart_deduplication(self):
        self.subscribe()
        self.intake()
        self.restart()
        self.assertEqual(self.intake(), (200, ACK))
        self.assertEqual(self.app.queue.counts(), {"pending": 1})
        self.assertTrue(self.app.ingest_one())
        self.restart()
        self.assertTrue(self.app.deliver_one())
        self.assertFalse(self.app.deliver_one())
        self.assertEqual(self.intake(), (200, ACK))
        self.assertFalse(self.app.ingest_one())
        self.assertEqual(len(self.cb.calls), 2)
        self.assertEqual(self.app.queue.counts(), {"done": 1})

    def test_interrupted_claim_recovers_on_restart(self):
        self.intake()
        self.assertEqual(self.app.queue.claim()["attempts"], 1)
        self.restart()
        self.assertTrue(self.app.ingest_one())
        self.assertEqual(self.row()["state"], "done")
        self.assertEqual(self.row()["attempts"], 2)

    def test_finish_failure_recovers_without_restart_and_core_dedupes(self):
        self.subscribe()
        self.intake()
        with patch.object(self.app.queue, "finish", side_effect=sqlite3.OperationalError("busy")):
            with self.assertRaises(sqlite3.OperationalError):
                self.app.ingest_one()
        self.assertEqual(self.row()["state"], "processing")
        self.assertFalse(self.app.ingest_one())
        self.clock[0] += 301
        self.assertTrue(self.app.ingest_one())
        self.assertEqual(self.row()["state"], "done")
        self.assertEqual(self.app.b.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    def test_ingest_failures_back_off_and_have_finite_budget(self):
        self.intake()
        with patch.object(self.app.b, "fetch_public_message", side_effect=RpcError(-32000, "secret upstream text", {"status": 503})):
            for attempt in range(1, 9):
                self.assertTrue(self.app.ingest_one())
                self.assertEqual(self.row()["attempts"], attempt)
                self.assertFalse(self.app.ingest_one())
                self.clock[0] = self.row()["due"]
        self.assertEqual(self.row()["state"], "failed")
        self.assertNotIn("secret", canonical(self.row()))
        self.assertEqual(self.intake(), (200, ACK))
        self.assertFalse(self.app.ingest_one())

    def test_permanent_deleted_message_is_finished(self):
        self.intake()
        with patch.object(self.app.b, "fetch_public_message", side_effect=RpcError(-32000, "missing", {"status": 404})):
            self.assertTrue(self.app.ingest_one())
        self.assertEqual(self.row()["state"], "done")

    def test_delivery_live_check_errors_are_backed_off_and_bounded(self):
        self.ingest()
        with patch.object(self.app.b, "live_trigger", side_effect=RpcError(-32000, "unavailable", {"status": 503})):
            for attempt in range(1, 9):
                self.assertTrue(self.app.deliver_one())
                row = self.app.b.db.execute("SELECT * FROM deliveries").fetchone()
                self.assertEqual(row["attempts"], attempt)
                self.assertFalse(self.app.deliver_one())
                self.clock[0] = row["due"]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(len(self.cb.calls), 1)

    def test_unsubscribe_serializes_with_delivery_failure_accounting(self):
        self.ingest()
        entered, release, unsubscribed = threading.Event(), threading.Event(), threading.Event()
        original = self.app.b.deliver_one
        def pause():
            entered.set()
            release.wait(2)
            return original()
        def unsubscribe():
            self.app.b.unsubscribe(self.params)
            unsubscribed.set()
        with patch.object(self.app.b, "deliver_one", side_effect=pause), patch.object(self.app.b, "live_trigger", side_effect=RpcError(-32000, "unavailable", {"status": 503})):
            delivering = threading.Thread(target=self.app.deliver_one)
            delivering.start()
            self.assertTrue(entered.wait(1))
            deleting = threading.Thread(target=unsubscribe)
            deleting.start()
            self.assertFalse(unsubscribed.wait(0.05))
            release.set()
            delivering.join(3)
            deleting.join(3)
        self.assertFalse(delivering.is_alive())
        self.assertTrue(unsubscribed.is_set())
        self.assertEqual(self.app.b.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_uncertain_reply_survives_restart_without_second_send(self):
        self.ingest()
        self.z.fail_send = True
        args = {"name": "reply_to_message", "arguments": {"message_id": 123, "content": "Answer", "request_id": "uncertain-route"}}
        self.assertEqual(self.rpc("tools/call", args)[0], 503)
        self.restart()
        self.z.fail_send = False
        result = self.rpc("tools/call", args)
        self.assertIn("uncertain", result[1]["error"]["message"])
        self.assertEqual(len([c for c in self.z.calls if c[:2] == ("POST", "messages")]), 1)

    def test_concurrent_same_reply_never_posts_twice(self):
        self.ingest()
        args = {"name": "reply_to_message", "arguments": {"message_id": 123, "content": "Answer", "request_id": "race-reply"}}
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.rpc("tools/call", args), range(8)))
        self.assertEqual(len([c for c in self.z.calls if c[:2] == ("POST", "messages")]), 1)
        self.assertTrue(any("result" in r[1] for r in responses))
        self.assertTrue(all("result" in r[1] or "uncertain" in r[1]["error"]["message"] for r in responses))

    def test_status_is_authenticated_and_contains_counts_not_secrets(self):
        self.intake()
        status, result = self.request("/status", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(result["ingress"], {"pending": 1})
        self.assertEqual(result["mode"], "outgoing_webhook")
        for secret in (TOKEN, SECRET, self.c.bearer, self.c.api_key):
            self.assertNotIn(secret, canonical(result))
        self.assertEqual(self.request("/healthz", method="GET", auth=False)[0], 200)
        with patch.object(self.app, "preflight", side_effect=ValueError("private error")):
            self.assertEqual(self.request("/status", method="GET")[0], 503)

    def test_gates_disable_subscriptions_ingress_callbacks_and_replies(self):
        self.ingest()
        self.app.s = replace(self.settings, webhook_enabled=False, subscriptions_enabled=False)
        self.assertIn("error", self.rpc("events/subscribe", self.params)[1])
        self.assertEqual(self.intake()[0], 503)
        self.assertFalse(self.app.deliver_one())
        args = {"name": "reply_to_message", "arguments": {"message_id": 123, "content": "No", "request_id": "closed"}}
        self.assertIn("error", self.rpc("tools/call", args)[1])
        self.assertEqual(self.rpc("events/unsubscribe", self.params)[1]["result"], {})

    def test_modern_and_legacy_envelopes_and_notifications(self):
        for method in CATALOG_METHODS:
            status, response = self.rpc(method, headers={"MCP-Protocol-Version": PROTOCOL})
            self.assertEqual(status, 200)
            self.assertEqual(response["result"]["resultType"], "complete")
            self.assertEqual(response["result"]["cacheScope"], "private")
            self.assertEqual(response["result"]["ttlMs"], 0)
            self.assertIn("io.modelcontextprotocol/serverInfo", response["result"]["_meta"])
        for version in LEGACY_VERSIONS:
            result = self.rpc("initialize", {"protocolVersion": version}, headers={"MCP-Protocol-Version": PROTOCOL})[1]["result"]
            self.assertEqual(result["protocolVersion"], version)
            self.assertNotIn("resultType", result)
        self.assertNotIn("resultType", self.rpc("tools/list")[1]["result"])
        result = self.rpc("ping", {"_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL}})[1]["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(self.request(value={"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))
        self.assertEqual(self.request(value={"jsonrpc": "2.0", "method": "events/subscribe", "params": self.params}), (202, None))
        self.assertEqual(len(self.cb.calls), 0)
        self.assertEqual(self.rpc("tools/list", headers={"MCP-Protocol-Version": "unknown"})[0], 400)
        self.assertEqual(self.rpc("unknown")[1]["error"]["code"], -32601)

    def test_invalid_rpc_envelopes_and_nested_params(self):
        for payload in ({}, {"jsonrpc": "2.0", "id": True, "method": "ping"}, {"jsonrpc": "2.0", "id": [], "method": "ping"}):
            self.assertEqual(self.request(value=payload)[0], 400)
        for params in ([], {"_meta": []}):
            self.assertEqual(self.request(value={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": params})[1]["error"]["code"], -32602)
        malformed = dict(self.params, delivery=[])
        self.assertEqual(self.rpc("events/subscribe", malformed)[1]["error"]["code"], -32602)

    def test_settings_fail_closed_and_env_names(self):
        with patch.dict(os.environ, {"ZULIP_WEBHOOK_TOKEN": TOKEN}, clear=True):
            settings = Settings.from_env().validate(self.c)
            self.assertFalse(settings.webhook_enabled)
            self.assertFalse(settings.subscriptions_enabled)
        for change in ({"webhook_token": "short"}, {"webhook_token": self.c.bearer}, {"ingress_max_attempts": 0}, {"ingress_max_pending": 0}, {"subscriptions_enabled": False, "webhook_enabled": True}):
            with self.assertRaises(ValueError):
                replace(self.settings, **change).validate(self.c)
        replace(self.settings, webhook_token=self.c.api_key).validate(self.c)
        with self.assertRaises(ValueError):
            self.settings.validate(replace(self.c, database=":memory:"))
        with patch.dict(os.environ, {"ZULIP_WEBHOOK_TOKEN": TOKEN, "WEBHOOK_ENABLED": "yes"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_bad_identity_prevents_start(self):
        original = self.z.call
        def wrong_identity(method, path, params):
            if path == "users/me":
                return {"user_id": 98, "email": self.c.bot_email, "is_bot": True}
            return original(method, path, params)
        self.http.close()
        self.app.close()
        self.z.call = wrong_identity
        self.app = PortableApp(Bridge(self.c, self.cb, self.z, now=lambda: self.clock[0]), self.settings)
        self.http = RunningHTTP(handler_for(self.app))
        with self.assertRaises(ValueError):
            self.app.start()
        self.assertFalse(self.app.ready)
        self.assertEqual(self.intake()[0], 503)

    def test_database_cannot_switch_owner_or_adopt_legacy_data(self):
        bridge = Bridge(replace(self.c, owner="another-owner"), self.cb, self.z)
        try:
            with self.assertRaises(ValueError):
                PortableApp(bridge, self.settings)
        finally:
            bridge.db.close()
        legacy = Bridge(replace(self.c, database=self.tmp.name + "/legacy.sqlite3"), self.cb, self.z)
        try:
            with legacy.db:
                legacy.db.execute("INSERT INTO state VALUES('last_gap','old')")
            with self.assertRaises(ValueError):
                PortableApp(legacy, self.settings)
        finally:
            legacy.db.close()

    def test_single_instance_lock_prevents_second_process_and_releases(self):
        source = "from portable import single_instance; import sys;\nwith single_instance(sys.argv[1]): pass"
        with single_instance(self.c.database):
            result = subprocess.run([sys.executable, "-c", source, self.c.database], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Another portable process", result.stderr)
        result = subprocess.run([sys.executable, "-c", source, self.c.database], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_import_path_needs_no_cloud_sdk_or_listener(self):
        source = """import sys
class BlockCloud:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('boto3','botocore','aws'):
            raise AssertionError('unexpected cloud import')
sys.meta_path.insert(0, BlockCloud())
import portable
assert not hasattr(portable, 'Listener')
"""
        result = subprocess.run([sys.executable, "-c", source], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
