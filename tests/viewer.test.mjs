import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createContext, runInContext } from 'node:vm';

const template = readFileSync(new URL(
  '../plugins/skillcraft/skills/skillcraft/assets/viewer.html', import.meta.url), 'utf8');
const script = template.split('<script>').at(-1).split('</script>')[0];
const runs = ['first', 'second', 'third'].map(id => ({
  id, snapshot_hash: 'current-' + id, prompt: 'Review ' + id,
}));

async function loadViewer({ saved, storage, onPost = async () => ({ ok: true }) } = {}) {
  const nodes = new Map();
  const posts = [];
  const requests = [];
  const timers = new Map();
  let ready;
  const initialized = new Promise(resolve => { ready = resolve; });
  const listeners = new Map();
  function element(id) {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, {
        style: {},
        classList: {
          add: name => classes.add(name),
          contains: name => classes.has(name),
          toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
        },
        addEventListener: (event, handler) => {
          listeners.set(`${id}:${event}`, handler);
          if (id === 'feedback' && event === 'input') ready();
        },
      });
    }
    return nodes.get(id);
  }
  const context = createContext({
    document: { getElementById: element, querySelector: element, addEventListener: () => {} },
    location: { pathname: '/review.html', hash: storage ? '' : '#cap=viewer-test-capability' },
    URLSearchParams,
    sessionStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    setTimeout: callback => { const id = Symbol(); timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      assert.equal(url, '/api/feedback');
      requests.push({ method: options?.method ?? 'GET', authorization: options?.headers?.Authorization });
      if (options?.method !== 'POST') return { json: async () => saved };
      const payload = JSON.parse(options.body);
      posts.push(payload);
      return onPost(payload);
    },
  });
  const data = { runs, server_available: !storage };
  runInContext(script.replace('/*__EMBEDDED_DATA__*/', 'const EMBEDDED_DATA = ' + JSON.stringify(data) + ';'), context);
  await initialized;
  return {
    posts, requests, timers, element, next: () => context.navigate(1), submit: () => context.showDoneDialog(),
    edit: text => { element('feedback').value = text; listeners.get('feedback:input')(); },
    verdict: value => { element('verdict').value = value; listeners.get('verdict:change')(); },
  };
}

test('live feedback reads and writes use the capability from the review URL', async () => {
  const viewer = await loadViewer();
  viewer.next();
  viewer.next();
  await viewer.submit();
  assert.equal(viewer.requests[0].method, 'GET');
  assert.equal(viewer.requests.at(-1).method, 'POST');
  assert.ok(viewer.requests.every(request => request.authorization === 'Bearer viewer-test-capability'));
});

test('immediate submit cancels draft timer and keeps visited runs without comments', async () => {
  const viewer = await loadViewer();
  viewer.next();
  viewer.next();
  viewer.edit('Ready for review');
  assert.equal(viewer.timers.size, 1);
  await viewer.submit();

  assert.equal(viewer.timers.size, 0);
  assert.deepEqual(viewer.posts.map(payload => payload.status), ['in_progress', 'in_progress', 'complete']);
  assert.deepEqual(viewer.posts.at(-1).reviews.map(({ reviewed, feedback }) => [reviewed, feedback]),
    [[true, ''], [true, ''], [true, 'Ready for review']]);
});

test('optional per-run verdicts survive submission and default to neutral', async () => {
  const viewer = await loadViewer();
  viewer.verdict('success');
  viewer.next();
  viewer.verdict('correction');
  viewer.edit('Use the requested format');
  viewer.next();
  await viewer.submit();

  const reviews = viewer.posts.at(-1).reviews;
  assert.deepEqual(reviews.map(review => review.verdict ?? null), ['success', 'correction', null]);
  assert.equal(reviews[1].reviewed, true);
  assert.equal(reviews[1].feedback, 'Use the requested format');
});

