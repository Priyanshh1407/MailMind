import { test, expect } from '@playwright/test';

async function setup(page) {
  const state = { paired: true, connected: true, generation: 1, revision: 0, feedbackCalls: 0, feedbackError: 0, feedbackDelay: 0, emailsDelay: 0, emailsStarted: false, telemetryError: 0, actionError: 0, tokenError: 0, syncCalls: 0, fetchCalls: 0 };
  const stamp = '2026-09-18T12:00:00Z';
  state.rows = Array.from({length:24},(_,index)=>({ id:'synthetic-'+index, account_id:'interview@example.test', sender:'Synthetic Sender <sender@example.test>', subject:'Synthetic email '+index,
    created_at:stamp, body_snippet:'Only synthetic email text is used.', prediction:index===3?null:['IMPORTANT','UPDATES','SPAM'][index%3], local_prediction:'SPAM',
    effective_category:index===3?null:['IMPORTANT','UPDATES','SPAM'][index%3], human_label:null, feedback:null,
    review_reason:index===3?{code:'provider_timeout',message:'The classification provider took too long to respond.'}:null,
    latest_prediction:{category:index===3?null:['IMPORTANT','UPDATES','SPAM'][index%3],outcome:index===3?'UNAVAILABLE':'CLASSIFIED',source:'synthetic',local:{outcome:'ABSTAIN',category:null,model_version:'legacy-binary'}},
    processing:{status:index===3?'retry':'complete',stage:index===3?'classify':'complete',attempt_count:1,next_retry_at:1789750000,error_code:index===3?'provider_transient':null},notification:index===0?{status:'sent'}:null,
    analysis:{analysis_version:'analysis-v1',predicted_category:index===3?null:['IMPORTANT','UPDATES','SPAM'][index%3],explanation_summary:index===3?'The provider timed out, so this message needs review.':'The message contains a direct request with a deadline.',signals:index===3?[]:[{signal:'direct_request',evidence:'Please review the synthetic request.'},{signal:'deadline'}],source:index===3?'system':'local_heuristic',model_version:index===3?null:'synthetic-local-v1',retrieval_used:false},
  }));
  state.rows[0].body=['Hello,','','Please review the synthetic request. It is due on Friday.','','Thanks,','Synthetic Sender'].join(String.fromCharCode(10));
  // Email 0 carries the full decision story; the others use the older layout.
  state.rows[0].decision={decided_by:'model',category:'IMPORTANT',route:'fallback',provider:'gemini',model_version:'gemini-3.5-flash-lite',precedents:{used:3,lookup:'used'},second_opinion:{category:'SPAM',agrees:false},elapsed_ms:1201,your_label:null};
  state.actions=[{action_id:7,account_id:'interview@example.test',email_id:'synthetic-0',fingerprint:'a'.repeat(64),action_type:'reply_required',title:'Reply to synthetic sender',description:'Send the requested confirmation.',evidence:'Please review the synthetic request.',due_at:'2026-10-02T11:30:00.000000Z',due_precision:'exact_time',confidence:'high',extraction_source:'local_heuristic',status:'open',snoozed_until:null,revision:0,created_at:stamp,updated_at:stamp,completed_at:null,analysis_revision:stamp,next_reminder_at:null,delivered_reminder_count:0}];
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin==='http://127.0.0.1:18106')return route.continue();
    if(url.origin!=='http://127.0.0.1:8000')return route.abort(); // Never contact real providers or font hosts.
    let status=200, data;
    const identity={account_id:'interview@example.test',generation:state.generation};
    const method=route.request().method(), path=url.pathname;
    if(path==='/session' && method==='POST'){state.paired=true;data={csrf_token:'synthetic-csrf'};}
    else if(!state.paired){status=401;data={detail:'Sign in to this local app first.'};}
    else if(path==='/session')data={email:identity.account_id,csrf_token:'synthetic-csrf',connected:state.connected,purge_pending:false,generation:state.generation,read_only:!state.connected&&state.workerError==='gmail_unavailable'};
    else if(path==='/status')data={...identity,connected:state.connected,is_polling:false,auth_in_progress:false,purge_pending:false,auto_mark_read:false,ingestion_paused:true,fetch_next_available:true,live_monitoring:true,active_pending_tasks:1,live_pending_tasks:0,backlog_pending_tasks:1,max_pending_tasks:100,resume_pending_tasks:50,current_batch_target_tasks:100,current_batch_admitted_tasks:100,workflow_total_tasks:24,workflow_finished_tasks:23,processing_counts:{complete:23,retry:1},notification_counts:{sent:1},ingestion_failures:[],semantic_search_index:{pending:0,indexed:24,failed:0},intelligence_backfill:{enabled:true,eligible:2,queued:0,running:0,retry:0,complete:0,dead:0},poll_interval_seconds:60,worker_lease_seconds:90,worker:state.workerError?{heartbeat_at:stamp,last_success_at:stamp,last_error_at:stamp,last_error_code:state.workerError}:null,connectivity:state.workerError?{gmail:'unreachable',failed_checks:3,retry_in_seconds:20}:{gmail:'ok',failed_checks:0,retry_in_seconds:null},ingestion:{status:'deferred',fetched_count:100,failed_count:0,last_checked_at:stamp,has_more:1,backlog_authorized:0,live_status:'empty',last_live_sync_at:stamp}};
    else if(path==='/telemetry'){
      status=state.telemetryError||200;
      data=status!==200?{detail:'Synthetic telemetry outage.'}:{...identity,totals:{saved:24,completed:23,labelled:state.rows.filter(row=>row.human_label).length,corrected:state.rows.filter(row=>row.human_label&&row.human_label!==row.prediction).length,confirmed:state.rows.filter(row=>row.human_label&&row.human_label===row.prediction).length,feedback_events:state.revision,prediction_attempts:24},classification_timing:{mean_ms:1201,sample_count:24},local_model:{ready:state.localReady||false,version:'legacy-binary',evaluation_scope:state.evaluationScope||null},mode:{local_only:state.localOnly||false},providers:{gemini:state.localOnly?'disabled_local_only':'configured_unverified',groq:state.localOnly?'disabled_local_only':'unconfigured'}};
    } else if(path==='/emails'){
      state.emailsStarted=true;
      const search=url.searchParams.get('search')||'',category=url.searchParams.get('category'),emailId=url.searchParams.get('email_id');
      const rows=state.rows.filter(row=>(!emailId||row.id===emailId)&&(row.subject+' '+row.sender+' '+row.body_snippet).toLowerCase().includes(search.toLowerCase())&&(!category||(row.effective_category||'NEEDS_REVIEW')===category));
      const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||20);
      data={...identity,emails:JSON.parse(JSON.stringify(rows.slice(offset,offset+limit))),total:rows.length,limit,offset,has_more:offset+limit<rows.length,search_mode:search.length>=3?'hybrid':'text',semantic_available:true,semantic_index:{pending:0,indexed:rows.length,failed:0}};
      if(state.emailsDelay)await new Promise(resolve=>setTimeout(resolve,state.emailsDelay));
      // A test can hold one specific search open until it releases the gate.
      if(state.holdSearch&&search===state.holdSearch){state.heldSearchStarted=true;await state.searchGate;}
    } else if(path==='/actions/summary'){
      status=state.actionError||200;
      const counts={open:0,completed:0,dismissed:0,snoozed:0};
      state.actions.forEach(action=>counts[action.status]++);
      const typeCounts={};state.actions.forEach(action=>typeCounts[action.action_type]=(typeCounts[action.action_type]||0)+1);
      data=status!==200?{detail:'Synthetic Action Center outage.'}:{...identity,total:state.actions.length,overdue:0,due_soon:counts.open,status_counts:counts,type_counts:typeCounts,reminder_counts:{scheduled:0,claimed:0,delivered:0,dismissed:0,retry:0,dead:0}};
    } else if(path==='/actions'){
      status=state.actionError||200;
      const actionStatus=url.searchParams.get('status');
      const actionType=url.searchParams.get('action_type');
      const actions=state.actions.filter(action=>(!actionStatus||action.status===actionStatus)&&(!actionType||action.action_type===actionType));
      data=status!==200?{detail:'Synthetic Action Center outage.'}:{...identity,actions:JSON.parse(JSON.stringify(actions)),limit:Number(url.searchParams.get('limit')||50),offset:Number(url.searchParams.get('offset')||0)};
    } else if(path.startsWith('/actions/')&&(method==='PATCH'||path.endsWith('/snooze'))){
      const actionId=Number(path.split('/')[2]),action=state.actions.find(item=>item.action_id===actionId),body=route.request().postDataJSON();
      action.status=path.endsWith('/snooze')?'snoozed':body.status;
      action.snoozed_until=path.endsWith('/snooze')?body.snoozed_until:null;
      action.completed_at=action.status==='completed'?'2026-09-28T10:00:00.000000Z':null;
      action.updated_at='2026-09-28T10:00:00.000000Z';
      action.revision++;
      data=JSON.parse(JSON.stringify(action));
    } else if(path==='/analytics/tokens'){
      status=state.tokenError||200;
      const window=url.searchParams.get('window')||'day';
      data=status!==200?{detail:'Synthetic token analytics outage.'}:{...identity,window,timezone:'Asia/Kolkata',start_at:'2026-09-27T18:30:00.000000Z',end_at:'2026-09-28T18:30:00.000000Z',totals:{event_count:2,input_tokens:120,output_tokens:30,total_tokens:150,unknown_events:0},provider_billed_tokens:130,local_processed_tokens:20,providers:[{key:'gemini',event_count:1,input_tokens:100,output_tokens:30,total_tokens:130,unknown_events:0},{key:'local',event_count:1,input_tokens:20,output_tokens:0,total_tokens:20,unknown_events:0}],operations:[{key:'classification_analysis',event_count:2,input_tokens:120,output_tokens:30,total_tokens:150,unknown_events:0}],count_methods:[{key:'provider_reported',event_count:2,input_tokens:120,output_tokens:30,total_tokens:150,unknown_events:0}],outcomes:[{key:'success',event_count:2,input_tokens:120,output_tokens:30,total_tokens:150,unknown_events:0}],daily:[{date:'2026-09-28',start_at:'2026-09-27T18:30:00.000000Z',end_at:'2026-09-28T18:30:00.000000Z',event_count:2,input_tokens:120,output_tokens:30,total_tokens:150,unknown_events:0,provider_billed_tokens:130,local_processed_tokens:20}]};
    } else if(path==='/feedback'){
      state.feedbackCalls++;
      if(state.feedbackDelay)await new Promise(resolve=>setTimeout(resolve,state.feedbackDelay));
      if(state.feedbackError){status=state.feedbackError;data={detail:'Synthetic feedback save failed.'};}
      else{const body=route.request().postDataJSON(),row=state.rows.find(row=>row.id===body.email_id);row.human_label=body.label;row.effective_category=body.label;row.feedback={revision_id:++state.revision,label:body.label,indexing_state:'pending'};data={revision_id:state.revision,indexing_state:'pending',message:'Correction saved. Indexing will retry.'};status=202;}
    } else if(path.startsWith('/feedback/')&&method==='DELETE'){
      const row=state.rows.find(row=>row.id===decodeURIComponent(path.split('/')[2]));row.human_label=null;row.effective_category=row.prediction;row.feedback={revision_id:++state.revision,label:null,indexing_state:'pending'};data={revision_id:state.revision,indexing_state:'pending',message:'Feedback removed. Previous feedback history is kept. Index cleanup is queued.'};
    } else if(path.endsWith('/history'))data={...identity,feedback:[{revision_id:1,label:'UPDATES',indexing_state:'indexed',created_at:stamp}],processing:[{stage:'notify',outcome:'sent',created_at:stamp}]};
    else if(path==='/logout'){state.paired=false;state.connected=false;state.generation++;data={message:'Signed out. Saved mail is kept.'};}
    else if(path==='/inbox/sync'){state.syncCalls++;status=202;data={job_id:43,status:'queued',message:'Checking Gmail for new messages.'};}
    else if(path==='/ingestion/fetch-next'){state.fetchCalls++;(state.fetchBodies||=[]).push(route.request().postDataJSON());status=202;data={job_id:44,status:'queued',message:'Loading the next 20 older messages.'};}
    else if(path==='/intelligence/backfill'){state.backfillCalls=(state.backfillCalls||0)+1;status=202;data={job_id:45,status:'queued',requested:20,admitted:2,eligible:2,capacity:99,message:'Queued 2 saved emails for analysis.'};}
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
  await expect(page.getByText('1.3 s',{exact:true})).toBeVisible(); // 1201 ms, one decimal, rounded up
  await page.getByRole('button',{name:'Systems healthy'}).click();
  await expect(page.getByText('Unavailable for three-category inference',{exact:false})).toBeVisible();
  await expect(page.locator('.health-grid p').filter({hasText:'Gemini:'})).toContainText('Configured');
  await expect(page.getByText('240ms',{exact:true})).toHaveCount(0);
  await expect(page.getByText('2/2',{exact:true})).toHaveCount(0);
});
test('all four lanes show their correct styles and names',async({page})=>{
  await setup(page);
  for(const category of ['Important','Updates','Spam','Needs Review'])await expect(page.getByRole('region',{name:category+' emails',exact:true})).toBeVisible();
  const spam=page.getByRole('article',{name:'Synthetic email 2',exact:true}).locator('.category-badge');
  await expect(spam).toHaveCSS('color','rgb(255, 107, 112)');
  await expect(page.getByRole('article',{name:'Synthetic email 3',exact:true})).toContainText('Needs review: The classification provider took too long to respond.');
  await expect(page.getByText('BLOCKED',{exact:true})).toHaveCount(0);
});
test('Phase 6 tabs, KPIs, action lifecycle and source navigation work together',async({page})=>{
  await setup(page);
  // One overview: inbox stats and follow-through KPIs share the same card design.
  const summary=page.getByRole('region',{name:'Overview'});
  await expect(summary.locator('.stat')).toHaveCount(8);
  await expect(summary).toContainText('Open actions1');
  await expect(summary).toContainText('Due soon1');
  await expect(summary).toContainText('Tokens today150');
  await expect(summary).toContainText('Saved emails24');
  // The section tabs live in the header navbar.
  await expect(page.locator('header .header-nav').getByRole('tablist',{name:'Dashboard sections'})).toBeVisible();
  const inbox=page.getByRole('tab',{name:'Inbox'});
  await inbox.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab',{name:'Action Center'})).toBeFocused();
  await expect(page.getByRole('heading',{name:'Reply to synthetic sender'})).toBeVisible();
  await page.getByRole('button',{name:'Complete',exact:true}).click();
  await page.getByRole('button',{name:'Completed'}).click();
  await expect(page.getByRole('heading',{name:'Reply to synthetic sender'})).toBeVisible();
  await page.getByRole('button',{name:'Open source'}).click();
  await page.getByRole('dialog').getByRole('button',{name:'Open in inbox'}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('tab',{name:'Inbox'})).toHaveAttribute('aria-selected','true');
  await expect(page.getByText('Showing the email that produced the selected action.')).toBeVisible();
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  await expect(page.getByRole('article',{name:'Synthetic email 1',exact:true})).toHaveCount(0);
});
test('Action Center flags only the tasks the AI was unsure about',async({page})=>{
  const state=await setup(page);
  state.actions=[{...state.actions[0]},{...state.actions[0],action_id:8,fingerprint:'b'.repeat(64),title:'Unsure synthetic task',confidence:'medium'}];
  await page.getByRole('tab',{name:'Action Center'}).click();
  const sure=page.locator('.action-card').filter({hasText:'Reply to synthetic sender'});
  const unsure=page.locator('.action-card').filter({hasText:'Unsure synthetic task'});
  await expect(unsure).toBeVisible({timeout:8000});
  await expect(unsure.getByText('Double-check this')).toBeVisible();
  await expect(sure.getByText('Double-check this')).toHaveCount(0);
  await expect(page.locator('.action-card dt',{hasText:'Confidence'})).toHaveCount(0);   // no noisy row
});

