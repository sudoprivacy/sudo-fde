import assert from 'node:assert/strict';
import {readFileSync, writeFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {application, formValue} from './app-harness.mjs';

const draft = {
  provider_id: 'my-router', display_name: 'My router', api_base: 'https://router.example/v1',
  protocol: 'openai', api_key: 'sk-test-only-123', models: [], default_model: '',
};

test('new provider: discover, select, return and save without losing credentials', async () => {
  const app = application();
  await app.context.action(app.source('new-provider', draft));
  assert.match(app.nodes['#dialog-content'].innerHTML, /选择要添加的模型/);
  await app.apply(['fast-chat', 'reasoner']);
  assert.deepEqual(app.input('models'), ['fast-chat', 'reasoner']);
  assert.equal(app.input('api_key'), draft.api_key);
  assert.equal(app.input('default_model'), 'fast-chat');
  assert.equal(vm.runInContext('state.providerDraft', app.context), null);

  const values = {...draft};
  for (const name of ['provider_id', 'display_name', 'api_base', 'api_key', 'models', 'default_model']) values[name] = app.input(name);
  await app.context.submit(app.form('new-provider', values));
  const saved = app.requests.find(r => r.path === '/api/models/providers' && r.method === 'POST');
  assert.deepEqual(saved.body.models, ['fast-chat', 'reasoner']);
  assert.equal(saved.body.api_key, draft.api_key);
  assert.equal(saved.body.default_model, 'fast-chat');
});

test('adding models preserves manual IDs and default, with no duplicates', async () => {
  const app = application();
  await app.context.action(app.source('new-provider', {...draft, models: [' manual-model ', 'fast-chat'], default_model: 'manual-model'}));
  await app.apply(['fast-chat', 'reasoner', 'reasoner']);
  assert.deepEqual(app.input('models'), ['manual-model', 'fast-chat', 'reasoner']);
  assert.equal(app.input('default_model'), 'manual-model');
});

test('cancel returns to the complete unsaved provider form', async () => {
  const app = application();
  const values = {...draft, models: ['manual-model'], default_model: 'manual-model', api_key: 'sk-test-<"&123'};
  await app.context.action(app.source('new-provider', values));
  await app.context.action({dataset: {action: 'close'}, closest: () => ({dataset: {kind: 'model-picker'}})});
  for (const name of ['models', 'default_model', 'api_key', 'display_name', 'api_base']) assert.deepEqual(app.input(name), values[name]);
  assert.equal(vm.runInContext('dialog.open', app.context), true);
  assert.equal(vm.runInContext('state.providerDraft', app.context), null);
});

test('edit uses the saved credential and sends no empty replacement key', async () => {
  const app = application();
  const values = {...draft, models: ['existing'], default_model: 'existing', api_key: ''};
  delete values.provider_id;
  await app.context.action(app.source('edit-provider', values, 'my-router'));
  assert.equal(app.requests[0].path, '/api/models/providers/my-router/catalog');
  assert.equal('api_key' in app.requests[0].body, false);
  await app.apply(['reasoner']);
  assert.deepEqual(app.input('models'), ['existing', 'reasoner']);
  assert.equal(app.input('default_model'), 'existing');
  await app.context.submit(app.form('edit-provider', {...values, models: app.input('models')}, 'my-router'));
  const saved = app.requests.find(r => r.method === 'PATCH');
  assert.deepEqual(saved.body.models, ['existing', 'reasoner']);
  assert.equal('api_key' in saved.body, false);
});

test('empty selection does not consume the draft or prevent a subsequent selection', async () => {
  const app = application();
  await app.context.action(app.source('new-provider', draft));
  await assert.rejects(app.apply([]), /至少选择一个模型/);
  assert.match(app.nodes['#dialog-content'].innerHTML, /选择要添加的模型/);
  await app.apply(['reasoner']);
  assert.deepEqual(app.input('models'), ['reasoner']);
  assert.equal(app.input('api_key'), draft.api_key);
});

test('provider form accepts both persisted arrays and form strings', () => {
  const app = application();
  for (const models of [['fast-chat', 'reasoner'], 'fast-chat,reasoner']) {
    app.context.providerModal('new-provider', {...draft, models});
    assert.deepEqual(app.input('models'), ['fast-chat', 'reasoner']);
  }
});

test('selecting a complete large catalog submits every model on create and edit', async () => {
  const models = ['gpt-6-sol', ...Array.from({length: 136}, (_, i) => `catalog-model-${i}`)];
  const app = application(models);
  await app.context.action(app.source('new-provider', draft));
  assert.match(app.nodes['#dialog-content'].innerHTML, /已获取 137 个可用模型/);
  await app.apply(models);
  assert.equal(app.input('models').length, 137);
  assert.match(app.nodes['#dialog-content'].innerHTML, /已添加 137 个模型/);
  await app.context.submit(app.form('new-provider', {...draft, models: app.input('models'), default_model: app.input('default_model')}));
  const created = app.requests.find(r => r.method === 'POST' && r.path === '/api/models/providers');
  assert.deepEqual(created.body.models, models);

  const values = {...draft, models: ['manual-model'], default_model: 'manual-model', api_key: ''};
  delete values.provider_id;
  app.context.providerModal('edit-provider', {...values, provider_id: draft.provider_id});
  await app.context.action(app.source('edit-provider', values, draft.provider_id));
  await app.apply(models);
  await app.context.submit(app.form('edit-provider', {...values, models: app.input('models')}, draft.provider_id));
  const edited = app.requests.find(r => r.method === 'PATCH');
  assert.deepEqual(edited.body.models, ['manual-model', ...models]);
  assert.equal('api_key' in edited.body, false);

  // pytest replays these actual frontend requests through the real API. This
  // prevents frontend-only mocks from hiding a server-side catalog size limit.
  if (process.env.FDE_PICKER_CONTRACT_OUTPUT) {
    writeFileSync(process.env.FDE_PICKER_CONTRACT_OUTPUT, JSON.stringify([created, edited]));
  }
});

test('row editor adds and removes models, switches default, and saves only the resulting catalog', async () => {
  const app = application();
  const values = {...draft, models: ['manual-model', 'gpt-6-sol'], default_model: 'manual-model', api_key: ''};
  delete values.provider_id;
  const form = app.form('edit-provider', values, 'my-router');
  const act = (action, model) => app.context.editProviderModel({dataset: {action, model}, closest: () => form});
  const read = name => formValue(form.querySelector('#provider-catalog').innerHTML, name);
  form.querySelector('#provider-manual-model').value = ' new-manual-model ';
  act('provider-model-add');
  assert.deepEqual(read('models'), ['manual-model', 'gpt-6-sol', 'new-manual-model']);
  assert.equal(read('default_model'), 'manual-model');
  act('provider-model-add');
  assert.match(form.querySelector('#provider-catalog-message').textContent, /无需重复添加/);
  act('provider-model-default', 'new-manual-model');
  assert.equal(read('default_model'), 'new-manual-model');
  act('provider-model-remove', 'manual-model');
  assert.deepEqual(read('models'), ['gpt-6-sol', 'new-manual-model']);
  assert.equal(read('default_model'), 'new-manual-model');
  await app.context.submit(form);
  const saved = app.requests.find(request => request.method === 'PATCH');
  assert.deepEqual(saved.body.models, ['gpt-6-sol', 'new-manual-model']);
  assert.equal(saved.body.default_model, 'new-manual-model');
  assert.equal('api_key' in saved.body, false);
  if (process.env.FDE_PICKER_CONTRACT_OUTPUT) {
    const previous = JSON.parse(readFileSync(process.env.FDE_PICKER_CONTRACT_OUTPUT, 'utf8'));
    writeFileSync(process.env.FDE_PICKER_CONTRACT_OUTPUT, JSON.stringify([...previous, saved]));
  }
});

test('removing default selects a remaining model and removing all blocks saving', async () => {
  const app = application();
  const form = app.form('new-provider', {...draft, models: ['old-model', 'new-model'], default_model: 'old-model'});
  const remove = model => app.context.editProviderModel({dataset: {action: 'provider-model-remove', model}, closest: () => form});
  remove('old-model');
  assert.equal(formValue(form.querySelector('#provider-catalog').innerHTML, 'default_model'), 'new-model');
  remove('new-model');
  assert.equal(form.querySelector('button[type=submit]').disabled, true);
  await assert.rejects(app.context.submit(form), /请至少添加一个模型/);
  assert.equal(app.requests.length, 0);
});

test('manual add validates one model ID and cancel from discovery keeps row edits', async () => {
  const app = application();
  const values = {...draft, models: ['manual-model'], default_model: 'manual-model'};
  const form = app.form('new-provider', values);
  for (const invalid of ['', 'two,models', 'has spaces', 'a'.repeat(129)]) {
    form.querySelector('#provider-manual-model').value = invalid;
    app.context.editProviderModel({dataset: {action: 'provider-model-add'}, closest: () => form});
    assert.match(form.querySelector('#provider-catalog-message').textContent, /有效的模型 ID/);
    assert.equal(form.querySelector('#provider-catalog').innerHTML, '');
  }
  await app.context.action(app.source('new-provider', values));
  await app.context.action({dataset: {action: 'close'}, closest: () => ({dataset: {kind: 'model-picker'}})});
  assert.deepEqual(app.input('models'), ['manual-model']);
  assert.equal(app.input('default_model'), 'manual-model');
});

test('changing provider default invalidates only that provider workspace choices', () => {
  const app = application();
  vm.runInContext(`state.workspaceModels={one:JSON.stringify(['my-router','old']),two:JSON.stringify(['other','chosen'])}`, app.context);
  app.context.resetProviderModelChoices('my-router');
  const choices = JSON.parse(vm.runInContext('JSON.stringify(state.workspaceModels)', app.context));
  assert.deepEqual(choices, {two: JSON.stringify(['other','chosen'])});
});
