import { test, expect } from '@playwright/test';

async function setup(page) {
  const state = { paired: true, connected: true, generation: 1, revision: 0, feedbackCalls: 0, feedbackError: 0, feedbackDelay: 0, emailsDelay: 0, emailsStarted: false, telemetryError: 0 };
  const stamp = '2026-09-18T12:00:00Z';
  state.rows = Array.from({length:24},(_,index)=>({ id:'synthetic-'+index, account_id:'interview@example.test', sender:'Synthetic Sender <sender@example.test>', subject:'Synthetic email '+index,
    created_at:stamp, body_snippet:'Only synthetic email text is used.', prediction:index===3?null:['IMPORTANT','UPDATES','SPAM'][index%3], local_prediction:'SPAM',
    effective_category:index===3?null:['IMPORTANT','UPDATES','SPAM'][index%3], human_label:null, feedback:null,
    latest_prediction:{category:index===3?null:['IMPORTANT','UPDATES','SPAM'][index%3],outcome:index===3?'UNAVAILABLE':'CLASSIFIED',source:'synthetic',local:{outcome:'ABSTAIN',category:null,model_version:'legacy-binary'}},
    processing:{status:index===3?'retry':'complete',stage:index===3?'classify':'complete',attempt_count:1,next_retry_at:1789750000,error_code:index===3?'provider_transient':null},notification:index===0?{status:'sent'}:null,
  }));
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin==='http://127.0.0.1:18106')return route.continue();
    if(url.origin!=='http://127.0.0.1:8000')return route.abort(); // Never contact real providers or font hosts.
    let status=200, data;
    const identity={account_id:'interview@example.test',generation:state.generation};
    const method=route.request().method(), path=url.pathname;
    if(path==='/session' && method==='POST'){state.paired=true;data={csrf_token:'synthetic-csrf'};}
    else if(!state.paired){status=401;data={detail:'Sign in to this local app first.'};}
    else if(path==='/session')data={email:identity.account_id,csrf_token:'synthetic-csrf',connected:state.connected,purge_pending:false,generation:state.generation};
    else if(path==='/status')data={...identity,connected:state.connected,is_polling:false,auth_in_progress:false,purge_pending:false,auto_mark_read:false,processing_counts:{complete:23,retry:1},notification_counts:{sent:1},ingestion_failures:[],poll_interval_seconds:60,worker_lease_seconds:90,worker:null,ingestion:null};
    else if(path==='/telemetry'){
      status=state.telemetryError||200;
      data=status!==200?{detail:'Synthetic telemetry outage.'}:{...identity,totals:{saved:24,completed:23,labelled:state.rows.filter(row=>row.human_label).length,corrected:state.rows.filter(row=>row.human_label&&row.human_label!==row.prediction).length,confirmed:state.rows.filter(row=>row.human_label&&row.human_label===row.prediction).length,feedback_events:state.revision,prediction_attempts:24},classification_timing:{mean_ms:95,sample_count:24},local_model:{ready:state.localReady||false,version:'legacy-binary',evaluation_scope:state.evaluationScope||null},mode:{local_only:state.localOnly||false},providers:{gemini:state.localOnly?'disabled_local_only':'configured_unverified',groq:state.localOnly?'disabled_local_only':'unconfigured'}};
    } else if(path==='/emails'){
      state.emailsStarted=true;
      const search=url.searchParams.get('search')||'',category=url.searchParams.get('category');
      const rows=state.rows.filter(row=>(row.subject+' '+row.sender+' '+row.body_snippet).toLowerCase().includes(search.toLowerCase())&&(!category||(row.effective_category||'NEEDS_REVIEW')===category));
      const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||20);
      data={...identity,emails:JSON.parse(JSON.stringify(rows.slice(offset,offset+limit))),total:rows.length,limit,offset,has_more:offset+limit<rows.length};
      if(state.emailsDelay)await new Promise(resolve=>setTimeout(resolve,state.emailsDelay));
    } else if(path==='/feedback'){
      state.feedbackCalls++;
      if(state.feedbackDelay)await new Promise(resolve=>setTimeout(resolve,state.feedbackDelay));
      if(state.feedbackError){status=state.feedbackError;data={detail:'Synthetic feedback save failed.'};}
      else{const body=route.request().postDataJSON(),row=state.rows.find(row=>row.id===body.email_id);row.human_label=body.label;row.effective_category=body.label;row.feedback={revision_id:++state.revision,label:body.label,indexing_state:'pending'};data={revision_id:state.revision,indexing_state:'pending',message:'Correction saved. Indexing will retry.'};status=202;}
    } else if(path.startsWith('/feedback/')&&method==='DELETE'){
      const row=state.rows.find(row=>row.id===decodeURIComponent(path.split('/')[2]));row.human_label=null;row.effective_category=row.prediction;row.feedback={revision_id:++state.revision,label:null,indexing_state:'pending'};data={revision_id:state.revision,indexing_state:'pending',message:'Feedback removed. Previous feedback history is kept. Index cleanup is queued.'};
    } else if(path.endsWith('/history'))data={...identity,feedback:[{revision_id:1,label:'UPDATES',indexing_state:'indexed',created_at:stamp}],processing:[{stage:'notify',outcome:'sent',created_at:stamp}]};
    else if(path==='/logout'){state.paired=false;state.connected=false;state.generation++;data={message:'Signed out. Saved mail is kept.'};}
    else if(path==='/process'){status=202;data={job_id:42,status:'queued',message:'Inbox processing queued.'};}
    else {status=404;data={detail:'Synthetic route not found.'};}
    try{await route.fulfill({status,contentType:'application/json',headers:{'Access-Control-Allow-Origin':'http://127.0.0.1:18106','Access-Control-Allow-Credentials':'true'},body:JSON.stringify(data)});}catch{ /* cancelled route during logout or teardown */ }
  });
  await page.goto('/');
  await expect(page.getByRole('button',{name:'Account options'})).toBeVisible();
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  return state;
}