test('Action Center opens the source email in a pop-up; Esc and outside clicks close it',async({page})=>{
  await setup(page);
  await page.getByRole('tab',{name:'Action Center'}).click();
  const open=page.getByRole('button',{name:'Open source'});
  await open.click();
  const dialog=page.getByRole('dialog',{name:'Synthetic email 0'});
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Synthetic Sender');
  await expect(dialog).toContainText('It is due on Friday.');               // the full email text
  await expect(dialog.locator('mark')).toHaveText('Please review the synthetic request.'); // the quoted evidence, highlighted
  // Stays in the Action Center: no jump to the Inbox.
  await expect(page.getByRole('tab',{name:'Action Center'})).toHaveAttribute('aria-selected','true');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(open).toBeFocused();                                          // focus returns to the opener
  await open.click();
  await expect(dialog).toBeVisible();
  await page.mouse.click(8,8);                                               // outside the panel, on the backdrop
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await open.click();
  await dialog.getByRole('button',{name:'Close source email'}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
test('Action Center type filter offers only existing types and narrows the list',async({page})=>{
  const state=await setup(page);
  state.actions.push({...state.actions[0],action_id:8,email_id:'synthetic-1',action_type:'payment_required',title:'Pay the venue invoice'});
  await page.getByRole('tab',{name:'Action Center'}).click();
  const typeFilter=page.getByLabel('Filter actions by type');
  await expect(typeFilter.locator('option')).toHaveText(['All types · 2','Reply · 1','Payment · 1']);
  await typeFilter.selectOption('payment_required');
  await expect(page.getByRole('heading',{name:'Pay the venue invoice'})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Reply to synthetic sender'})).toHaveCount(0);
  await typeFilter.selectOption('');
  await expect(page.getByRole('heading',{name:'Reply to synthetic sender'})).toBeVisible();
});
test('Phase 6 explanations keep model rationale separate from correction controls',async({page})=>{
  await setup(page);
  const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});
  await card.getByText('Why this category?').click();
  const panel=card.locator('.classification-explanation');
  await expect(panel.getByRole('button')).toHaveCount(0);
  await expect(panel).toContainText('The message contains a direct request with a deadline.');
  await expect(panel).toContainText('Direct Request');
  // The whole decision: who decided (a fallback here), meaning, past corrections, second opinion.
  await expect(panel).toContainText('Gemini (gemini-3.5-flash-lite) chose Important in 1.3 s. The main Gemini model was unavailable, so the backup Gemini model answered.');
  await expect(panel).toContainText('Direct communication, or something that needs your action or attention.');
  await expect(panel).toContainText('3 similar emails you corrected were shown to the model as examples.');
  await expect(panel).toContainText("The local model would have chosen Spam; the cloud model's decision is the one used.");
  await expect(panel).not.toContainText('Source:');
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'Set label'})).toBeVisible();
  // Without a decision story, the short source line is still shown.
  const older=page.getByRole('article',{name:'Synthetic email 1',exact:true});
  await older.getByText('Why this category?').click();
  await expect(older.locator('.classification-explanation')).toContainText('Source: Local model - synthetic-local-v1');
});
test('Phase 6 usage renders Recharts, window controls, breakdowns and accessible values',async({page})=>{
  await setup(page);
  await page.getByRole('tab',{name:'Usage'}).click();
  await expect(page.locator('.recharts-responsive-container')).toBeVisible();
  await expect(page.locator('.usage-metric').filter({hasText:'Cloud billed'})).toBeVisible();
  await expect(page.locator('.usage-metric').filter({hasText:'Local processed'})).toBeVisible();
  await expect(page.getByText('Token counts are operational usage, not a cost estimate.')).toBeVisible();
  await page.getByRole('button',{name:'Month'}).click();
  await expect(page.getByRole('button',{name:'Month'})).toHaveAttribute('aria-pressed','true');
  await page.getByText('View daily token values').click();
  await expect(page.getByRole('columnheader',{name:'Cloud billed'})).toBeVisible();
  await expect(page.getByRole('cell',{name:'130'})).toBeVisible();
});
test('Phase 6 section failures stay local and preserve the inbox',async({page})=>{
  const state=await setup(page);
  state.actionError=503;
  await page.getByRole('tab',{name:'Action Center'}).click();
  await page.getByRole('button',{name:'Due soon'}).click();
  await expect(page.getByRole('alert')).toContainText('Actions could not be loaded.');
  await page.getByRole('tab',{name:'Inbox'}).click();
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  state.tokenError=503;
  await page.getByRole('tab',{name:'Usage'}).click();
  await page.getByRole('button',{name:'Week'}).click();
  await expect(page.getByRole('alert')).toContainText('Token analytics could not be loaded.');
});
test('inbox progress is visible and the header reflects durable work',async({page})=>{
  await setup(page);
  const progress=page.getByRole('region',{name:'Inbox work progress'});
  await expect(progress).toContainText('24Fetched & saved');
  await expect(progress).toContainText('1Waiting');
  await expect(progress).toContainText('23Completed');
  await expect(progress).toContainText('Workflow finished 23 of 24');
  await expect(page.locator('.connection-state')).toHaveText('Processing inbox');
});
test('Phase 8 saved-mail intelligence backfill is explicit, bounded and side-effect safe',async({page})=>{
  const state=await setup(page);
  await expect(page.getByText('Intelligence backfill: 2 eligible / 0 active / 0 complete.')).toBeVisible();
  await expect(page.getByText('It never sends historical alerts, marks mail read, or creates automatic reminders.')).toBeVisible();
  await page.getByRole('button',{name:'Analyze up to 20 saved emails'}).click();
  await expect.poll(()=>state.backfillCalls||0).toBe(1);
  await expect(page.getByText('Queued 2 saved emails for analysis.')).toBeVisible();
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
  await expect(page.getByRole('region',{name:'Updates emails',exact:true}).getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  await card.getByText('Classification details').click();await expect(card).toContainText('Original primary: IMPORTANT');await expect(card).toContainText('Your feedback: UPDATES · index update pending');
  await card.getByRole('button',{name:'Edit label'}).click();await card.getByLabel('Correct category').selectOption('SPAM');await card.getByRole('button',{name:'Save category'}).click();
  await expect(page.getByRole('region',{name:'Spam emails',exact:true}).getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  await card.getByRole('button',{name:'Undo'}).click();await expect(page.getByRole('region',{name:'Important emails',exact:true}).getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
});
test('failed feedback stays visible without false success',async({page})=>{
  const state=await setup(page);state.feedbackError=500;
  const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});await card.getByRole('button',{name:'Set label'}).click();await card.getByRole('button',{name:'Save category'}).click();
  await expect(page.getByRole('alert')).toContainText('Synthetic feedback save failed.');
  await page.waitForTimeout(200);await expect(page.getByRole('alert')).toBeVisible();
  await expect(card.getByRole('button',{name:'Undo'})).toHaveCount(0);
  await page.getByRole('button',{name:'Dismiss error'}).click();await expect(page.getByRole('alert')).toHaveCount(0);
});
test('pending feedback blocks duplicate actions',async({page})=>{
  const state=await setup(page);state.feedbackDelay=500;const button=page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'Confirm'});
  await button.dblclick();await expect(button).toBeDisabled();await expect.poll(()=>state.feedbackCalls).toBe(1);
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'Undo'})).toBeVisible();expect(state.feedbackCalls).toBe(1);
});
test('pagination, search empty results and clearing filters work',async({page})=>{
  await setup(page);await page.getByRole('button',{name:'Next page'}).click();await expect(page.getByText('21–24 of 24 matching saved emails')).toBeVisible();
  await page.getByLabel('Search sender, subject, body, or meaning').fill('does-not-exist');
  await expect(page.getByText('No emails match these filters.',{exact:false})).toBeVisible();await page.getByRole('button',{name:'Clear search'}).click();await expect(page.getByText('1–20 of 24 matching saved emails')).toBeVisible();
});
test('search and action feedback do not move the viewport or remove the current board',async({page})=>{
  test.setTimeout(45000); // waits ~6 s for a toast to auto-hide; slow under parallel load
  const state=await setup(page);
  await expect(page.locator('.dashboard.booting')).toHaveCount(0); // measure layout after the power-on roll-up
  await page.locator('.filters').scrollIntoViewIfNeeded();
  const position=()=>page.evaluate(()=>({scrollY:window.scrollY,filterTop:document.querySelector('.filters').getBoundingClientRect().top}));
  const before=await position();
  // Hold this search's response open (no timer), so the check below can't race it.
  let releaseSearch;state.searchGate=new Promise(resolve=>{releaseSearch=resolve;});state.holdSearch='does-not-exist';
  await page.getByLabel('Search sender, subject, body, or meaning').fill('does-not-exist');
  await expect.poll(()=>state.heldSearchStarted).toBe(true);
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  const during=await position();
  expect(Math.abs(during.scrollY-before.scrollY)).toBeLessThanOrEqual(1);
  expect(Math.abs(during.filterTop-before.filterTop)).toBeLessThanOrEqual(1);
  releaseSearch();state.holdSearch=null;
  await expect(page.getByText('No emails match these filters.',{exact:false})).toBeVisible();
  await page.getByRole('button',{name:'Clear search'}).click();
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  const syncButton=page.getByRole('button',{name:'Sync new messages'});
  await syncButton.scrollIntoViewIfNeeded();
  const beforeAction=await position();
  await syncButton.click();
  // Under parallel load the local mock can be slow; this test is about layout, not speed.
  await expect(page.getByText('Checking Gmail for new messages.')).toBeVisible({timeout:15000});
  const afterAction=await position();
  expect(Math.abs(afterAction.scrollY-beforeAction.scrollY)).toBeLessThanOrEqual(1);
  expect(Math.abs(afterAction.filterTop-beforeAction.filterTop)).toBeLessThanOrEqual(1);
  await expect(page.getByText('Checking Gmail for new messages.')).toHaveCount(0,{timeout:7000});
});
test('older mail loads automatically in small batches near the end of the inbox, without a toast',async({page})=>{
  const state=await setup(page);
  // 24 saved emails at 20 per page: page 1 is already the second-to-last page.
  await expect.poll(()=>state.fetchCalls).toBeGreaterThanOrEqual(1);
  expect(state.fetchBodies[0]).toEqual({limit:20});
  await expect(page.getByRole('button',{name:/Fetch next/})).toHaveCount(0);
  await expect(page.getByText('Loading the next 20 older messages.')).toHaveCount(0);
  await page.getByRole('button',{name:'Sync new messages'}).click();
  await expect.poll(()=>state.syncCalls).toBe(1);
});
test('a search near the end of its results does not load older mail',async({page})=>{
  const state=await setup(page);
  await expect.poll(()=>state.fetchCalls).toBeGreaterThanOrEqual(1);
  const before=state.fetchCalls;
  await page.getByLabel('Search sender, subject, body, or meaning').fill('Synthetic email 1');
  await expect(page.getByRole('article',{name:'Synthetic email 1',exact:true})).toBeVisible();
  await page.waitForTimeout(1500);
  expect(state.fetchCalls).toBe(before);
});
test('history includes timestamps and delivery steps',async({page})=>{
  await setup(page);const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});await card.getByRole('button',{name:'History'}).click();
  await expect(card.getByRole('heading',{name:'Feedback'})).toBeVisible();await expect(card).toContainText('notify · sent');await card.getByRole('button',{name:'Hide history'}).click();await expect(card.getByRole('heading',{name:'Feedback'})).toHaveCount(0);
});
test('narrow screen has no horizontal overflow even with long email text',async({page})=>{
  await page.setViewportSize({width:375,height:812});const state=await setup(page);state.rows[0].sender='LongSender'.repeat(50);state.rows[0].subject='LongSubject'.repeat(40);
  await page.getByRole('button',{name:'Sync new messages'}).click();await expect(page.getByText('Checking Gmail for new messages.')).toBeVisible();
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.getByRole('tab',{name:'Action Center'}).click();
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.getByRole('tab',{name:'Usage'}).click();
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:'../docs/phase6-mobile.png',fullPage:false});
});
test('animations settle on exact values: counters, share bars, tab pill and panel',async({page})=>{
  await setup(page);
  await page.getByRole('tab',{name:'Usage'}).click();
  const panel=page.locator('#panel-usage');
  await expect(panel).toHaveCSS('opacity','1');
  await expect(page.locator('.usage-metric').filter({hasText:'Cloud billed'}).locator('strong')).toHaveText('130');
  await expect(page.locator('.usage-metric').filter({hasText:'Input'}).locator('strong')).toHaveText('120');
  const bar=page.locator('.breakdown-bar').first();
  await expect(bar).toHaveCSS('transform','none');
  await expect(page.getByRole('tab',{name:'Usage'}).locator('.tab-indicator')).toHaveCount(1);
  await expect(page.locator('.tab-indicator')).toHaveCount(1);
});
test('power-on sequence plays once when the dashboard comes up, then ends',async({page})=>{
  await setup(page);
  await expect(page.locator('.dashboard.booting')).toHaveCount(1);
  await expect(page.locator('.dashboard.booting')).toHaveCount(0,{timeout:5000});
  await expect(page.locator('.stat').filter({hasText:'Saved emails'}).locator('strong')).toHaveText('24');
});
test('reduced motion skips the power-on sequence',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await setup(page);
  await expect(page.locator('.dashboard')).toBeVisible();
  await expect(page.locator('.dashboard.booting')).toHaveCount(0);
  await expect(page.locator('.stat').filter({hasText:'Saved emails'}).locator('strong')).toHaveText('24',{timeout:500});
});
test('reduced motion shows final numbers and bars immediately',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await setup(page);
  await page.getByRole('tab',{name:'Usage'}).click();
  await expect(page.locator('.usage-metric').filter({hasText:'Cloud billed'}).locator('strong')).toHaveText('130',{timeout:500});
  await expect(page.locator('.breakdown-bar').first()).toHaveCSS('transform','none',{timeout:500});
});
test('reduced motion disables hover movement and transitions',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await setup(page);const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});await card.hover();await expect(card).toHaveCSS('transform','none');await expect(card).toHaveCSS('transition-duration','0s');
});
test('session expiry opens a new local session without asking for a code',async({page})=>{
  const state=await setup(page);state.paired=false;await page.getByRole('button',{name:'Sync new messages'}).click();
  await expect(page.getByRole('button',{name:'Account options'})).toBeVisible();
  await expect(page.getByLabel('Pairing code')).toHaveCount(0);
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
});
test('logout during delayed refresh cannot restore old emails',async({page})=>{
  const state=await setup(page);state.emailsDelay=700;state.emailsStarted=false;
  await page.getByRole('button',{name:'Sync new messages'}).click();await expect.poll(()=>state.emailsStarted).toBe(true);
  await page.getByRole('button',{name:'Account options'}).click();await page.getByRole('button',{name:'Stop processing',exact:true}).click();
  await expect(page.getByRole('button',{name:'Connect Google',exact:true})).toBeVisible();await page.waitForTimeout(850);await expect(page.getByRole('article')).toHaveCount(0);
});
test('refresh outage labels last-known data and disables email mutations',async({page})=>{
  const state=await setup(page);
  // Let the automatic older-mail load (and its refresh) settle first, then the next poll hits the outage.
  await expect.poll(()=>state.fetchCalls).toBeGreaterThanOrEqual(1);state.telemetryError=503;
  await expect(page.getByRole('alert')).toContainText('Synthetic telemetry outage.',{timeout:12000});
  await expect(page.getByText('Showing last known data.',{exact:false})).toBeVisible();await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'Confirm'})).toBeDisabled();
});

