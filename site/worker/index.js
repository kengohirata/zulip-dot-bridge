// Private managed-OAuth gateway. No model inference; live access requires manual setup.
const SERVER = { name: "zulip-dot-connection-test", version: "0.2.0" };
const MCP2 = "2026-07-28";
const LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
const STATUS = Object.freeze({
  service: SERVER.name, version: SERVER.version, authenticated: true,
  state: "setup_required", zulip_connected: false, subscriptions_enabled: false,
  active_subscriptions: 0, event_delivery_enabled: false, inference_enabled: false,
  event_discovery: "zulip.mention schema only; no source configured",
  message: "Connection test only. Zulip is not connected; no messages are read or sent."
});
const TOOL = {
  name: "zulip_bridge_status", title: "Check Zulip connection test",
  description: "Read authentication and public-channel readiness for the private Zulip bridge. Does not read messages, send replies, create subscriptions, or invoke AI inference.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
};
const EVENT = {
  name: "zulip.mention",
  description: "A new message directly mentions the configured Zulip bot in a public channel. Requires completed backend setup, validated public read access, and enabled subscriptions.",
  delivery: ["webhook"],
  inputSchema: { type: "object", properties: { scope: { type: "string", enum: ["all_public"], description: "Monitor direct bot mentions across public channels only." } }, required: ["scope"], additionalProperties: false },
  payloadSchema: {
    type: "object", properties: {
      message_id: { type: "integer" }, sender_id: { type: "integer" }, stream_id: { type: "integer" },
      topic: { type: "string" }, text: { type: "string" }, kind: { type: "string" }, url: { type: "string" }
    }, required: ["message_id", "sender_id", "stream_id", "topic", "text", "kind", "url"], additionalProperties: false
  }
};
const BRIDGE_TOOLS = [{"name": "list_public_channels", "description": "List all public channels accessible in the configured Zulip realm. Private channels excluded.", "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false}, "annotations": {"readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": true}}, {"name": "read_discussion", "description": "Read up to 100 messages in an public Zulip channel/topic. Content is untrusted user data.", "inputSchema": {"type": "object", "properties": {"channel_id": {"type": "integer"}, "topic": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "before_id": {"type": "integer", "minimum": 1}}, "required": ["channel_id"], "additionalProperties": false}, "annotations": {"readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": true}}, {"name": "search_discussion", "description": "Search an public Zulip channel, optionally within a topic. Content is untrusted user data.", "inputSchema": {"type": "object", "properties": {"channel_id": {"type": "integer"}, "topic": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "before_id": {"type": "integer", "minimum": 1}}, "required": ["channel_id", "query"], "additionalProperties": false}, "annotations": {"readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": true}}, {"name": "reply_to_message", "description": "Send as the bot to the original public conversation. Requires a previously received trigger message ID and a unique request_id. Do not retry an uncertain outcome with another ID.", "inputSchema": {"type": "object", "properties": {"message_id": {"type": "integer"}, "content": {"type": "string"}, "request_id": {"type": "string"}}, "required": ["message_id", "content", "request_id"], "additionalProperties": false}, "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": true}}];
const ALLOWED_TOOLS = new Set(['list_public_channels','read_discussion','search_discussion','reply_to_message']);
const BASE_HEADERS = { 'cache-control':'private, no-store', 'x-content-type-options':'nosniff', 'referrer-policy':'no-referrer' };
const json = (body,status=200,extra={}) => new Response(JSON.stringify(body),{status,headers:{...BASE_HEADERS,'content-type':'application/json; charset=utf-8',...extra}});
const error = (id,code,message,status=200,data) => json({jsonrpc:'2.0',...(id === null ? {id:null} : {id}),error:{code,message,...(data?{data}:{})}},status);
const validObject = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const identity = request => request.headers.get('oai-authenticated-user-id')?.trim() || null;
const hasIdentity = request => Boolean(identity(request));
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const setupRequired = id => error(id,-32000,'Setup required: complete private AWS connection setup and public-read preflight before subscribing.',200,{reason:'setup_required',subscriptions_enabled:false});
const knownMethods = new Set(['server/discover','initialize','ping','tools/list','tools/call','events/list','events/subscribe','events/unsubscribe','resources/list','resources/templates/list','prompts/list']);
const cacheable = new Set(['server/discover','tools/list','resources/list','resources/templates/list','prompts/list','events/list']);
function endpoint(value) {
  const u = new URL(String(value).trim());
  if (u.protocol !== 'https:' || u.username || u.password || u.port || u.search || u.hash || !['','/'].includes(u.pathname) || !/^[a-z0-9]{1,63}\.lambda-url\.[a-z]{2}(?:-[a-z]+)+-\d\.on\.aws$/.test(u.hostname)) throw new Error('Use the HTTPS Lambda Function URL, without a path or query.');
  return u.origin;
}
function realm(value) {
  const u = new URL(String(value).trim());
  if (u.protocol !== 'https:' || u.username || u.password || u.port || u.search || u.hash || !['','/'].includes(u.pathname)) throw new Error('Use the HTTPS Zulip realm URL, without a path or query.');
  return u.origin;
}
async function readBounded(response,limit=262144) {
  if (Number(response.headers.get('content-length')||0)>limit) throw new Error('Oversized payload');
  const reader=response.body?.getReader(); if(!reader) return '';
  let length=0; const chunks=[];
  try { while(true){const {done,value}=await reader.read();if(done)break;length+=value.byteLength;if(length>limit)throw new Error('Oversized payload');chunks.push(value);} }
  catch(e){await reader.cancel();throw e;}
  const bytes=new Uint8Array(length);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.byteLength;}return new TextDecoder().decode(bytes);
}
async function configFor(env,owner) {
  if (!env?.DB) return null;
  const row=await env.DB.prepare('SELECT owner_id, endpoint, expected_realm, proxy_secret, updated_at FROM bridge_config WHERE id = 1').first();
  if(row && row.owner_id !== owner) throw new Error('OWNER_ONLY');
  return row;
}
class VerificationError extends Error { constructor(code){super(code);this.code=code;} }
async function upstream(config,path,body,request) {
  const origin=endpoint(config.endpoint);
  const h={'authorization':'Bearer '+config.proxy_secret,'accept':'application/json','content-type':'application/json'};
  for(const name of ['mcp-protocol-version','mcp-method','mcp-name']) {const v=request?.headers.get(name);if(v)h[name]=v;}
  let response;try{response=await fetch(origin+path,{method:body?'POST':'GET',headers:h,...(body?{body:JSON.stringify(body)}:{}),redirect:'manual',signal:AbortSignal.timeout(20000)});}catch{throw new VerificationError('NETWORK');}
  if(!response.ok)throw new VerificationError('HTTP_'+response.status);
  if(!response.headers.get('content-type')?.toLowerCase().includes('application/json'))throw new VerificationError('CONTENT_TYPE');
  let text;try{text=await readBounded(response);}catch{throw new VerificationError('BODY_READ');}
  // Do not reflect a configured credential, even if an upstream misbehaves.
  let parsed;try{parsed=JSON.parse(text);}catch{throw new VerificationError('INVALID_JSON');}
  return redactSecret(parsed,config.proxy_secret);
}
function redactSecret(value,secret) {
  if(typeof value==='string')return value.split(secret).join('[redacted]');
  if(Array.isArray(value))return value.map(item=>redactSecret(item,secret));
  if(validObject(value))return Object.fromEntries(Object.entries(value).map(([key,item])=>[key.split(secret).join('[redacted]'),redactSecret(item,secret)]));
  return value;
}
async function preflight(config) {
  const status=await upstream(config,'/status');
  if(status.configured!==true || status.public_read_preflight_ok!==true || realm(status.zulip_realm)!==config.expected_realm)throw new VerificationError('PREFLIGHT');
  return {configured:true,zulip_realm:config.expected_realm,public_read_preflight_ok:true,subscriptions_enabled:status.subscriptions_enabled===true,webhook_enabled:status.webhook_enabled===true};
}
const localStatus = (extra={}) => ({...STATUS,version:SERVER.version,...extra});
function responseEnvelope(id,result,modern,method){return json({jsonrpc:'2.0',id,result:modern?{...result,resultType:'complete',...(cacheable.has(method)?{ttlMs:0,cacheScope:'private'}:{}),_meta:{'io.modelcontextprotocol/serverInfo':SERVER}}:result});}
async function mcp(request,env) {
  if(request.method!=='POST')return json({error:'Use POST for this stateless MCP endpoint.'},405,{allow:'POST'});
  const origin=request.headers.get('origin');if(origin&&origin!==new URL(request.url).origin)return json({error:'Cross-origin browser requests are not allowed.'},403);
  if(request.headers.get('content-type')?.split(';')[0].trim().toLowerCase()!=='application/json')return json({error:'Expected application/json.'},415);
  let body;try{body=JSON.parse(await readBounded(request,65536));}catch{return error(null,-32700,'Invalid or oversized JSON request.',400);}
  if(!validObject(body)||body.jsonrpc!=='2.0'||typeof body.method!=='string'||(body.id!==undefined&&typeof body.id!=='string'&&typeof body.id!=='number'&&body.id!==null))return error(null,-32600,'Invalid request',400);
  const id=body.id??null,params=body.params??{};if(!validObject(params))return error(id,-32602,'Invalid params');
  if(body.id===undefined)return new Response(null,{status:202,headers:BASE_HEADERS});
  const modern=body.method!=='initialize'&&(body.method==='server/discover'||request.headers.get('mcp-protocol-version')===MCP2||params._meta?.['io.modelcontextprotocol/protocolVersion']===MCP2);
  console.info(JSON.stringify({event:'mcp_bridge_request',method:knownMethods.has(body.method)?body.method:'unknown',modern,authenticated:hasIdentity(request)}));
  const respond=result=>responseEnvelope(id,result,modern,body.method);
  if(!knownMethods.has(body.method))return error(id,-32601,'Method not found');
  if(body.method==='server/discover')return respond({supportedVersions:[MCP2],capabilities:{tools:{},events:{}}});
  if(body.method==='initialize')return respond({protocolVersion:LEGACY.includes(params.protocolVersion)?params.protocolVersion:LEGACY[0],capabilities:{tools:{}},serverInfo:SERVER});
  if(body.method==='ping')return respond({});
  if(body.method==='resources/list')return respond({resources:[]});
  if(body.method==='resources/templates/list')return respond({resourceTemplates:[]});
  if(body.method==='prompts/list')return respond({prompts:[]});
  if(body.method==='tools/list')return respond({tools:[TOOL,...BRIDGE_TOOLS]});
  // Unauthenticated discovery is generic and never reads the owner's config.
  if(!hasIdentity(request)){
    if(body.method==='tools/list')return respond({tools:[TOOL]});
    if(body.method==='events/list')return respond({events:[EVENT]});
    return error(id,-32001,'Authenticated user required. Connect this private plugin with ChatGPT.',401);
  }
  let config;try{config=await configFor(env,identity(request));}catch(e){return error(id,-32002,e.message==='OWNER_ONLY'?'Only the configured owner can use this bridge.':'Private connection storage is temporarily unavailable.',e.message==='OWNER_ONLY'?403:503);}
  if(body.method==='tools/call'&&params.name===TOOL.name){
    const args=params.arguments??{};if(!validObject(args)||Object.keys(args).length)return error(id,-32602,'This status tool accepts no arguments.');
    let status=localStatus({configured:false,storage_available:Boolean(env?.DB)});
    if(config){try{const p=await preflight(config);status=localStatus({...p,state:'connected',zulip_connected:true,active_subscriptions:null,event_discovery:'zulip.mention across all public channels',event_delivery_enabled:p.subscriptions_enabled&&p.webhook_enabled,message:'AWS backend authenticated and public-channel access validated. Event automation setup is separate.'});}catch{status=localStatus({configured:true,state:'backend_unavailable',zulip_connected:false,message:'The saved AWS backend could not pass authentication and public-read checks. Check its configuration.'});}}
    return respond({content:[{type:'text',text:JSON.stringify(status)}],structuredContent:status,isError:false});
  }
  if(body.method==='events/list')return respond({events:[EVENT]});
  if(!config){if(body.method==='tools/list')return respond({tools:[TOOL]});if(body.method==='events/unsubscribe')return respond({});return setupRequired(id);}
  if(body.method==='events/subscribe'||body.method==='events/unsubscribe'){
    const a=params.arguments;if(params.name!=='zulip.mention'||!validObject(a)||Object.keys(a).length!==1||a.scope!=='all_public')return error(id,-32602,'Expected zulip.mention with scope all_public.');
  }
  if(body.method==='tools/call'&&!ALLOWED_TOOLS.has(params.name))return error(id,-32602,'Unknown tool');
  if(!['tools/list','tools/call','events/subscribe','events/unsubscribe'].includes(body.method))return error(id,-32601,'Method not found');
  try{
    if(body.method!=='events/unsubscribe'){const p=await preflight(config);if(body.method==='events/subscribe'&&!p.subscriptions_enabled)return setupRequired(id);}
    const r=await upstream(config,'/mcp',body,request);
    if(r.jsonrpc!=='2.0'||r.id!==id||(!validObject(r.result)&&!validObject(r.error)))throw new Error('INVALID_BACKEND_RESPONSE');
    if(r.error)return json(r);
    if(body.method==='tools/list'){
      if(!Array.isArray(r.result.tools))throw new Error('INVALID_BACKEND_RESPONSE');
      const tools=r.result.tools.filter(t=>validObject(t)&&ALLOWED_TOOLS.has(t.name));
      return respond({tools:[TOOL,...tools]});
    }
    return respond(r.result);
  }catch{return error(id,-32002,'The AWS backend could not complete this request. Check authentication and public-channel access.',200,{reason:'backend_unavailable'});}
}
const STYLE = `:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#142c30;background:#edf4f5;color-scheme:light;font-synthesis:none}*{box-sizing:border-box}body{margin:0;min-height:100svh;display:grid;place-items:center;padding:1.3rem}main{max-width:38rem;width:100%;padding:clamp(1.3rem,4vw,2.5rem);background:white;border-top:.4rem solid #0f766e;border-radius:.6rem;box-shadow:0 12px 42px #102c3010}h1{font-size:2rem;line-height:1.15;letter-spacing:-.03em;margin:.4rem 0 1rem}p{line-height:1.6;font-size:1rem}.eyebrow,.help{font-size:.875rem;color:#43636b}.badge{display:inline-block;background:#fff2cf;color:#735100;padding:.4rem .7rem;border-radius:.3rem;font-weight:650;font-size:1rem}form{margin-top:1.6rem}label{display:block;font-weight:650;margin:1.2rem 0 .4rem;font-size:1rem}input:not([type=checkbox]){width:100%;border:1px solid #718f94;border-radius:.35rem;padding:.75rem;font:inherit;font-size:1rem}input:focus-visible,button:focus-visible{outline:3px solid #21a79a;outline-offset:3px}.consent{display:flex;gap:.7rem;align-items:flex-start;font-weight:400;line-height:1.5}.consent input{margin-top:.3rem;width:1.1rem;height:1.1rem;flex-shrink:0}button{border:0;border-radius:.35rem;padding:.85rem 1.1rem;background:#0f766e;color:white;font:inherit;font-weight:650;cursor:pointer;width:100%;margin-top:.7rem}button:disabled{opacity:.6;cursor:wait}.notice{padding:.9rem;background:#f1f6f6;border-radius:.35rem}.error{color:#a11b24}footer{margin-top:1.5rem;padding-top:1rem;border-top:1px solid #d8e5e7;line-height:1.6;font-size:.875rem;color:#43636b}code{overflow-wrap:anywhere}#result{min-height:1.5rem;line-height:1.5}a{color:#08665e;text-underline-offset:.15em}`;
const favicon=encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#0f766e"/><path d="M8 11h16L8 21h16" fill="none" stroke="white" stroke-width="3" stroke-linejoin="round"/></svg>');
function page(content,nonce='',script='') {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><meta name="description" content="Private setup for the Zulip connection to dot."><title>Zulip · dot bridge</title><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,${favicon}"><style>${STYLE}</style></head><body><main><div class="eyebrow">ZULIP + dot</div>${content}</main>${script?`<script nonce="${nonce}">${script}</script>`:''}</body></html>`;
}
const randomToken=()=>Array.from(crypto.getRandomValues(new Uint8Array(32)),v=>v.toString(16).padStart(2,'0')).join('');
function html(content,status=200,nonce='',extra={}){return new Response(content,{status,headers:{...BASE_HEADERS,'content-type':'text/html; charset=utf-8','content-security-policy':`default-src 'none'; style-src 'unsafe-inline'; script-src ${nonce?"'nonce-"+nonce+"'":"'none'"}; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'self'`,...extra}});}
async function setupPage(request,env) {
  if(!hasIdentity(request))return html(page('<h1>Sign-in required</h1><p>Open this private site from the same ChatGPT account that owns the Zulip plugin.</p>'),401);
  if(!env?.DB)return html(page('<h1>Setup temporarily unavailable</h1><p>Private connection storage is not ready. Please return to dot.</p>'),503);
  let c;try{c=await configFor(env,identity(request));}catch(e){return html(page(`<h1>${e.message==='OWNER_ONLY'?'Owner access required':'Setup temporarily unavailable'}</h1><p>Return to the dot conversation for help.</p>`),e.message==='OWNER_ONLY'?403:503);}
  const csrf=randomToken(),nonce=randomToken();
  const script=`const form=document.getElementById('setup');form.addEventListener('submit',async(event)=>{event.preventDefault();const button=form.querySelector('button');const result=document.getElementById('result');button.disabled=true;result.className='';result.textContent='Checking the AWS connection and public-channel access…';const secret=form.elements.proxy_secret;try{const response=await fetch('/api/config',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':${JSON.stringify(csrf)}},body:JSON.stringify({endpoint:form.elements.endpoint.value,expected_realm:form.elements.expected_realm.value,proxy_secret:secret.value,consent:form.elements.consent.checked})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to save the connection.');secret.value='';result.textContent='Connection saved and public-channel access verified. '+(data.subscriptions_enabled&&data.webhook_enabled?'Return to dot to finish the mention automation.':'Event delivery remains disabled until setup is complete.');document.getElementById('state').textContent='Connection saved';}catch(error){result.className='error';result.textContent=error.message||'Unable to save the connection. Try again.';}finally{button.disabled=false;}});`;
  return html(page(`<h1>Connect your AWS bridge</h1><span class="badge" id="state">${c?'Connection saved':'Not connected'}</span><p>This connects your dot to the AWS backend for the Zulip realm you enter below.</p><form id="setup" autocomplete="off"><label for="endpoint">AWS Lambda Function URL</label><input id="endpoint" name="endpoint" type="url" required spellcheck="false" autocomplete="off" value="${esc(c?.endpoint)}" placeholder="https://….lambda-url.us-east-1.on.aws"><label for="expected_realm">Zulip realm</label><input id="expected_realm" name="expected_realm" type="url" required spellcheck="false" autocomplete="off" value="${esc(c?.expected_realm)}" placeholder="https://your-organization.zulipchat.com"><label for="proxy_secret">AWS proxy secret</label><input id="proxy_secret" name="proxy_secret" type="password" required minlength="32" maxlength="512" autocomplete="new-password" spellcheck="false"><p class="help">Use the separate proxy secret from AWS setup. Keep your Zulip bot API key in AWS only. Saved secrets are never shown here.</p><label class="consent"><input name="consent" type="checkbox" required><span>I authorize this private site to save the proxy secret and use this AWS backend for public-channel reads and replies to direct mentions. A live mention automation is set up separately with dot.</span></label><button type="submit">Save and verify connection</button><p id="result" role="status" aria-live="polite"></p></form><footer>The secret is stored server-side and bound to your signed-in account. Saving first verifies the bot’s public-channel access. No connection test runs before you submit. This bridge makes no AI API calls; AWS usage may incur hosting charges.</footer>`,nonce,script),200,nonce,{'set-cookie':`__Host-bridge_csrf=${csrf}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=900`});
}
async function saveConfig(request,env) {
  const owner=identity(request);if(!owner)return json({error:'Sign in with the owning ChatGPT account.'},401);
  if(!env?.DB)return json({error:'Private connection storage is unavailable. Try again later.'},503);
  const origin=new URL(request.url).origin;
  const token=request.headers.get('x-csrf-token');const cookie=request.headers.get('cookie')?.split(';').map(v=>v.trim()).find(v=>v.startsWith('__Host-bridge_csrf='))?.slice('__Host-bridge_csrf='.length);
  if(request.headers.get('origin')!==origin||!token||!/^[a-f0-9]{64}$/.test(token)||token!==cookie)return json({error:'This setup session expired. Reload the page and try again.'},403);
  if(request.headers.get('content-type')?.split(';')[0].trim().toLowerCase()!=='application/json')return json({error:'Expected JSON.'},415);
  let submitted;try{submitted=JSON.parse(await readBounded(request,8192));}catch{return json({error:'Invalid setup request.'},400);}
  if(!validObject(submitted)||submitted.consent!==true)return json({error:'Confirm that you authorize saving and using this private connection.'},400);
  let candidate;try{candidate={endpoint:endpoint(submitted.endpoint),expected_realm:realm(submitted.expected_realm),proxy_secret:String(submitted.proxy_secret||'')};if(!/^[\x21-\x7e]{32,512}$/.test(candidate.proxy_secret))throw new Error('Use a proxy secret of at least 32 characters with no spaces.');}catch(e){return json({error:e.message.startsWith('Use ')?e.message:'Enter valid HTTPS setup URLs.'},400);}
  try{await configFor(env,owner);}catch(e){return json({error:e.message==='OWNER_ONLY'?'Only the configured owner may change this connection.':'Private connection storage is unavailable.'},e.message==='OWNER_ONLY'?403:503);}
  let ready;try{ready=await preflight(candidate);}catch(e){const code=e instanceof VerificationError&&/^(NETWORK|HTTP_[1-5][0-9]{2}|CONTENT_TYPE|INVALID_JSON|BODY_READ|PREFLIGHT)$/.test(e.code)?e.code:'CHECK_FAILED';console.log(JSON.stringify({event:'bridge_setup_failed',code}));return json({error:'AWS verification failed ('+code+'). Nothing was saved.'},422);}
  try{
    const result=await env.DB.prepare('INSERT INTO bridge_config (id, owner_id, endpoint, expected_realm, proxy_secret, updated_at) VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET endpoint = excluded.endpoint, expected_realm = excluded.expected_realm, proxy_secret = excluded.proxy_secret, updated_at = excluded.updated_at WHERE bridge_config.owner_id = excluded.owner_id').bind(owner,candidate.endpoint,candidate.expected_realm,candidate.proxy_secret,new Date().toISOString()).run();
    if(result.meta?.changes!==1)return json({error:'Only the configured owner may change this connection.'},403);
  }catch{return json({error:'The connection verified, but storage failed. Nothing was confirmed saved; keep the form open and try again.'},503);}
  return json({saved:true,configured:true,zulip_realm:candidate.expected_realm,public_read_preflight_ok:true,subscriptions_enabled:ready.subscriptions_enabled,webhook_enabled:ready.webhook_enabled});
}
export default {
  async fetch(request,env) {
    const path=new URL(request.url).pathname;
    try{
      if(path==='/mcp')return await mcp(request,env);
      if((path==='/'||path==='/setup')&&request.method==='GET')return await setupPage(request,env);
      if(path==='/api/config'&&request.method==='POST')return await saveConfig(request,env);
      if(path==='/api/status'&&request.method==='GET'){
        if(!hasIdentity(request))return json({error:'Authenticated user required'},401);
        const c=await configFor(env,identity(request));return json({configured:Boolean(c),state:c?'saved':'setup_required',...(c?{zulip_realm:c.expected_realm}:{}),subscriptions_verified:false});
      }
      return json({error:'Not found'},404);
    }catch(e){return json({error:e.message==='OWNER_ONLY'?'Owner access required':'The private connection is temporarily unavailable.'},e.message==='OWNER_ONLY'?403:503);}
  }
};