test('stats are account totals and readiness is honest',async({page})=>{
  await setup(page);
  await expect(page.getByText('1–20 of 24 matching saved emails')).toBeVisible();
  await expect(page.getByRole('heading',{name:'Saved emails',exact:true}).locator('..')).toContainText('24');
  await expect(page.getByText('95 ms',{exact:true})).toBeVisible();
  await page.getByText('System health',{exact:false}).click();
  await expect(page.getByText('Unavailable for three-category inference',{exact:false})).toBeVisible();
  await expect(page.locator('.health-grid p').filter({hasText:'Gemini:'})).toContainText('Configured');
  await expect(page.getByText('240ms',{exact:true})).toHaveCount(0);
  await expect(page.getByText('2/2',{exact:true})).toHaveCount(0);
});
test('all four lanes show their correct styles and names',async({page})=>{
  await setup(page);
  for(const category of ['IMPORTANT','UPDATES','SPAM','Needs Review'])await expect(page.getByRole('region',{name:category+' emails',exact:true})).toBeVisible();
  const spam=page.getByRole('article',{name:'Synthetic email 2',exact:true}).locator('.category-badge');
  await expect(spam).toHaveCSS('color','rgb(180, 35, 60)');
  await expect(page.getByText('BLOCKED',{exact:true})).toHaveCount(0);
});
test('account menu and feedback editor work with keyboard and Escape',async({page})=>{
  await setup(page);const trigger=page.getByRole('button',{name:'Account options'});
  await trigger.focus();await page.keyboard.press('Enter');
  await expect(page.getByRole('button',{name:'Switch Google account'})).toBeFocused();
  await page.keyboard.press('ArrowDown');await expect(page.getByRole('button',{name:'Disconnect Google',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');await expect(trigger).toBeFocused();await expect(trigger).toHaveAttribute('aria-expanded','false');
  const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});await card.getByRole('button',{name:'Set label'}).click();
  await expect(card.getByLabel('Correct category')).toBeFocused();await page.keyboard.press('Escape');await expect(card.getByRole('button',{name:'Set label'})).toBeFocused();
});
test('feedback can be changed, moved between lanes and undone',async({page})=>{
  await setup(page);const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});
  await card.getByRole('button',{name:'Set label'}).click();await card.getByLabel('Correct category').selectOption('UPDATES');await card.getByRole('button',{name:'Save category'}).click();
  await expect(page.getByRole('region',{name:'UPDATES emails',exact:true}).getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  await card.getByText('Classification details').click();await expect(card).toContainText('Original primary: IMPORTANT');await expect(card).toContainText('Your feedback: UPDATES · index update pending');
  await card.getByRole('button',{name:'Edit label'}).click();await card.getByLabel('Correct category').selectOption('SPAM');await card.getByRole('button',{name:'Save category'}).click();
  await expect(page.getByRole('region',{name:'SPAM emails',exact:true}).getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  await card.getByRole('button',{name:'Undo'}).click();await expect(page.getByRole('region',{name:'IMPORTANT emails',exact:true}).getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
});
test('failed feedback stays visible without false success',async({page})=>{
  const state=await setup(page);state.feedbackError=500;
  const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});await card.getByRole('button',{name:'Set label'}).click();await card.getByRole('button',{name:'Save category'}).click();
  await expect(page.getByRole('alert')).toContainText('Synthetic feedback save failed.');
  await page.waitForTimeout(200);await expect(page.getByRole('alert')).toBeVisible();
  await expect(card.getByRole('button',{name:'Undo'})).toHaveCount(0);
  await page.getByRole('button',{name:'Retry refresh'}).click();await expect(page.getByRole('alert')).toHaveCount(0);
});
test('pending feedback blocks duplicate actions',async({page})=>{
  const state=await setup(page);state.feedbackDelay=500;const button=page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'Confirm'});
  await button.dblclick();await expect(button).toBeDisabled();await expect.poll(()=>state.feedbackCalls).toBe(1);
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'Undo'})).toBeVisible();expect(state.feedbackCalls).toBe(1);
});
test('pagination, search empty results and clearing filters work',async({page})=>{
  await setup(page);await page.getByRole('button',{name:'Next page'}).click();await expect(page.getByText('21–24 of 24 matching saved emails')).toBeVisible();
  await page.getByLabel('Search sender, subject or saved body').fill('does-not-exist');await page.getByRole('button',{name:'Search',exact:true}).click();
  await expect(page.getByText('No emails match these filters.',{exact:false})).toBeVisible();await page.getByRole('button',{name:'Clear filters'}).click();await expect(page.getByText('1–20 of 24 matching saved emails')).toBeVisible();
});
test('history includes timestamps and delivery steps',async({page})=>{
  await setup(page);const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});await card.getByRole('button',{name:'History'}).click();
  await expect(card.getByRole('heading',{name:'Feedback'})).toBeVisible();await expect(card).toContainText('notify · sent');await card.getByRole('button',{name:'Hide history'}).click();await expect(card.getByRole('heading',{name:'Feedback'})).toHaveCount(0);
});
test('narrow screen has no horizontal overflow even with long email text',async({page})=>{
  await page.setViewportSize({width:375,height:812});const state=await setup(page);state.rows[0].sender='LongSender'.repeat(50);state.rows[0].subject='LongSubject'.repeat(40);
  await page.getByRole('button',{name:'Queue inbox check'}).click();await expect(page.getByText('Inbox processing queued.')).toBeVisible();
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:'../docs/phase6-mobile.png',fullPage:false});
});
test('reduced motion disables hover movement and transitions',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await setup(page);const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});await card.hover();await expect(card).toHaveCSS('transform','none');await expect(card).toHaveCSS('transition-duration','0s');
});
test('session expiry clears account data instead of showing stale private rows',async({page})=>{
  const state=await setup(page);state.paired=false;await page.getByRole('button',{name:'Queue inbox check'}).click();
  await expect(page.getByLabel('Pairing code')).toBeVisible();await expect(page.getByRole('article')).toHaveCount(0);
});
test('logout during delayed refresh cannot restore old emails',async({page})=>{
  const state=await setup(page);state.emailsDelay=700;state.emailsStarted=false;
  await page.getByRole('button',{name:'Queue inbox check'}).click();await expect.poll(()=>state.emailsStarted).toBe(true);
  await page.getByRole('button',{name:'Account options'}).click();await page.getByRole('button',{name:'Logout',exact:true}).click();
  await expect(page.getByLabel('Pairing code')).toBeVisible();await page.waitForTimeout(850);await expect(page.getByRole('article')).toHaveCount(0);
});
test('refresh outage labels last-known data and disables email mutations',async({page})=>{
  const state=await setup(page);state.telemetryError=503;await page.getByRole('button',{name:'Queue inbox check'}).click();await expect(page.getByRole('alert')).toContainText('Synthetic telemetry outage.');
  await expect(page.getByText('Showing last known data.',{exact:false})).toBeVisible();await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'Confirm'})).toBeDisabled();
});