test('submission waits for a draft already in flight', async () => {
  let requestStarted;
  const draftStarted = new Promise(resolve => { requestStarted = resolve; });
  let releaseDraft;
  const held = new Promise(resolve => { releaseDraft = resolve; });
  const viewer = await loadViewer({
    saved: { reviews: runs.slice(1).map(run => ({
      run_id: run.id, snapshot_hash: run.snapshot_hash, reviewed: true, feedback: '',
    })) },
    onPost: payload => {
      if (payload.status === 'complete') return { ok: true };
      requestStarted();
      return held;
    },
  });
  viewer.edit('Draft');
  const [save] = viewer.timers.values();
  const saving = save();
  await draftStarted;
  viewer.edit('Final');
  const submitting = viewer.submit();
  await Promise.resolve();

  assert.deepEqual(viewer.posts.map(payload => payload.status), ['in_progress']);
  releaseDraft({ ok: true });
  await Promise.all([saving, submitting]);
  assert.deepEqual(viewer.posts.map(payload => payload.status), ['in_progress', 'complete']);
  assert.equal(viewer.posts.at(-1).reviews[0].feedback, 'Final');
  assert.equal(viewer.timers.size, 0);
});

test('live reload restores visited flags and ignores changed snapshots', async () => {
  const viewer = await loadViewer({
    saved: { reviews: [
      { run_id: 'second', snapshot_hash: runs[1].snapshot_hash, reviewed: true, feedback: '' },
      { run_id: 'third', snapshot_hash: 'old-third', reviewed: true, feedback: 'Stale feedback' },
    ] },
  });

  viewer.next();
  assert.equal(viewer.element('done-btn').classList.contains('ready'), false);
  assert.equal(viewer.posts.length, 0);
  await viewer.submit();
  assert.deepEqual(viewer.posts[0].reviews.map(({ reviewed, feedback }) => [reviewed, feedback]),
    [[true, ''], [true, ''], [false, '']]);
  assert.equal(viewer.posts[0].status, 'in_progress');
});

test('live reload restores explicit verdicts while legacy reviews stay neutral', async () => {
  const viewer = await loadViewer({ saved: { reviews: [
    { run_id: 'first', snapshot_hash: runs[0].snapshot_hash, reviewed: true, feedback: '', verdict: 'failure' },
    { run_id: 'second', snapshot_hash: runs[1].snapshot_hash, reviewed: true, feedback: '' },
  ] } });
  assert.equal(viewer.element('verdict').value, 'failure');
  viewer.next();
  assert.equal(viewer.element('verdict').value, '');
});

test('static reload restores a session draft with blank visited runs', async () => {
  const storage = new Map();
  const viewer = await loadViewer({ storage });
  viewer.next();
  viewer.verdict('success');
  viewer.edit('Static feedback');
  viewer.next();

  const refreshed = await loadViewer({ storage });
  assert.equal(refreshed.element('done-btn').classList.contains('ready'), true);
  refreshed.next();
  assert.equal(refreshed.element('verdict').value, 'success');
  assert.equal(refreshed.element('feedback').value, 'Static feedback');
  assert.equal(refreshed.posts.length, 0);
});

test('feedback validation accepts legacy verdicts and rejects invalid corrections', () => {
  const scriptRoot = fileURLToPath(new URL('../plugins/skillcraft/skills/skillcraft', import.meta.url));
  const check = `
import json, sys
sys.path.insert(0, ${JSON.stringify(scriptRoot)})
from scripts.generate_review import validate_feedback
run = {'id': 'first', 'snapshot_hash': 'current-first'}
base = {'schema_version': 1, 'status': 'complete', 'reviews': [
    {'run_id': 'first', 'snapshot_hash': 'current-first', 'reviewed': True, 'feedback': ''}
]}
validate_feedback(base, [run])
valid = json.loads(json.dumps(base))
valid['reviews'][0].update(verdict='correction', feedback='Fix the heading')
validate_feedback(valid, [run])
for changes in ({'verdict': 'maybe'}, {'verdict': 'correction'}, {'verdict': 'correction', 'feedback': 'Fix it', 'reviewed': False}):
    invalid = json.loads(json.dumps(base))
    invalid['reviews'][0].update(changes)
    try:
        validate_feedback(invalid, [run])
    except ValueError:
        continue
    raise AssertionError(f'Accepted invalid review: {changes}')
`;
  execFileSync('python3', ['-c', check]);
});