test('email action buttons wrap inside the card when space is tight (browser zoom)',async({page})=>{
  await page.setViewportSize({width:1100,height:900});await setup(page);
  await expect(page.locator('.dashboard.booting')).toHaveCount(0,{timeout:5000});
  const overflowing=await page.evaluate(()=>{let count=0;for(const card of document.querySelectorAll('.email-card')){const box=card.getBoundingClientRect();for(const button of card.querySelectorAll('.email-actions .button')){const r=button.getBoundingClientRect();if(r.right>box.right+1||r.left<box.left-1)count++;}}return count;});
  expect(overflowing).toBe(0);
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true}).getByRole('button',{name:'History'})).toBeVisible();
});
test('switching sections scrolls Action Center and Usage to their start, and Inbox back to the overview',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});  // instant scroll, so positions are exact
  await page.setViewportSize({width:1440,height:700});await setup(page);
  const below=async key=>page.evaluate(id=>{const header=document.querySelector('.dashboard-header').getBoundingClientRect().bottom;return {scrollY:window.scrollY,gap:document.getElementById(id).getBoundingClientRect().top-header};},'panel-'+key);
  await page.getByRole('tab',{name:'Action Center'}).click();
  await expect.poll(async()=>(await below('actions')).scrollY).toBeGreaterThan(0);
  let at=await below('actions');expect(at.gap).toBeGreaterThanOrEqual(0);expect(at.gap).toBeLessThan(40);
  await page.getByRole('tab',{name:'Usage'}).click();
  await expect(page.locator('.recharts-responsive-container')).toBeVisible();
  await expect.poll(async()=>(await below('usage')).gap).toBeLessThan(40);
  await page.getByRole('tab',{name:'Inbox'}).click();
  await expect.poll(()=>page.evaluate(()=>window.scrollY)).toBe(0);
  await expect(page.getByRole('region',{name:'Overview'})).toBeInViewport();
  // Arrow keys switch sections the same way.
  await page.getByRole('tab',{name:'Inbox'}).focus();await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab',{name:'Action Center'})).toHaveAttribute('aria-selected','true');
  await expect.poll(async()=>(await below('actions')).scrollY).toBeGreaterThan(0);
});
test('a rejected Google login asks to sign in again and says why',async({page})=>{
  const state=await setup(page);
  state.connected=false;state.workerError='gmail_unavailable';
  await page.reload();
  const renew=page.getByRole('alert').filter({hasText:'Google sign-in needs renewing'});
  await expect(renew).toBeVisible();
  await expect(renew).toContainText('Click Connect Google to sign in again');
  await expect(page.getByRole('button',{name:/Connect Google/}).first()).toBeVisible();
  // Saved mail stays readable; anything that changes it is disabled.
  await expect(renew).toContainText('browse your saved mail and actions (read-only)');
  const card=page.getByRole('article',{name:'Synthetic email 0',exact:true});
  await expect(card).toBeVisible();
  await expect(card.getByRole('button',{name:'History'})).toBeEnabled();
  await card.getByRole('button',{name:'History'}).click();
  await expect(card.getByRole('heading',{name:'Feedback'})).toBeVisible();
  await expect(card.getByRole('button',{name:/(Edit|Set) label/})).toBeDisabled();
  await expect(page.getByRole('button',{name:/Sync new messages/})).toBeDisabled();
  // A deliberate disconnect keeps the plain message.
  state.workerError=null;
  await page.reload();
  await expect(page.getByText('Google is disconnected')).toBeVisible();
  await expect(page.getByText('Google sign-in needs renewing')).toHaveCount(0);
});