test('desktop layout keeps all lanes readable without overflow',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});await setup(page);
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.locator('.board').scrollIntoViewIfNeeded();
  await page.screenshot({path:'../docs/phase6-desktop.png',fullPage:false});
});


test('synthetic model readiness keeps its evaluation limitation visible',async({page})=>{
  const state=await setup(page);state.localReady=true;state.evaluationScope='synthetic_benchmark_only';
  await page.getByLabel('Search sender, subject or saved body').fill('Synthetic');
  await page.getByRole('button',{name:'Search',exact:true}).click();
  await page.getByText('System health',{exact:false}).click();
  await expect(page.getByText('Loaded · synthetic benchmark only',{exact:false})).toBeVisible();
});


test('local-only mode disables cloud, inbox checks and Google sign-in controls',async({page})=>{
  const state=await setup(page);state.localOnly=true;
  await page.getByLabel('Search sender, subject or saved body').fill('Synthetic');await page.getByRole('button',{name:'Search',exact:true}).click();
  await page.getByText('System health',{exact:false}).click();
  await expect(page.getByText('Local-only mode is active. Cloud, Gmail and external alerts are disabled.',{exact:false})).toBeVisible();
  await expect(page.getByRole('button',{name:'Queue inbox check'})).toBeDisabled();
  await page.getByRole('button',{name:'Account options'}).click();await expect(page.getByRole('button',{name:'Switch Google account'})).toBeDisabled();
  for(const provider of ['Gemini:','Groq:'])await expect(page.locator('.health-grid p').filter({hasText:provider})).toContainText('Disabled');
});
