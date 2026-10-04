"""Cloud-independent outgoing-webhook adapter (Python 3.12+, standard library).

Run one process behind a trusted TLS/authentication reverse proxy. The adapter
never starts the optional generic-bot poller. SQLite must be on a durable local
filesystem; do not share its database across processes or use network storage.
Webhook acknowledgement means an ID was committed, not that a callback/reply
was delivered. External callbacks are at-least-once; recipients deduplicate IDs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import signal
import sqlite3
import threading
import time

from bridge import Bridge, Config, MAX_BODY, PROTOCOL, RpcError, canonical, permanent_unavailable, positive

LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "zulip-dot-bridge-portable", "version": "0.2.0"}
CATALOG_METHODS = {"server/discover", "tools/list", "events/list", "resources/list", "resources/templates/list", "prompts/list"}
ACK = {"response_not_required": True}


def env_bool(name):
    value = os.environ.get(name, "false").strip().lower()
    if value not in ("true", "false"):
        raise ValueError(name + " must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    webhook_token: str
    subscriptions_enabled: bool = False
    webhook_enabled: bool = False
    ingress_max_attempts: int = 8
    ingress_max_pending: int = 10000

    def validate(self, config):
        if (not isinstance(self.webhook_token, str) or len(self.webhook_token) < 24
                or config.bearer in (self.webhook_token, config.api_key)):
            raise ValueError("Configure strong credentials and a distinct MCP bearer")
        if type(self.subscriptions_enabled) is not bool or type(self.webhook_enabled) is not bool:
            raise ValueError("Gates must be booleans")
        if self.webhook_enabled and not self.subscriptions_enabled:
            raise ValueError("Enable subscriptions before webhook intake")
        if type(self.ingress_max_attempts) is not int or not 1 <= self.ingress_max_attempts <= 20:
            raise ValueError("INGRESS_MAX_ATTEMPTS must be between 1 and 20")
        if type(self.ingress_max_pending) is not int or not 1 <= self.ingress_max_pending <= 1000000:
            raise ValueError("INGRESS_MAX_PENDING must be between 1 and 1000000")
        if not config.database or config.database == ":memory:" or config.database.startswith("file:"):
            raise ValueError("Portable intake requires a persistent SQLite file")
        return self

    @classmethod
    def from_env(cls):
        return cls(os.environ["ZULIP_WEBHOOK_TOKEN"], env_bool("SUBSCRIPTIONS_ENABLED"),
                   env_bool("WEBHOOK_ENABLED"), int(os.environ.get("INGRESS_MAX_ATTEMPTS", "8")),
                   int(os.environ.get("INGRESS_MAX_PENDING", "10000")))


@contextmanager
def single_instance(database):
    """POSIX advisory process lock, released by the OS even after a crash.

    The lock file is deliberately never unlinked: unlinking a held inode could
    let a third process acquire a different lock. All portable CLI processes
    must use the same resolved database path. This cannot police other programs.
    """
    import fcntl  # POSIX server dependency, part of Python's standard library.
    database = os.path.realpath(database)
    os.makedirs(os.path.dirname(database), mode=0o700, exist_ok=True)
    fd = os.open(database + ".portable.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another portable process owns this database") from None
        yield
    finally:
        os.close(fd)


class QueueFull(Exception):
    pass


class IngressQueue:
    """ID-only durable queue, separate connection/lock from slow core RPC reads.

    The CLI holds single_instance before opening this object. Claims commit
    before work; interrupted claims recover on restart. Attempts count claims,
    including crashes, so repeated crashes eventually require operator review.
    Completed/dead IDs remain for deduplication; retention is operator-managed.
    """
    def __init__(self, database, settings, now=time.time):
        self.s, self.now = settings, now
        self.lock = threading.Lock()
        self.db = sqlite3.connect(database, timeout=1, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=1000;
            CREATE TABLE IF NOT EXISTS webhook_jobs (
                message_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL,
                received REAL NOT NULL,
                lease_until REAL,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS webhook_jobs_due ON webhook_jobs(state,due);
        """)
        with self.db:
            self.db.execute("UPDATE webhook_jobs SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END, due=?, last_error='interrupted' WHERE state='processing'",
                            (self.s.ingress_max_attempts, self.now()))

    def enqueue(self, message_id):
        positive(message_id)
        # BEGIN IMMEDIATE serializes the count+insert and duplicate checks.
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM webhook_jobs WHERE message_id=?", (message_id,)).fetchone():
                return False
            count = self.db.execute("SELECT count(*) FROM webhook_jobs WHERE state IN ('pending','processing')").fetchone()[0]
            if count >= self.s.ingress_max_pending:
                raise QueueFull()
            self.db.execute("INSERT INTO webhook_jobs(message_id,due,received) VALUES(?,?,?)", (message_id, self.now(), self.now()))
        return True  # The transaction has committed before a caller may ACK.

    def claim(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # A transient failure while saving the outcome must not strand a
            # claimed job until the next process restart.
            self.db.execute("UPDATE webhook_jobs SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END, due=?, last_error='interrupted', lease_until=NULL WHERE state='processing' AND lease_until<=?",
                            (self.s.ingress_max_attempts, self.now(), self.now()))
            row = self.db.execute("SELECT * FROM webhook_jobs WHERE state='pending' AND due<=? ORDER BY due,message_id LIMIT 1", (self.now(),)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE webhook_jobs SET state='processing',attempts=attempts+1,lease_until=? WHERE message_id=?", (self.now() + 300, row["message_id"]))
            return {**dict(row), "attempts": row["attempts"] + 1}

    def finish(self, job, error=None):
        attempts = job["attempts"]
        state = "done" if error is None else ("failed" if attempts >= self.s.ingress_max_attempts else "pending")
        due = self.now() if error is None else self.now() + min(3600, 2 ** attempts)
        with self.lock, self.db:
            self.db.execute("UPDATE webhook_jobs SET state=?,due=?,last_error=?,lease_until=NULL WHERE message_id=? AND state='processing'",
                            (state, due, error, job["message_id"]))

    def counts(self):
        with self.lock:
            return dict(self.db.execute("SELECT state,count(*) FROM webhook_jobs GROUP BY state").fetchall())

    def close(self):
        self.db.close()


def rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


class PortableApp:
    def __init__(self, bridge, settings):
        self.b, self.s = bridge, settings.validate(bridge.c)
        self.ready = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.work_lock = threading.Lock()
        # Hold the same delivery selection through core processing and error
        # accounting while allowing core/unsubscribe to reuse this lock.
        bridge.delivery_lock = threading.RLock()
        self.worker = None
        self.worker_error = False
        # Bind persisted IDs/replies to one realm+bot+owner and reject legacy
        # poller databases. A fresh database avoids silently mixing adapters.
        identity = canonical([bridge.c.realm, bridge.c.bot_email, bridge.c.bot_id, bridge.c.owner])
        with bridge.lock, bridge.db:
            bridge.db.execute("PRAGMA synchronous=FULL")
            if bridge.db.execute("SELECT 1 FROM state WHERE key='queue'").fetchone():
                raise ValueError("Use a fresh database for outgoing-webhook mode")
            old = bridge.db.execute("SELECT value FROM state WHERE key='portable_identity'").fetchone()
            if not old and any(bridge.db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                               for table in ("state", "subscriptions", "public_messages", "messages", "deliveries", "replies")):
                raise ValueError("Use a fresh database for outgoing-webhook mode")
            if old and old["value"] != identity:
                raise ValueError("Database belongs to another realm, bot, or owner")
            bridge.db.execute("INSERT OR IGNORE INTO state VALUES('portable_identity',?)", (identity,))
        self.queue = IngressQueue(bridge.c.database, settings, bridge.now)

    def preflight(self):
        """Read-only identity/access check; never changes bot/channel membership."""
        own = self.b.zulip.call("GET", "users/me", {})
        if (own.get("user_id") != self.b.c.bot_id or own.get("email") != self.b.c.bot_email
                or own.get("is_bot") is not True):
            raise ValueError("Configured bot identity mismatch")
        channels = self.b.public_channels()
        if not channels:
            raise ValueError("No accessible public channels")
        self.b.tools_call("read_discussion", {"channel_id": next(iter(channels)), "limit": 1})
        return True

    def start(self, background=True):
        if self.ready or self.worker:
            raise RuntimeError("Application already started")
        self.preflight()
        self.ready = True
        if background:
            self.worker = threading.Thread(target=self.run_worker, name="zulip-webhook-worker", daemon=True)
            self.worker.start()

    def close(self):
        self.ready = False
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join()  # Wait for in-flight work before closing SQLite.
        self.queue.close()
        self.b.db.close()

    def intake(self, payload):
        token = payload.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.s.webhook_token.encode()):
            return 401, {"error": "Unauthorized"}
        if not self.ready or not self.s.webhook_enabled:
            return 503, {"error": "Webhook not enabled or ready"}
        message = payload.get("message")
        if not isinstance(message, dict):
            return 400, {"error": "Invalid message"}
        if payload.get("trigger") != "mention" or message.get("type") != "stream":
            return 200, ACK  # In particular, never queue or retain DM bodies.
        try:
            mid = positive(message.get("id"))
            self.queue.enqueue(mid)  # No upstream network calls in this path.
        except (ValueError, OverflowError):
            return 400, {"error": "Invalid message id"}
        except (QueueFull, sqlite3.Error, OSError):
            return 503, {"error": "Durable intake unavailable"}
        self.wake.set()
        return 200, ACK

    def ingest_one(self):
        if not self.ready or not self.s.webhook_enabled:
            return False
        with self.work_lock:
            job = self.queue.claim()
            if job is None:
                return False
            error = None
            try:
                # Trust only the ID from intake; obtain canonical public data
                # before handing an authenticated source event to the shared core.
                source = self.b.fetch_public_message(job["message_id"])
                self.b.ingest_event({"type": "message", "message": source})
            except ValueError:
                pass  # Missing/non-public/invalid canonical source is ineligible.
            except RpcError as exc:
                if not permanent_unavailable(exc):
                    error = "upstream_unavailable"
            except Exception:
                error = "processing_unavailable"  # Never persist raw exception text.
            self.queue.finish(job, error)
            return True

    def deliver_one(self):
        if not self.ready or not self.s.webhook_enabled or not self.s.subscriptions_enabled:
            return False
        with self.work_lock, self.b.delivery_lock:
            with self.b.lock:
                row = self.b.db.execute("SELECT d.sub_id,d.event_id,d.attempts FROM deliveries d JOIN subscriptions s ON s.id=d.sub_id WHERE d.state='pending' AND d.due<=? ORDER BY d.due LIMIT 1", (self.b.now(),)).fetchone()
            if not row:
                return False
            try:
                return self.b.deliver_one()
            except Exception:
                # Core already bounds callback HTTP failures. Bound transient
                # live-eligibility failures too, which otherwise bubble outward.
                attempts = row["attempts"] + 1
                with self.b.lock, self.b.db:
                    self.b.db.execute("UPDATE deliveries SET attempts=?,state=?,due=? WHERE sub_id=? AND event_id=? AND state='pending'",
                                      (attempts, "failed" if attempts >= 8 else "pending",
                                       self.b.now() + min(3600, 2 ** attempts), row["sub_id"], row["event_id"]))
                return True

    def run_worker(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                ingested = self.ingest_one()
                delivered = self.deliver_one()
                if ingested or delivered:
                    self.worker_error = False
            except Exception:
                self.worker_error = True
                ingested = delivered = False
            if not ingested and not delivered:
                self.wake.wait(1)

    def status(self):
        self.preflight()
        with self.b.lock:
            deliveries = dict(self.b.db.execute("SELECT state,count(*) FROM deliveries GROUP BY state").fetchall())
            subscriptions = self.b.db.execute("SELECT count(*) FROM subscriptions WHERE owner=? AND expires>?", (self.b.c.owner, self.b.now())).fetchone()[0]
        return {"configured": True, "zulip_realm": self.b.c.realm, "public_read_preflight_ok": True,
                "ready": self.ready, "subscriptions_enabled": self.s.subscriptions_enabled,
                "webhook_enabled": self.s.webhook_enabled, "active_subscriptions": subscriptions,
                "ingress": self.queue.counts(), "deliveries": deliveries, "worker_error": self.worker_error,
                "mode": "outgoing_webhook", "callback_policy": "exact allowed hosts; public HTTPS only"}

    def rpc(self, request, protocol=None):
        """Stateless legacy and MCP2 envelopes; no OAuth or session impersonation."""
        rid = request.get("id") if isinstance(request, dict) else None
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or (rid is not None and type(rid) not in (str, int, float))):
            return 400, rpc_error(None, -32600, "Invalid Request")
        params = request.get("params", {})
        if not isinstance(params, dict) or ("_meta" in params and not isinstance(params["_meta"], dict)):
            return 200, rpc_error(rid, -32602, "Invalid params")
        if protocol and protocol not in (PROTOCOL, *LEGACY_VERSIONS):
            return 400, rpc_error(rid, -32600, "Unsupported protocol version")
        if "id" not in request:
            return 202, None  # Notifications never trigger state-changing methods.
        method = request["method"]
        modern = method != "initialize" and (method == "server/discover" or protocol == PROTOCOL
                  or params.get("_meta", {}).get("io.modelcontextprotocol/protocolVersion") == PROTOCOL)
        if method == "initialize":
            requested = params.get("protocolVersion")
            result = {"protocolVersion": requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0],
                      "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
        elif method == "ping":
            result = {}
        elif method in ("resources/list", "resources/templates/list", "prompts/list"):
            key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]
            result = {key: []}
        else:
            if method == "events/subscribe" and (not self.ready or not self.s.subscriptions_enabled):
                return 200, rpc_error(rid, -32000, "Subscriptions not enabled or ready")
            if (method == "tools/call" and params.get("name") == "reply_to_message"
                    and (not self.ready or not self.s.webhook_enabled or not self.s.subscriptions_enabled)):
                return 200, rpc_error(rid, -32000, "Replies not enabled or ready")
            try:
                response = self.b.rpc(request)
            except (ValueError, TypeError, KeyError, AttributeError):
                return 200, rpc_error(rid, -32602, "Invalid or unauthorized parameters")
            except Exception:
                return 503, rpc_error(rid, -32000, "Request unavailable; a reply may have an uncertain outcome. Reuse its request_id.")
            if "error" in response:
                return 200, response
            result = response["result"]
            if method == "server/discover":
                result["serverInfo"] = SERVER_INFO
        if modern:
            result = {**result, "resultType": "complete", "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
            if method in CATALOG_METHODS:
                result.update(ttlMs=0, cacheScope="private")
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")
    def finite_float(value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float)
    if not isinstance(value, dict):
        raise ValueError("Object required")
    return value


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        # Public exposure requires a real TLS proxy with body, rate, concurrency,
        # header and time limits. Do not publish this stdlib server directly.
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def reply(self, status, value):
            raw = b"" if value is None else canonical(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw)

        def bearer_ok(self):
            return len(self.headers.get_all("Authorization", [])) == 1 and app.b.authorized(self.headers.get("Authorization"))

        def do_GET(self):
            if self.headers.get("Origin"):
                return self.reply(403, {"error": "Browser-origin requests disabled"})
            if self.path == "/healthz":
                return self.reply(200, {"ok": True, "meaning": "process alive only"})
            if self.path == "/status":
                if not self.bearer_ok():
                    return self.reply(401, {"error": "Unauthorized"})
                try:
                    return self.reply(200, app.status())
                except Exception:
                    return self.reply(503, {"error": "Readiness preflight unavailable"})
            return self.reply(405 if self.path in ("/mcp", "/zulip/events") else 404, {"error": "Not supported"})

        def do_POST(self):
            if self.path not in ("/mcp", "/zulip/events"):
                return self.reply(404, {"error": "Not found"})
            if self.headers.get("Origin"):
                return self.reply(403, {"error": "Browser-origin requests disabled"})
            if self.path == "/mcp" and not self.bearer_ok():
                return self.reply(401, {"error": "Unauthorized"})
            if (self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding")
                    or len(self.headers.get_all("Content-Type", [])) != 1
                    or self.headers.get_content_type() != "application/json"):
                return self.reply(415, {"error": "Plain JSON with Content-Length required"})
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise ValueError("Invalid Content-Length")
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY:
                    return self.reply(413, {"error": "Invalid body size"})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete body")
                payload = strict_json(raw)
                if self.path == "/zulip/events":
                    status, value = app.intake(payload)
                else:
                    status, value = app.rpc(payload, self.headers.get("MCP-Protocol-Version"))
                return self.reply(status, value)
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                return self.reply(400, {"error": "Invalid payload"})
            except (TimeoutError, OSError, sqlite3.Error):
                return self.reply(503, {"error": "Request unavailable"})

    return Handler


def main():
    os.umask(0o077)
    config = Config.from_env()
    settings = Settings.from_env().validate(config)
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    with single_instance(config.database):
        app = PortableApp(Bridge(config), settings)
        server = None
        try:
            # Gates never override a failed identity/public-read check.
            app.start()
            server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
            server.daemon_threads = False
            server.timeout = 0.5
            def stopping(_signum, _frame):
                app.stop.set()
                app.wake.set()
            signal.signal(signal.SIGTERM, stopping)
            signal.signal(signal.SIGINT, stopping)
            while not app.stop.is_set():
                server.handle_request()
        finally:
            if server:
                server.server_close()
            app.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Portable bridge stopped: check configuration, durable storage, and Zulip access. No credentials were logged.") from None
