import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, validateResponse } from '../src/api.js';

test('private requests include cookies and mutation CSRF, without saving a pairing code', async () => {
  const calls = [];
  const api = createApi('http://localhost:8000', async (...args) => {
    calls.push(args);
    return { ok: true, json: async () => ({ message: 'done' }) };
  });
  api.setCsrf('synthetic-csrf');
  await api.request('/feedback', { method: 'POST', body: { email_id: 'synthetic', label: 'SPAM' } });
  assert.equal(calls[0][1].credentials, 'include');
  assert.equal(calls[0][1].headers['X-CSRF-Token'], 'synthetic-csrf');
  assert.equal(JSON.parse(calls[0][1].body).email_id, 'synthetic');
  api.setCsrf('');
  await api.request('/logout', { method: 'POST' });
  assert.equal(calls[1][1].headers['X-CSRF-Token'], '');
});

test('failed actions show server errors instead of reporting success', async () => {
  const api = createApi('', async () => ({ ok: false, status: 503, json: async () => ({ detail: 'Deletion did not finish.' }) }));
  await assert.rejects(api.request('/account-data', { method: 'DELETE' }), error => error.status === 503 && error.message === 'Deletion did not finish.');
});

test('validation errors are turned into readable text', async () => {
  const api = createApi('', async () => ({ ok: false, status: 422, json: async () => ({ detail: [{ type: 'missing' }] }) }));
  await assert.rejects(api.request('/feedback', { method: 'POST' }), /check your input/);
});


test('telemetry accepts disabled local providers only with a consistent boolean mode',()=>{
  const data={account_id:'synthetic@example.test',generation:1,totals:{saved:0,completed:0,labelled:0,corrected:0,confirmed:0,feedback_events:0,prediction_attempts:0},local_model:{ready:false},classification_timing:{sample_count:0,mean_ms:null},providers:{gemini:'disabled_local_only',groq:'disabled_local_only'},mode:{local_only:true}};
  assert.doesNotThrow(()=>validateResponse('/telemetry','GET',data));
  assert.throws(()=>validateResponse('/telemetry','GET',{...data,mode:{local_only:'true'}}));
  assert.throws(()=>validateResponse('/telemetry','GET',{...data,mode:{local_only:false}}));
  assert.throws(()=>validateResponse('/telemetry','GET',{...data,providers:{gemini:'configured_unverified',groq:'disabled_local_only'}}));
});