test('service alerts: restarting, warning before shutdown, then recovered',async({page})=>{
  test.setTimeout(45000);
  // The supervisor file (served by the dashboard server) drives these alerts.
  let supervisor={schema:1,services:{api:{state:'running',restarts:0},worker:{state:'running',restarts:0}},shutdown_in_seconds:null,message:null};
  await setup(page);
  await page.route('**/mailmind-supervisor.json',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(supervisor)}));
  const email=page.getByRole('article',{name:'Synthetic email 0',exact:true});
  await expect(email).toBeVisible();
  supervisor={...supervisor,services:{...supervisor.services,worker:{state:'restarting',restarts:2,retry_in_seconds:4,last_exit_code:1}}};
  const restarting=page.getByRole('status').filter({hasText:'Restarting a MailMind service'});
  await expect(restarting).toBeVisible({timeout:6000});
  await expect(restarting).toContainText('Restarting the inbox worker in 4 s · restart 2');
  supervisor={...supervisor,services:{...supervisor.services,worker:{state:'crash_loop',restarts:5,retry_in_seconds:16,last_exit_code:1}},
    shutdown_in_seconds:42,message:"MailMind's worker keeps failing right after it starts (exit code 1, 5 times in a row)."};
  const warning=page.getByRole('status').filter({hasText:'MailMind may need to stop'});
  await expect(warning).toBeVisible({timeout:6000});
  await expect(warning).toContainText('exit code 1, 5 times in a row');
  await expect(warning).toContainText('Shutting down in 42 s unless it recovers');
  await expect(email).toBeVisible();   // informed first; nothing disappears
  supervisor={...supervisor,services:{...supervisor.services,worker:{state:'running',restarts:5,retry_in_seconds:null}},shutdown_in_seconds:null,message:null};
  await expect(page.getByRole('status').filter({hasText:'Everything is running again'})).toBeVisible({timeout:6000});
  await expect(warning).toHaveCount(0);
});

