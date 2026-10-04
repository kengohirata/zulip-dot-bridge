import assert from 'node:assert/strict';
import worker from '../worker/index.js';
let calls = 0;
globalThis.fetch = async () => { calls++; throw new Error('Outbound requests are forbidden in this probe.'); };
const call = async (method, params = {}, authenticated = false, protocol) => {
  const response = await worker.fetch(new Request('https://probe.invalid/mcp', {method:'POST', headers: {'content-type':'application/json', ...(protocol ? {'mcp-protocol-version':protocol} : {}), ...(authenticated ? {'oai-authenticated-user-id':'local-unit-test-user'} : {})}, body: JSON.stringify({jsonrpc:'2.0',id:1,method,params})}));
  return {status:response.status, body:await response.json()};
};
const discovery = await call('server/discover');
assert.equal(discovery.status,200);
assert.deepEqual(discovery.body.result.supportedVersions,['2026-07-28']);
assert.ok(discovery.body.result.capabilities.events);
for (const protocolVersion of ['2024-11-05','2025-03-26','2025-06-18','2025-11-25']) assert.equal((await call('initialize',{protocolVersion})).body.result.protocolVersion,protocolVersion);
const tools = (await call('tools/list')).body.result.tools;
assert.equal(tools.length,5); assert.equal(tools[0].name,'zulip_bridge_status'); assert.equal(tools[0].annotations.readOnlyHint,true);
const events = (await call('events/list')).body.result.events;
assert.equal(events.length,1); assert.equal(events[0].name,'zulip.mention'); assert.deepEqual(events[0].delivery,['webhook']);
assert.equal((await call('tools/call',{name:'zulip_bridge_status'})).status,401);
const status = (await call('tools/call',{name:'zulip_bridge_status'},true)).body.result.structuredContent;
assert.equal(status.authenticated,true); assert.equal(status.zulip_connected,false); assert.equal(status.state,'setup_required');
assert.equal(status.active_subscriptions,0); assert.equal(status.inference_enabled,false);
assert.equal((await call('tools/call',{name:'zulip_bridge_status',arguments:{unexpected:1}},true)).body.error.code,-32602);
assert.equal((await call('events/subscribe',{name:'zulip.mention'})).status,401);
assert.equal((await call('events/subscribe',{name:'zulip.mention'},true)).body.error.data.reason,'setup_required');
assert.deepEqual((await call('events/unsubscribe',{},true)).body.result,{});
assert.equal((await call('unknown',{},true)).body.error.code,-32601);
assert.equal((await worker.fetch(new Request('https://probe.invalid/api/status'))).status,401);
assert.equal((await worker.fetch(new Request('https://probe.invalid/'))).status,401);
const page = await worker.fetch(new Request('https://probe.invalid/',{headers:{'oai-authenticated-user-id':'local-unit-test-user'}}));
assert.equal(page.status,503); assert.match(await page.text(),/storage is not ready/);
assert.equal((await worker.fetch(new Request('https://probe.invalid/mcp'))).status,405);
assert.equal((await worker.fetch(new Request('https://probe.invalid/mcp',{method:'POST',headers:{'content-type':'application/json'},body:'bad'}))).status,400);
assert.equal((await worker.fetch(new Request('https://probe.invalid/mcp',{method:'POST',headers:{'content-type':'application/json','origin':'https://untrusted.invalid'},body:'{}'}))).status,403);
assert.equal(calls,0);
console.log('PASS: discovery, four legacy initializations, read-only tool, planned event schema, identity gates, disabled subscriptions, page, input errors, cross-origin rejection; no outbound calls. Local request identity is a unit-test fixture, not live OAuth verification.');

// MCP2 stable schema requires resultType on all results and cache hints on catalogs.
for (const method of ['server/discover','tools/list','resources/list','resources/templates/list','prompts/list','events/list']) {
  const result = (await call(method,{_meta:{'io.modelcontextprotocol/protocolVersion':'2026-07-28','io.modelcontextprotocol/clientCapabilities':{}}},true,'2026-07-28')).body.result;
  assert.equal(result.resultType,'complete'); assert.equal(result.ttlMs,0); assert.equal(result.cacheScope,'private');
  assert.equal(result._meta['io.modelcontextprotocol/serverInfo'].name,'zulip-dot-connection-test');
}
const modernCall = await call('tools/call',{name:'zulip_bridge_status'},true,'2026-07-28');
assert.equal(modernCall.body.result.resultType,'complete');
assert.equal(modernCall.body.result.structuredContent.state,'setup_required');
assert.equal((await call('events/unsubscribe',{},true,'2026-07-28')).body.result.resultType,'complete');
assert.equal((await call('events/subscribe',{},true,'2026-07-28')).body.error.data.reason,'setup_required');
assert.equal((await call('tools/list')).body.result.resultType,undefined);
assert.equal((await call('initialize',{protocolVersion:'2025-11-25'},true,'2026-07-28')).body.result.resultType,undefined);
assert.equal(calls,0);
console.log('PASS: MCP2 result discriminators, private cache hints, serverInfo metadata and legacy response preservation.');
