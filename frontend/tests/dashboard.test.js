import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, validateResponse } from '../src/api.js';
import { backoffDelay, createPoller } from '../src/polling.js';
import { assertAccount, connectivityProblem, disconnectReason, supervisorAlert, effectiveCategory, formatTenthsAsSeconds, loadDashboard, localModelSummary, millisecondsToTenthsUp, shouldLoadOlderMail, workerState } from '../src/dashboard.js';
const deferred = () => { let resolve, reject; const promise = new Promise((yes,no) => { resolve=yes; reject=no; }); return { promise, resolve, reject }; };
const flush = () => new Promise(resolve => setImmediate(resolve));

test('successful HTML or nonobject JSON is rejected instead of showing success', async () => {
  for (const value of [null,[],false]) {
    const api=createApi('',async()=>({ok:true,json:async()=>value}));
    await assert.rejects(api.request('/feedback',{method:'POST'}),/Unexpected server response/);
  }
  const api=createApi('',async()=>({ok:true,json:async()=>{throw new SyntaxError();}}));
  await assert.rejects(api.request('/status'),/Unexpected server response/);
});
test('non-JSON HTTP errors preserve the status for session handling', async () => {
  for (const status of [401,404,500]) {
    const api=createApi('',async()=>({ok:false,status,json:async()=>{throw new SyntaxError();}}));
    await assert.rejects(api.request('/status'),error=>error.status===status);
  }
});
test('malformed email-page shapes and invalid categories are rejected', () => {
  const page={account_id:'a',generation:1,emails:[],total:0,limit:20,offset:0,has_more:false,
    search_mode:'text',semantic_available:false,semantic_index:{pending:0,indexed:0,failed:0}};
  validateResponse('/emails?limit=20','GET',page);
  validateResponse('/emails','GET',{...page,emails:[{id:'x',subject:'s',sender:'s',created_at:'date',effective_category:null,review_reason:{code:'low_confidence',message:'The model was not confident enough to choose a category.'}}]});
  for (const invalid of [{...page,total:'0'},{...page,emails:{}},{...page,generation:undefined},{...page,emails:[{id:'x',subject:'s',sender:'s',created_at:'date',effective_category:'BLOCKED'}]}]) assert.throws(()=>validateResponse('/emails','GET',invalid),/Unexpected/);
});
test('malformed mutation job acknowledgement is rejected', () => {
  assert.throws(()=>validateResponse('/authenticate','POST',{message:'ok'}),/Unexpected/);
});
test('a network outage has an actionable message', async () => {
  const api=createApi('',async()=>{throw new TypeError('fetch failed');});
  await assert.rejects(api.request('/session'),/Check that the local API is running/);
});
test('abort remains cancellation even when transport returns a late result', async () => {
  const response=deferred(), controller=new AbortController();
  const api=createApi('',()=>response.promise);
  const run=api.request('/feedback',{method:'POST',signal:controller.signal});
  controller.abort();response.resolve({ok:true,json:async()=>({message:'late'})});
  await assert.rejects(run,error=>error.name==='AbortError');
});
test('timeout asks user to check saved state before repeating a mutation', async () => {
  const api=createApi('',(_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')))));
  await assert.rejects(api.request('/feedback',{method:'POST',timeout:5}),/whether your action was saved/);
});
test('polling waits for completion before scheduling another run', async () => {
  const response=deferred(), schedules=[], delivered=[];
  const poller=createPoller(()=>response.promise,{onData:value=>delivered.push(value),schedule:(callback,delay)=>{schedules.push({callback,delay});return 1;},cancel:()=>{}});
  const run=poller.start();await flush();
  assert.equal(schedules.length,0);
  response.resolve('done');await run;
  assert.deepEqual(delivered,['done']);assert.equal(schedules.length,1);assert.equal(schedules[0].delay,5000);poller.stop();
});
test('repeated refreshes do not overlap active requests', async () => {
  const first=deferred(),second=deferred();let calls=0,active=0,max=0;
  const poller=createPoller(async()=>{calls++;active++;max=Math.max(max,active);const value=await(calls===1?first.promise:second.promise);active--;return value;},{schedule:()=>1,cancel:()=>{}});
  const run=poller.start();await flush();poller.refresh();poller.refresh();
  assert.equal(calls,1);first.resolve('one');await run;await flush();assert.equal(calls,2);
  second.resolve('two');await flush();assert.equal(max,1);poller.stop();
});
test('stopped polling aborts and cannot deliver a late result or error', async () => {
  for (const fails of [false,true]) {
    const response=deferred(),delivered=[],errors=[];let signal;
    const poller=createPoller(value=>{signal=value;return response.promise;},{onData:value=>delivered.push(value),onError:error=>errors.push(error),schedule:()=>1,cancel:()=>{}});
    const run=poller.start();await flush();poller.stop();assert.equal(signal.aborted,true);
    if(fails)response.reject(new Error('old'));else response.resolve('old');await run;
    assert.deepEqual(delivered,[]);assert.deepEqual(errors,[]);
  }
});
test('restarting polling discards old-account response before loading new data', async () => {
  const old=deferred(),delivered=[];let calls=0;
  const poller=createPoller(()=>++calls===1?old.promise:Promise.resolve('new'),{onData:value=>delivered.push(value),schedule:()=>1,cancel:()=>{}});
  const run=poller.start();await flush();poller.stop();poller.start();old.resolve('old');await run;await flush();
  assert.deepEqual(delivered,['new']);poller.stop();
});
test('polling failures remain errors and still schedule later checks', async () => {
  const errors=[],schedules=[];
  const poller=createPoller(()=>Promise.reject(new Error('offline')),{onError:error=>errors.push(error.message),schedule:callback=>{schedules.push(callback);return 1;},cancel:()=>{}});
  await poller.start();assert.deepEqual(errors,['offline']);assert.equal(schedules.length,1);poller.stop();
});
test('mixed account generations and account identities are rejected', () => {
  const session={email:'a',generation:3};
  assertAccount(session,{account_id:'a',generation:3});
  for(const data of [{account_id:'a',generation:2},{account_id:'b',generation:3}])assert.throws(()=>assertAccount(session,data),error=>error.status===409);
});
test('email lanes use effective category rather than original prediction', () => {
  assert.equal(effectiveCategory({prediction:'IMPORTANT',effective_category:'SPAM'}),'SPAM');
  assert.equal(effectiveCategory({prediction:'IMPORTANT',effective_category:null}),'NEEDS_REVIEW');
});
test('snapshot refresh timing is measured and disconnected snapshots skip mail', async () => {
  const calls=[];let ticks=0;
  const api={request:async path=>{calls.push(path);return path==='/session'?{connected:false,email:null,generation:2}:{account_id:null,generation:2};}};
  const data=await loadDashboard(api,{offset:0,search:''},new AbortController().signal,null,()=>++ticks*12);
  assert.equal(data.refreshMs,12);assert.equal(data.page,null);assert.equal(calls.some(path=>path.startsWith('/emails')),false);
});
test('a missing local session is opened automatically and then loaded', async () => {
  const calls=[];let csrf='';
  const api={
    setCsrf:value=>{csrf=value;},
    request:async(path,options={})=>{
      calls.push([path,options.method||'GET']);
      if(path==='/session' && options.method!=='POST' && !csrf){const error=new Error('missing');error.status=401;throw error;}
      if(path==='/session' && options.method==='POST')return {csrf_token:'synthetic-csrf'};
      if(path==='/session')return {connected:false,email:null,generation:2,csrf_token:csrf};
      return {account_id:null,generation:2};
    },
  };
  const data=await loadDashboard(api,{offset:0,search:''},new AbortController().signal);
  assert.equal(data.session.connected,false);
  assert.equal(csrf,'synthetic-csrf');
  assert.deepEqual(calls.slice(0,3),[['/session','GET'],['/session','POST'],['/session','GET']]);
});
test('snapshot loader refuses to publish switched-account email data', async () => {
  const api={request:async path=>path==='/session'?{connected:true,email:'a',generation:2}:{account_id:path.startsWith('/emails')?'b':'a',generation:2}};
  await assert.rejects(loadDashboard(api,{offset:0,search:''},new AbortController().signal),error=>error.status===409);
});
test('worker status distinguishes paused, never observed, stale and processing', () => {
  const now=Date.now();const data={session:{connected:false},status:{poll_interval_seconds:60,worker:null}};
  assert.equal(workerState(data,now),'Paused');data.session.connected=true;assert.equal(workerState(data,now),'Not observed yet');
  data.status.worker={heartbeat_at:new Date(now-200000).toISOString()};assert.equal(workerState(data,now),'Stale heartbeat');
  data.status.worker.heartbeat_at=new Date(now).toISOString();data.status.is_polling=true;assert.equal(workerState(data,now),'Processing');
});

test('a success response without a mutation acknowledgement is rejected', () => {
  assert.throws(()=>validateResponse('/feedback','POST',{}),/Unexpected/);
});
test('one snapshot failure cancels its sibling reads', async () => {
  const signals=[];
  const api={request:async(path,{signal})=>{
    signals.push(signal);
    if(path==='/session')return {connected:true,email:'a',generation:1};
    if(path==='/status')throw new Error('offline');
    return {account_id:'a',generation:1};
  }};
  await assert.rejects(loadDashboard(api,{offset:0,search:''},new AbortController().signal),/offline/);
  assert.ok(signals.every(signal=>signal.aborted));
});

test('older mail loads automatically only near the end of the unfiltered inbox', () => {
  const status = { connected: true, live_monitoring: true, fetch_next_available: true };
  const browse = { search: '', category: '', emailId: '' };
  const at = offset => ({ offset, limit: 20, total: 100 });
  assert.equal(shouldLoadOlderMail(at(0), browse, status), false);
  assert.equal(shouldLoadOlderMail(at(40), browse, status), false);
  assert.equal(shouldLoadOlderMail(at(60), browse, status), true);   // second-to-last page
  assert.equal(shouldLoadOlderMail(at(80), browse, status), true);   // last page
  assert.equal(shouldLoadOlderMail(at(80), { ...browse, search: 'invoice' }, status), false);
  assert.equal(shouldLoadOlderMail(at(80), { ...browse, category: 'SPAM' }, status), false);
  assert.equal(shouldLoadOlderMail(at(80), browse, { ...status, fetch_next_available: false }), false);
  assert.equal(shouldLoadOlderMail(at(80), browse, { ...status, live_monitoring: false }), false);
  assert.equal(shouldLoadOlderMail(at(80), browse, { ...status, connected: false }), false);
});

test('average classification time is shown in seconds, one decimal, rounded up', () => {
  const seconds = ms => formatTenthsAsSeconds(millisecondsToTenthsUp(ms));
  assert.equal(seconds(1200), '1.2');
  assert.equal(seconds(1201), '1.3');          // always rounds up
  assert.equal(seconds(1249.9), '1.3');
  assert.equal(seconds(1200.0000001), '1.2');  // float noise is not a real extra millisecond
  assert.equal(seconds(95), '0.1');
  assert.equal(seconds(0), '0.0');
  assert.equal(seconds(14567), '14.6');
  assert.equal(seconds(null), '-');
  assert.equal(seconds(-5), '-');
});

test('health panel says the local shadow is off, and how to turn it on', () => {
  assert.deepEqual(localModelSummary({ ready: false, status: 'off' }),
    { value: 'Off · comparison shadow disabled', note: 'Enable with MAILMIND_SHADOW_MODEL_ENABLED=true' });
  assert.equal(localModelSummary({ ready: false, status: 'loading' }).value, 'Loading · cloud is handling requests');
  assert.equal(localModelSummary({ ready: true, status: 'ready', version: 'v2' }).note, 'v2');
  assert.equal(localModelSummary(null).value, 'Waiting for status');
});

test('connectivity alert: device offline first, then Gmail unreachable, else nothing', () => {
  const base = { browserOnline: true, connected: true, workerErrorCode: null };
  assert.equal(connectivityProblem(base), null);
  assert.equal(connectivityProblem({ ...base, browserOnline: false }), 'offline');
  assert.equal(connectivityProblem({ ...base, browserOnline: false, workerErrorCode: 'network_unavailable' }), 'offline');
  assert.equal(connectivityProblem({ ...base, workerErrorCode: 'network_unavailable' }), 'gmail');
  assert.equal(connectivityProblem({ ...base, workerErrorCode: 'gmail_temporarily_unavailable' }), 'gmail');
  assert.equal(connectivityProblem({ ...base, workerErrorCode: 'cycle_failed' }), null);   // not a connectivity problem
  assert.equal(connectivityProblem({ ...base, connected: false, workerErrorCode: 'network_unavailable' }), null);
});

test('refresh backs off exponentially after failures and resets after a success', async () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6].map(n => backoffDelay(n)), [5000, 10000, 20000, 40000, 60000, 60000]);
  const delays = []; let fail = 3;
  const poller = createPoller(async () => { if (fail-- > 0) throw new Error('down'); return 'ok'; },
    { onError: () => {}, schedule: (callback, delay) => { delays.push(delay); return 1; }, cancel: () => {} });
  await poller.start();
  for (let index = 0; index < 3; index++) await poller.refresh();
  assert.deepEqual(delays, [5000, 10000, 20000, 5000]);
});

