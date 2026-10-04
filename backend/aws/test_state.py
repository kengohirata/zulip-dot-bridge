import copy
import importlib.util
import json
import pathlib
import unittest
from aws.state import DynamoBridge, Store
from aws.test_checkpoint import FakeTable
from bridge import EVENT, RpcError

spec = importlib.util.spec_from_file_location('core_test_fixtures', pathlib.Path(__file__).parents[1] / 'tests' / 'test_bridge.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)

class DynamoTests(unittest.TestCase):
    def setUp(self):
        self.cb, self.z = fixtures.Callback(), fixtures.ZulipMock()
        self.clock, self.queued = [1000], []
        self.store = Store(FakeTable())
        self.b = DynamoBridge(fixtures.config(), self.store, lambda job,delay=0:self.queued.append((job,delay)), self.cb,self.z,now=lambda:self.clock[0])
        self.params = {'name':EVENT,'arguments':{'scope':'all_public'},'delivery':{'mode':'webhook','url':'https://callback.example/events','secret':fixtures.SECRET}}
    def prepare(self):
        self.b.subscribe(self.params)
        self.b.ingest_message(123)
        return next(self.store.prefix('delivery#'))[0]
    def test_subscription_reuses_generation_and_challenge(self):
        sid = self.b.subscribe(self.params)['id']
        generation = self.store.get('sub#'+sid)['generation']
        self.b.subscribe(copy.deepcopy(self.params))
        self.assertEqual(self.store.get('sub#'+sid)['generation'],generation)
        self.assertEqual(len(self.cb.calls),1)
    def test_failed_challenge_not_persisted(self):
        self.cb.challenge_ok = False
        with self.assertRaises(RpcError): self.b.subscribe(self.params)
        self.assertEqual(list(self.store.prefix('sub#')),[])
    def test_delivery_and_dedupe(self):
        key = self.prepare()
        self.b.ingest_message(123)
        self.assertEqual(len(list(self.store.prefix('delivery#'))),1)
        self.b.deliver(key)
        self.assertEqual(self.store.get(key)['state'],'delivered')
        count = len(self.cb.calls)
        self.b.deliver(key)
        self.assertEqual(len(self.cb.calls),count)
    def test_retry_outbox_recovers_queue_failure(self):
        key = self.prepare()
        self.cb.status = 503
        self.b.deliver(key)
        self.assertEqual(self.store.get(key)['state'],'pending')
        self.clock[0] += 10
        self.queued.clear()
        self.b.recover()
        self.assertTrue(any(job['key']==key for job,_ in self.queued))
        self.cb.status = 200
        self.b.deliver(key)
        self.assertEqual(self.store.get(key)['state'],'delivered')
    def test_partial_ingest_recovery(self):
        key = self.prepare()
        self.store.delete(key)
        record = self.store.get("msg#123")
        record["outbox_complete"] = False
        self.store.put("msg#123",record)
        self.b.recover()
        self.assertEqual(self.store.get(key)['state'],'pending')
    def test_unsubscribe_cannot_revive_old_generation(self):
        key = self.prepare()
        original = self.store.get(key)
        self.b.unsubscribe(self.params)
        self.b.subscribe(self.params)
        # Simulate recovering a previously absent outbox item from an interrupted ingest.
        self.store.delete(key)
        record = self.store.get("msg#123")
        record["outbox_complete"] = False
        self.store.put("msg#123",record)
        self.b.recover()
        self.b.deliver(key)
        self.assertEqual(self.store.get(key)['state'],'revoked')
    def test_private_and_edited_trigger_do_not_deliver(self):
        key = self.prepare()
        self.z.message['content'] = 'changed'
        self.b.deliver(key)
        self.assertEqual(self.store.get(key)['state'],'revoked')
    def test_successful_reply_is_idempotent(self):
        self.prepare()
        args = {'message_id':123,'content':'answer','request_id':'request-a'}
        self.assertEqual(self.b.tools_call('reply_to_message',args)['id'],456)
        self.b.tools_call('reply_to_message',args)
        self.assertEqual(len([c for c in self.z.calls if c[:2]==('POST','messages')]),1)
        with self.assertRaises(ValueError): self.b.tools_call('reply_to_message',{**args,'content':'different'})
    def test_uncertain_reply_cannot_resend(self):
        self.prepare()
        self.z.fail_send = True
        args = {'message_id':123,'content':'answer','request_id':'request-b'}
        with self.assertRaises(OSError): self.b.tools_call('reply_to_message',args)
        self.z.fail_send = False
        with self.assertRaises(RpcError): self.b.tools_call('reply_to_message',args)
        self.assertEqual(len([c for c in self.z.calls if c[:2]==('POST','messages')]),1)
    def test_completed_records_leave_sparse_pending_index(self):
        key = self.prepare()
        self.assertNotIn("bucket",self.store.table.items["msg#123"])
        self.assertEqual(self.store.table.items[key]["bucket"],"pending")
        self.b.deliver(key)
        self.assertNotIn("bucket",self.store.table.items[key])
        self.assertEqual(list(self.store.pending(self.clock[0])),[])
    def test_reply_claim_must_be_durable_before_send(self):
        self.prepare()
        old = self.store.put
        def fail(key, value, absent=False):
            if key.startswith('reply#'): raise OSError('DynamoDB unavailable')
            return old(key,value,absent)
        self.store.put = fail
        with self.assertRaises(OSError): self.b.tools_call('reply_to_message',{'message_id':123,'content':'answer','request_id':'request-c'})
        self.assertEqual(len([c for c in self.z.calls if c[:2]==('POST','messages')]),0)

if __name__ == '__main__': unittest.main()
