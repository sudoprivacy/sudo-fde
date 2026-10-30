import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {application} from './app-harness.mjs';

function workspaceApp(aiError = null) {
  const current = {workspace: {id: 'ws-one', name: 'Workspace', phase: 'intro', version: 0},
    evidence: [], artifacts: [], jobs: [], catalogs: [], exports: [], editable: true};
  const app = application([], path => {
    if (path.endsWith('/evidence')) return {id:'answer-evidence',next_job: aiError ? null : {id: 'new-job'}, ai_error: aiError};
    if (path === '/api/workspaces/ws-one') return current;
  });
  app.context.fixture = {current, session: {
    user: {id: 'me', name: 'Me'}, tenant_id: 'my-tenant', tenants: [{id: 'my-tenant', name: 'My tenant'}],
    role: 'tenant_owner', router_bound: false, models: ['unbound-org-model'], chat_models: ['unbound-org-model'],
    providers: [{provider_id: 'personal', display_name: 'Personal', default_model: 'chat-primary',
      default_chat_model: 'chat-primary', models: ['gpt-image-2', 'chat-primary', 'chat-alternative'],
      chat_models: ['chat-primary', 'chat-alternative']}],
  }};
  vm.runInContext('Object.assign(state, fixture)', app.context);
  return app;
}

test('saving material and running AI both send the explicitly selected personal model', async () => {
  const app = workspaceApp();
  const selection = JSON.stringify(['personal', 'chat-alternative']);
  app.nodes['#model-select'] = {value: selection};
  await app.context.submit(app.form('evidence', {text: '我们做食品批发'}));
  const saved = app.requests.find(r => r.path.endsWith('/evidence'));
  assert.deepEqual(saved.body, {text: '我们做食品批发', start_ai: true, model: 'chat-alternative', provider_id: 'personal'});
  assert.match(app.nodes['#app'].innerHTML, /\[&quot;personal&quot;,&quot;chat-alternative&quot;\]" selected/);
  await app.context.queueAgent('summarize');
  const queued = app.requests.find(r => r.path.endsWith('/agent/summarize'));
  assert.equal(queued.body.model, 'chat-alternative');
  assert.equal(queued.body.provider_id, 'personal');
  if (process.env.FDE_WORKSPACE_MODEL_REQUESTS) writeFileSync(process.env.FDE_WORKSPACE_MODEL_REQUESTS, JSON.stringify([saved, queued]));
});

test('default selection respects provider default and omits unavailable organization and image models', () => {
  const app = workspaceApp();
  const choice = app.context.selectedWorkspaceModel();
  assert.equal(choice.provider_id, 'personal');
  assert.equal(choice.model, 'chat-primary');
  const selector = app.context.workspaceModelSelector();
  assert.doesNotMatch(selector, /gpt-image-2|unbound-org-model/);
  assert.match(selector, /chat-primary&quot;\]" selected/);
  app.context.fixture.session.providers[0].default_model = 'gpt-image-2';
  app.context.fixture.session.providers[0].default_chat_model = 'chat-alternative';
  vm.runInContext('state.workspaceModels = {}', app.context);
  assert.equal(app.context.selectedWorkspaceModel().model, 'chat-alternative');
});

test('model selection survives rerenders and is isolated per workspace', () => {
  const app = workspaceApp();
  app.nodes['#model-select'] = {value: JSON.stringify(['personal', 'chat-alternative'])};
  app.context.selectedWorkspaceModel();
  delete app.nodes['#model-select'];
  app.context.renderWorkspace();
  assert.match(app.nodes['#app'].innerHTML, /chat-alternative&quot;\]" selected/);
  app.context.fixture.current.workspace.id = 'ws-other';
  assert.equal(app.context.selectedWorkspaceModel(false).model, 'chat-primary');
  app.context.fixture.current.workspace.id = 'ws-one';
  assert.equal(app.context.selectedWorkspaceModel(false).model, 'chat-alternative');
});

test('personal provider named router and model IDs containing colons remain unambiguous', async () => {
  const app = workspaceApp();
  const provider = app.context.fixture.session.providers[0];
  Object.assign(provider, {provider_id: 'router', models: ['custom:latest'], chat_models: ['custom:latest'], default_chat_model: 'custom:latest'});
  await app.context.queueAgent('summarize');
  const queued = app.requests.find(r => r.path.endsWith('/agent/summarize'));
  assert.equal(queued.body.provider_id, 'router');
  assert.equal(queued.body.model, 'custom:latest');
});

test('no model still permits saving material, and optional AI errors explain that it was saved', async () => {
  const app = workspaceApp({code: 'PROVIDER_NOT_FOUND', message: '个人提供商已停用'});
  await app.context.submit(app.form('evidence', {text: '我们做食品批发'}));
  assert.match(app.nodes['#toast'].textContent, /资料已保存，AI 未启动：个人提供商已停用/);
  app.context.fixture.session.providers = [];
  await app.context.submit(app.form('evidence', {text: '补充资料'}));
  assert.equal(app.requests.filter(r => r.path.endsWith('/evidence')).at(-1).body.start_ai, false);
});

test('workspace reentry shows an existing result and interview answers use real question IDs', async () => {
  const app = workspaceApp();
  const artifact = {kind:'interview',status:'proposed',body:{questions:[{id:'question-live-id',text:'何时锁定库存？',options:[]}]}};
  app.context.fixture.current.artifacts = [artifact];
  assert.equal(app.context.preferredWorkspaceTab(app.context.fixture.current), 'interview');
  assert.match(app.context.card(artifact), /data-question="question-live-id"/);
  const form = app.form('interview-answer', {answer:'销售确认订单时锁定库存'});
  form.dataset.question = 'question-live-id';
  await app.context.submit(form);
  const request = app.requests.find(r=>r.path.endsWith('/agent/interview'));
  assert.deepEqual(request.body.params.answers, [{question_id:'question-live-id',evidence_ids:['answer-evidence']}]);
});

test('refinement editor restores saved values for the selected slot and clears new slots', () => {
  const app = workspaceApp();
  app.context.fixture.refinement = {bindings:[{slot_id:'one',body:{action:'gap',fields:[],grain:[],scope:'尚未采集',note:'供应商历史交期缺失'}}]};
  vm.runInContext('state.refinement=fixture.refinement', app.context);
  const fields = Object.fromEntries(['slot_id','action','fields','grain','scope','note'].map(name=>[name,{value:''}]));
  fields.slot_id.value='one';
  const form={querySelector:selector=>fields[selector.slice(6,-1)]};
  app.context.syncRefinementForm(form);
  assert.equal(fields.action.value,'gap');
  assert.equal(fields.note.value,'供应商历史交期缺失');
  fields.slot_id.value='two';
  app.context.syncRefinementForm(form);
  assert.equal(fields.action.value,'bind');
  assert.equal(fields.note.value,'');
});

test('viewer has no export creation or refinement editor controls', () => {
  const app=workspaceApp();
  app.context.fixture.current.editable=false;
  vm.runInContext("state.tab='files'",app.context);app.context.renderWorkspace();
  assert.doesNotMatch(app.nodes['#app'].innerHTML,/data-action="export"/);
  vm.runInContext("state.tab='refine'",app.context);app.context.renderWorkspace();
  assert.doesNotMatch(app.nodes['#app'].innerHTML,/data-action="refinement-editor"/);
});