test('connectivity alerts: offline, back online, and Gmail unreachable',async({page,context})=>{
  test.setTimeout(45000);
  const state=await setup(page);
  await expect(page.getByRole('status').filter({hasText:"You're offline"})).toHaveCount(0);
  await context.setOffline(true);
  const offline=page.getByRole('status').filter({hasText:"You're offline"});
  await expect(offline).toBeVisible();
  await expect(offline).toContainText('Your saved mail is still here');
  await expect(offline).toContainText('No need to reconnect Google');
  // No blank screen and no login prompt: the saved mail stays.
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  await expect(page.getByText('Google is disconnected')).toHaveCount(0);
  await context.setOffline(false);
  await expect(page.getByRole('status').filter({hasText:"You're back online"})).toBeVisible();
  await expect(offline).toHaveCount(0);
  await expect(page.getByRole('status').filter({hasText:"You're back online"})).toHaveCount(0,{timeout:8000});
  // Device online, but the worker's Gmail checks are failing.
  state.workerError='network_unavailable';
  const gmail=page.getByRole('status').filter({hasText:"Can't reach Gmail"});
  await expect(gmail).toBeVisible({timeout:12000});
  await expect(gmail).toContainText("You're still connected");
  await expect(gmail).toContainText(/Retrying in \d+ s · 3 checks failed/);   // exponential backoff countdown
  await expect(page.getByRole('article',{name:'Synthetic email 0',exact:true})).toBeVisible();
  state.workerError=null;
  await expect(page.getByRole('status').filter({hasText:"You're back online"})).toBeVisible({timeout:12000});
});
test('desktop layout keeps all lanes readable without overflow',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});await setup(page);
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.locator('.board').scrollIntoViewIfNeeded();
  await page.screenshot({path:'../docs/phase6-desktop.png',fullPage:false});
});


