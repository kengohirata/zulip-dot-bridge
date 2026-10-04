import base64
import copy
import hashlib
import hmac
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from bridge import Bridge, Config, EVENT, Listener, MentionParser, PublicHTTPS, RpcError, Zulip, canonical, signed_headers, signing_key


SECRET = 'whsec_' + base64.b64encode(b'test-only-signing-key-not-real-123').decode()
NEW_SECRET = 'whsec_' + base64.b64encode(b'another-test-signing-key-12345678').decode()


def config(db=':memory:'):
    return Config('https://realm.example', 'bot@realm.example', 99, 'test-api-key-only-xxxxxxxxx',
                  'test-bearer-only-xxxxxxxxxx', 'test-owner',
                  frozenset({'callback.example'}), database=db)


class Callback:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.challenge_ok = True

    def request(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        parsed = json.loads(body)
        if parsed.get('type') == 'verification':
            return self.status, canonical({'challenge': parsed['challenge'] if self.challenge_ok else 'wrong'}).encode()
        return self.status, b'{}'


class ZulipMock:
    def __init__(self):
        self.calls = []
        self.public = True
        self.archived = False
        self.active = True
        self.is_bot = False
        self.mention = True
        self.fail_send = False
        self.bad_queue = False
        self.events = []
        self.message = {'id': 123, 'type': 'stream', 'stream_id': 7, 'sender_id': 8,
                        'subject': 'Research', 'content': '@**bot** question', 'timestamp': 990}

    def call(self, method, path, params):
        self.calls.append((method, path, copy.deepcopy(params)))
        if path == 'streams':
            return {'streams': [{'stream_id': 7, 'invite_only': not self.public, 'is_archived': self.archived, 'name': 'Public'},
                                {'stream_id': 9, 'invite_only': True, 'name': 'Private'}]}
        if path == 'users/me':
            return {'user_id': 99, 'email': 'bot@realm.example', 'is_bot': True}
        if path == 'users/8':
            return {'user': {'user_id': 8, 'is_active': self.active, 'is_bot': self.is_bot}}
        if path.startswith('messages/'):
            m = copy.deepcopy(self.message)
            if params.get('apply_markdown'):
                m['content'] = '<span class="user-mention" data-user-id="99">@bot</span>' if self.mention else '<p>hello</p>'
            return {'message': m}
        if path == 'messages' and method == 'GET':
            return {'result': 'success', 'messages': [copy.deepcopy(self.message), {'type': 'stream', 'stream_id': 9, 'content': 'private'}]}
        if path == 'messages' and method == 'POST':
            if self.fail_send:
                raise OSError('simulated uncertain response')
            return {'result': 'success', 'id': 456}
        if path == 'register':
            return {'queue_id': 'q-test', 'last_event_id': -1}
        if path == 'events':
            if self.bad_queue:
                raise RpcError(-32000, 'expired', {'provider_code': 'BAD_EVENT_QUEUE_ID'})
            return {'events': self.events}
        raise AssertionError((method, path, params))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.cb, self.z = Callback(), ZulipMock()
        self.clock = [1000]
        self.b = Bridge(config(), self.cb, self.z, now=lambda: self.clock[0])
        self.params = {'name': EVENT, 'arguments': {'scope':'all_public'}, 'delivery': {'mode': 'webhook', 'url': 'https://callback.example/events', 'secret': SECRET}}

    def event(self):
        return {'id': 0, 'type': 'message', 'message': copy.deepcopy(self.z.message)}

    def subscribe_ingest(self):
        sub = self.b.subscribe(self.params)
        self.b.ingest_event(self.event())
        return sub

    def test_fail_closed_config_and_auth(self):
        with self.assertRaises(ValueError):
            replace(config(), bearer='').validate()
        self.assertTrue(self.b.authorized('Bearer ' + self.b.c.bearer))
        self.assertFalse(self.b.authorized(None))
        self.assertFalse(self.b.authorized('Bearer wrong'))

    def test_discovery_and_tools(self):
        result = self.b.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover'})['result']
        self.assertEqual(result['supportedVersions'], ['2026-07-28'])
        self.assertIn('events', result['capabilities'])
        self.assertEqual(len(self.b.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']), 4)

    def test_subscription_identity_idempotence_verification_cache(self):
        a = self.b.subscribe(self.params)
        b = self.b.subscribe(copy.deepcopy(self.params))
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(len(self.cb.calls), 1)
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 1)

    def test_failed_challenge_never_activates(self):
        self.cb.challenge_ok = False
        with self.assertRaises(RpcError) as error:
            self.b.subscribe(self.params)
        self.assertEqual(error.exception.code, -32015)
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)

    def test_bad_secret_and_callback_rejected(self):
        for secret in ('bad', 'whsec_AAAA'):
            with self.assertRaises(ValueError):
                signing_key(secret)
        for url in ('http://callback.example/e', 'https://evil.example/e', 'https://callback.example:444/e', 'https://user@callback.example/e'):
            p = copy.deepcopy(self.params)
            p['delivery']['url'] = url
            with self.assertRaises(ValueError):
                self.b.subscribe(p)

    def test_signed_event_and_ingest_dedupe(self):
        self.subscribe_ingest()
        self.b.ingest_event(self.event())
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 1)
        self.assertTrue(self.b.deliver_one())
        method, url, body, headers = self.cb.calls[-1]
        event = json.loads(body)
        self.assertEqual(event['eventId'], headers['webhook-id'])
        raw = (headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.').encode() + body
        expected = 'v1,' + base64.b64encode(hmac.digest(signing_key(SECRET), raw, hashlib.sha256)).decode()
        self.assertEqual(expected, headers['webhook-signature'])
        self.assertFalse(self.b.deliver_one())

    def test_retry_keeps_id_fresh_signature(self):
        self.subscribe_ingest()
        self.cb.status = 503
        self.b.deliver_one()
        first = self.cb.calls[-1]
        self.clock[0] += 3
        self.cb.status = 200
        self.b.deliver_one()
        second = self.cb.calls[-1]
        self.assertEqual(first[2], second[2])
        self.assertEqual(first[3]['webhook-id'], second[3]['webhook-id'])
        self.assertNotEqual(first[3]['webhook-signature'], second[3]['webhook-signature'])

    def test_410_413_no_retry(self):
        for status in (410, 413):
            with self.subTest(status=status):
                b = Bridge(config(), Callback(), ZulipMock(), now=lambda: 1000)
                b.subscribe(self.params)
                b.ingest_event(self.event())
                b.callbacks.status = status
                b.deliver_one()
                self.assertEqual(b.db.execute('SELECT state FROM deliveries').fetchone()[0], 'failed')

    def test_secret_rotation_dual_signatures(self):
        self.b.subscribe(self.params)
        self.params['delivery']['secret'] = NEW_SECRET
        subid = self.b.subscribe(self.params)['id']
        sub = dict(self.b.db.execute('SELECT * FROM subscriptions WHERE id=?', (subid,)).fetchone())
        self.assertEqual(len(signed_headers(sub, b'{}', 'evt', 1000)['webhook-signature'].split()), 2)

    def test_unsubscribe_deletes_queued_event_and_is_idempotent(self):
        self.subscribe_ingest()
        self.b.unsubscribe(self.params)
        self.b.unsubscribe(self.params)
        self.assertFalse(self.b.deliver_one())

    def test_expiry_blocks_delivery(self):
        self.params['ttlMs'] = 1000
        self.subscribe_ingest()
        self.clock[0] += 2
        self.b.deliver_one()
        self.assertEqual(len(self.cb.calls), 1)
        self.assertEqual(self.b.db.execute('SELECT state FROM deliveries').fetchone()[0], 'revoked')

    def test_private_channels_and_dms_ignored(self):
        self.b.subscribe(self.params)
        e = self.event()
        e['message']['type'] = 'private'
        self.b.ingest_event(e)
        self.z.public = False
        self.b.ingest_event(self.event())
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM public_messages').fetchone()[0], 0)

    def test_nonmention_bot_inactive_ignored(self):
        self.b.subscribe(self.params)
        self.z.mention = False
        self.b.ingest_event(self.event())
        self.z.mention = True
        self.z.is_bot = True
        self.b.ingest_event(self.event())
        self.z.is_bot = False
        self.z.active = False
        self.b.ingest_event(self.event())
        self.assertFalse(self.b.deliver_one())
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_privacy_revoked_before_delivery(self):
        self.subscribe_ingest()
        self.z.public = False
        self.b.deliver_one()
        self.assertEqual(len(self.cb.calls), 1)

    def test_public_read_filters_response_and_paginate(self):
        result = self.b.tools_call('read_discussion', {'channel_id': 7, 'before_id': 200})
        self.assertEqual(len(result['messages']), 1)
        self.assertEqual(self.z.calls[-1][2]['anchor'], 200)
        self.assertFalse(self.z.calls[-1][2]['include_anchor'])
        with self.assertRaises(ValueError):
            self.b.tools_call('read_discussion', {'channel_id': 9})

    def test_reply_target_and_idempotence(self):
        self.subscribe_ingest()
        args = {'message_id': 123, 'content': 'Reply', 'request_id': 'response-test'}
        first = self.b.tools_call('reply_to_message', args)
        self.assertEqual(first, self.b.tools_call('reply_to_message', args))
        sends = [x for x in self.z.calls if x[:2] == ('POST', 'messages')]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][2]['to'], 7)
        self.assertEqual(sends[0][2]['topic'], 'Research')
        with self.assertRaises(ValueError):
            self.b.tools_call('reply_to_message', dict(args, content='different'))

    def test_uncertain_send_is_not_retried(self):
        self.subscribe_ingest()
        self.z.fail_send = True
        args = {'message_id': 123, 'content': 'Reply', 'request_id': 'uncertain'}
        with self.assertRaises(OSError):
            self.b.tools_call('reply_to_message', args)
        with self.assertRaises(RpcError):
            self.b.tools_call('reply_to_message', args)
        self.assertEqual(len([x for x in self.z.calls if x[:2] == ('POST', 'messages')]), 1)

    def test_listener_persists_queue_cursor_and_recovers_expired_queue(self):
        listener = Listener(self.b)
        listener.step()
        self.assertTrue(next(x for x in self.z.calls if x[1] == 'register')[2]['all_public_streams'])
        self.z.events = [self.event()]
        listener.step()
        state = json.loads(self.b.db.execute("SELECT value FROM state WHERE key='queue'").fetchone()[0])
        self.assertEqual(state['last_event_id'], 0)
        self.z.bad_queue = True
        listener.step()
        self.assertIsNone(self.b.db.execute("SELECT value FROM state WHERE key='queue'").fetchone())
        self.assertIsNotNone(self.b.db.execute("SELECT value FROM state WHERE key='last_gap'").fetchone())

    def test_restart_preserves_pending_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = config(tmp + '/state.db')
            first = Bridge(c, self.cb, self.z, now=lambda: 1000)
            first.subscribe(self.params)
            first.ingest_event(self.event())
            first.db.close()
            second = Bridge(c, self.cb, self.z, now=lambda: 1000)
            self.assertTrue(second.deliver_one())
            second.db.close()

    def test_live_changed_message_blocks_callback_and_reply(self):
        self.subscribe_ingest()
        self.z.message['content'] = 'edited content, no longer same request'
        self.b.deliver_one()
        self.assertEqual(len(self.cb.calls), 1)
        with self.assertRaises(ValueError):
            self.b.tools_call('reply_to_message', {'message_id': 123, 'content': 'Reply', 'request_id': 'changed'})

    def test_removed_mention_blocks_reply(self):
        self.subscribe_ingest()
        self.z.mention = False
        with self.assertRaises(ValueError):
            self.b.tools_call('reply_to_message', {'message_id': 123, 'content': 'Reply', 'request_id': 'removed'})

    def test_unsubscribe_after_channel_becomes_private(self):
        self.params['arguments'] = {'scope': 'all_public'}
        self.subscribe_ingest()
        self.z.public = False
        self.b.unsubscribe(self.params)
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)

    def test_archived_public_can_be_read_but_not_replied(self):
        self.subscribe_ingest()
        self.z.archived = True
        self.assertTrue(self.b.tools_call('read_discussion', {'channel_id':7})['messages'])
        with self.assertRaises(ValueError):
            self.b.tools_call('reply_to_message', {'message_id':123,'content':'Reply','request_id':'archived'})
        streams = [c for c in self.z.calls if c[1]=='streams']
        self.assertFalse(streams[-1][2]['exclude_archived'])

    def test_real_client_preserves_expired_queue_error_code(self):
        transport = unittest.mock.Mock()
        transport.request.return_value = (400, b'{"result":"error","code":"BAD_EVENT_QUEUE_ID"}')
        with self.assertRaises(RpcError) as error:
            Zulip(config(), transport).call('GET', 'events', {'queue_id': 'expired'})
        self.assertEqual(error.exception.data['provider_code'], 'BAD_EVENT_QUEUE_ID')
        self.assertEqual(error.exception.data['status'], 400)

    def test_deleted_queued_message_advances_cursor(self):
        listener = Listener(self.b)
        listener.step()
        self.z.events = [self.event()]
        original = self.z.call
        def call(method, path, params):
            if path == 'messages/123':
                raise RpcError(-32000, 'missing', {'status': 400, 'provider_code': 'BAD_REQUEST'})
            return original(method, path, params)
        self.z.call = call
        listener.step()
        state = json.loads(self.b.db.execute("SELECT value FROM state WHERE key='queue'").fetchone()[0])
        self.assertEqual(state['last_event_id'], 0)
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_transient_fetch_failure_preserves_cursor(self):
        listener = Listener(self.b)
        listener.step()
        self.z.events = [self.event()]
        original = self.z.call
        def call(method, path, params):
            if path == 'messages/123':
                raise RpcError(-32000, 'unavailable', {'status': 503})
            return original(method, path, params)
        self.z.call = call
        with self.assertRaises(RpcError):
            listener.step()
        state = json.loads(self.b.db.execute("SELECT value FROM state WHERE key='queue'").fetchone()[0])
        self.assertEqual(state['last_event_id'], -1)

    def test_ssrf_private_and_mixed_dns_blocked_before_socket(self):
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:8.8.8.8'):
            with self.subTest(address=address), patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]), patch('socket.socket') as sock:
                with self.assertRaises(ValueError):
                    PublicHTTPS({'callback.example'}).request('POST', 'https://callback.example/events')
                sock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
