import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import vm from 'node:vm';

const script = readFileSync(new URL('../../src/fde_server/static/app.js', import.meta.url), 'utf8');

const unescape = value => value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
export function formValue(html, name) {
  const inputs = [...html.matchAll(new RegExp(`name="${name}"[^>]*value="([^"]*)"`, 'g'))].map(match => unescape(match[1]));
  if (name === 'models') return inputs;
  if (inputs.length) return inputs[0];
  const select = html.match(new RegExp(`<select[^>]*name="${name}"[^>]*>([\\s\\S]*?)</select>`));
  assert.ok(select, `Missing field ${name}`);
  const chosen = select[1].match(/<option value="([^"]*)" selected/) || select[1].match(/<option value="([^"]*)"/);
  return unescape(chosen?.[1] || '');
}

// Execute the shipped application with only DOM/network boundaries replaced.
// In particular, use its real action -> submit -> providerModal -> api flow.
export function application(catalogModels = ['fast-chat', 'reasoner'], respond = () => undefined) {
  const requests = [];
  const nodes = Object.fromEntries(['#app', '#toast', '#dialog-content'].map(id => [id, {innerHTML: '', style: {}}]));
  const dialog = {
    open: true,
    submitButton: {},
    querySelector() { return this.submitButton; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    addEventListener() {},
  };
  nodes['#dialog'] = dialog;
  class FormData {
    constructor(form) { this.values = form.values; }
    entries() { return this.values[Symbol.iterator](); }
    getAll(name) { return this.values.filter(([key]) => key === name).map(([, value]) => value); }
  }
  const context = vm.createContext({
    document: {querySelector: id => nodes[id], cookie: 'fde_csrf=test-csrf', addEventListener() {}},
    location: {pathname: '/invite', hash: ''},
    URLSearchParams, FormData, crypto: {randomUUID}, setTimeout: () => 0, clearTimeout() {},
    fetch: async (path, options) => {
      requests.push({path, ...options, body: options.body ? JSON.parse(options.body) : undefined});
      const data = respond(path, options) ?? (path.endsWith('/catalog') ? {models: catalogModels} :
        path === '/api/session' ? {user: {}, tenants: [], providers: [], models: []} : []);
      return {ok: true, json: async () => data};
    },
  });
  vm.runInContext(script, context);
  const form = (kind, values, id) => {
    const elements = {
      '#provider-catalog': {innerHTML: ''}, '#provider-catalog-search': {value: ''},
      '#provider-manual-model': {value: '', focus() {}}, '#provider-catalog-message': {textContent: ''},
      '#provider-catalog-no-match': {hidden: true}, 'button[type=submit]': {}, '.error-slot': {},
    };
    return {
      dataset: {kind, id}, isConnected: true,
      get values() {
        const html = elements['#provider-catalog'].innerHTML;
        const current = html ? {...values, models: formValue(html, 'models'), default_model: formValue(html, 'default_model')} : values;
        return Object.entries(current).flatMap(([name, value]) => (Array.isArray(value) ? value : [value]).map(item => [name, item]));
      },
      querySelector: selector => elements[selector] || {focus() {}},
      querySelectorAll: () => [],
    };
  };
  const source = (kind, values, id) => {
    const f = form(kind, values, id);
    return {dataset: {action: 'fetch-provider-models'}, closest: () => f};
  };
  const apply = models => context.submit({dataset: {kind: 'model-picker'}, values: models.map(m => ['selected_models', m])});
  const input = name => formValue(nodes['#dialog-content'].innerHTML, name);
  return {context, nodes, requests, form, source, apply, input};
}