test('supervisor status becomes a restart notice, then a warning before any shutdown', () => {
  assert.equal(supervisorAlert(null), null);
  assert.equal(supervisorAlert({ services: { api: { state: 'running' } }, shutdown_in_seconds: null }), null);
  assert.deepEqual(supervisorAlert({ services: { worker: { state: 'restarting', restarts: 2, retry_in_seconds: 4 } }, shutdown_in_seconds: null }),
    { kind: 'restarting', service: 'worker', label: 'the inbox worker', restarts: 2, retryIn: 4 });
  const loop = supervisorAlert({ services: { api: { state: 'crash_loop', restarts: 5 } }, shutdown_in_seconds: 42, message: 'api keeps failing' });
  assert.deepEqual(loop, { kind: 'crash_loop', service: 'api', label: "MailMind's server", restarts: 5, shutdownIn: 42, message: 'api keeps failing' });
  // A crash loop outranks an ordinary restart elsewhere.
  assert.equal(supervisorAlert({ services: { worker: { state: 'restarting', retry_in_seconds: 1 }, api: { state: 'crash_loop' } }, shutdown_in_seconds: 9 }).kind, 'crash_loop');
});

test('a pause caused by a rejected login is explained; other disconnects are not', () => {
  const snapshot = (connected, code, auth = false) => ({ session: { connected }, status: { auth_in_progress: auth, worker: code ? { last_error_code: code } : null } });
  assert.equal(disconnectReason(snapshot(false, 'gmail_unavailable')), 'login_rejected');
  assert.equal(disconnectReason(snapshot(true, 'gmail_unavailable')), null);
  assert.equal(disconnectReason(snapshot(false, 'gmail_unavailable', true)), null);
  assert.equal(disconnectReason(snapshot(false, 'network_unavailable')), null);
  assert.equal(disconnectReason(snapshot(false, null)), null);
  assert.equal(disconnectReason(null), null);
});