test('synthetic model readiness keeps its evaluation limitation visible',async({page})=>{
  const state=await setup(page);state.localReady=true;state.evaluationScope='synthetic_benchmark_only';
  await page.getByLabel('Search sender, subject, body, or meaning').fill('Synthetic');
  await expect(page.getByText('Showing text and meaning-based matches.')).toBeVisible();
  await page.getByRole('button',{name:'Systems healthy'}).click();
  await expect(page.getByText('Loaded · synthetic benchmark only',{exact:false})).toBeVisible();
});


test('local-only mode disables cloud, inbox checks and Google sign-in controls',async({page})=>{
  const state=await setup(page);state.localOnly=true;
  await page.getByLabel('Search sender, subject, body, or meaning').fill('Synthetic');await expect(page.getByText('Showing text and meaning-based matches.')).toBeVisible();
  await page.getByRole('button',{name:'Systems healthy'}).click();
  await expect(page.getByText('Local-only mode is active. Cloud, Gmail and external alerts are disabled.',{exact:false})).toBeVisible();
  await expect(page.getByRole('button',{name:'Sync new messages'})).toBeDisabled();
  await page.getByRole('button',{name:'Account options'}).click();await expect(page.getByRole('button',{name:'Switch Google account'})).toBeDisabled();
  for(const provider of ['Gemini:','Groq:'])await expect(page.locator('.health-grid p').filter({hasText:provider})).toContainText('Disabled');
});
