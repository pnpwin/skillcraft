import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { consumeWithReconnect, initialCursor, messageText, reconcileProjection, selectTurn, spawnBounded, turnsAfter, verifiedSkills } from '../../plugins/skillcraft/opencode-learning.js';

test('selects only the latest completed turn and targets the prior pair key', () => {
  const messages = [
    { id: 'u1', type: 'user', text: 'first' },
    { id: 'a1', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [{ type: 'text', text: 'answer 1' }] },
    { id: 'u2', type: 'user', text: 'confirmed' },
    { id: 's2', type: 'skill', skill: 'demo', text: 'skill body' },
    { id: 'child', type: 'synthetic', payload: { text: 'ignore' } },
    { id: 'a2-tool', type: 'assistant', time: { completed: 2 }, finish: 'tool-calls', content: [] },
    { id: 'a2', type: 'assistant', time: { completed: 3 }, finish: 'stop', content: [{ type: 'text', text: ' final answer ' }] },
  ];
  const selected = selectTurn(messages);
  assert.equal(selected.user.id, 'u2');
  assert.equal(selected.final.id, 'a2');
  assert.equal(selected.targetTurn, 'u1');
  assert.deepEqual(selected.segment.map((message) => message.id), ['u2', 's2', 'child', 'a2-tool']);
  assert.equal(messageText(selected.final), 'final answer');
});

test('records a persisted user message even when no assistant response completed', () => {
  const selected = selectTurn([
    { id: 'prior-user', type: 'user', text: 'prior' },
    { id: 'prior-answer', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [] },
    { id: 'u', type: 'user', text: 'prompt' },
    { id: 'a', type: 'assistant', time: { completed: 1 }, finish: 'tool-calls', content: [] },
  ]);
  assert.equal(selected.user.id, 'u');
  assert.equal(selected.final, null);
  assert.equal(selected.targetTurn, 'prior-user');
  assert.equal(messageText(selected.user), 'prompt');
});

test('does not target an older completed turn across an interrupted user turn', () => {
  const selected = selectTurn([
    { id: 'uA', type: 'user', text: 'A' },
    { id: 'aA', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [] },
    { id: 'uB', type: 'user', text: 'B' },
    { id: 'toolB', type: 'assistant', time: { completed: 2 }, finish: 'tool-calls', content: [] },
    { id: 'uC', type: 'user', text: 'C' },
    { id: 'aC', type: 'assistant', time: { completed: 3 }, finish: 'stop', content: [] },
  ]);
  assert.equal(selected.user.id, 'uC');
  assert.equal(selected.targetTurn, undefined);
});

test('does not carry a skill attachment across an interrupted turn boundary', () => {
  const selected = selectTurn([
    { id: 'u1', type: 'user', text: 'first' },
    { id: 'skill1', type: 'skill', skill: 'demo', text: 'skill body' },
    { id: 'interrupt1', type: 'assistant', time: { completed: 1 }, finish: 'tool-calls', content: [] },
    { id: 'u2', type: 'user', text: 'second' },
    { id: 'a2', type: 'assistant', time: { completed: 2 }, finish: 'stop', content: [] },
  ]);
  assert.equal(selected.user.id, 'u2');
  assert.deepEqual(selected.segment.map((message) => message.id), ['u2']);
});

test('reconciliation includes an interrupted unseen user before a later completed turn', () => {
  const messages = [
    { id: 'uA', type: 'user', text: 'A' },
    { id: 'aA', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [] },
    { id: 'uB', type: 'user', text: 'B' },
    { id: 'toolB', type: 'assistant', time: { completed: 2 }, finish: 'tool-calls', content: [] },
    { id: 'uC', type: 'user', text: 'C' },
    { id: 'aC', type: 'assistant', time: { completed: 3 }, finish: 'stop', content: [] },
  ];
  const unseen = turnsAfter(messages, 'uA');
  assert.deepEqual(unseen.map((turn) => [turn.user.id, turn.final?.id]), [['uB', undefined], ['uC', 'aC']]);
  assert.equal(unseen[1].targetTurn, undefined);
});

test('reconnect reconciliation recovers every completed turn whose idle event was lost', async () => {
  const controller = new AbortController();
  let projection = [
    { id: 'uA', type: 'user', text: 'A' },
    { id: 'aA', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [] },
  ];
  let state;
  let connections = 0;
  const recovered = [];
  await consumeWithReconnect((signal) => ({
    async *[Symbol.asyncIterator]() {
      connections += 1;
      if (connections === 1) {
        yield 'disconnect';
        projection = [
          ...projection,
          { id: 'uB', type: 'user', text: 'B' },
          { id: 'aB', type: 'assistant', time: { completed: 2 }, finish: 'stop', content: [] },
          { id: 'uC', type: 'user', text: 'C' },
          { id: 'aC', type: 'assistant', time: { completed: 3 }, finish: 'stop', content: [] },
        ];
        throw new Error('event stream disconnected before idle events');
      }
      signal.aborted || controller.abort();
    },
  }), async () => {}, controller.signal, {
    baseDelayMs: 1,
    maxDelayMs: 2,
    onConnected: async () => {
      if (!state) state = { cursor: initialCursor(projection), completed: new Set(), pendingSeen: new Set() };
      else await reconcileProjection(projection, state, async (turn) => {
        recovered.push(turn.user.id);
        return true;
      });
    },
  });
  assert.deepEqual(recovered, ['uB', 'uC']);
  assert.equal(state.cursor, 'uC');
});

test('binds native attachment only when its body, file hash, and enrolled snapshot match', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'skillcraft-opencode-'));
  try {
    const file = join(directory, 'SKILL.md');
    const source = '---\nname: demo\n---\n\nExact body\n';
    const body = '\nExact body\n';
    await writeFile(file, source);
    const hash = createHash('sha256').update(await readFile(file)).digest('hex');
    const snapshotHash = 'snapshot-at-turn-start';
    const message = { skill: 'demo', text: body };
    const entry = { id: 'demo', path: file, content: body };
    const inventory = [{ path: file, hash, snapshot_hash: snapshotHash }];
    assert.deepEqual(await verifiedSkills([message], [entry], inventory), [
      { path: file, hash, snapshot_hash: snapshotHash },
    ]);
    assert.deepEqual(await verifiedSkills([{ ...message, text: 'changed body' }], [entry], inventory), []);
    assert.deepEqual(await verifiedSkills([message], [entry], [{ ...inventory[0], hash: 'changed' }]), []);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('keeps consuming after a per-event error and reconnects when an iterator ends', async () => {
  const controller = new AbortController();
  const consumed = [];
  let subscriptions = 0;
  await consumeWithReconnect((signal) => ({
    async *[Symbol.asyncIterator]() {
      subscriptions += 1;
      if (subscriptions === 1) {
        yield 'throws';
        yield 'continues';
      } else {
        signal.aborted || controller.abort();
      }
    },
  }), async (event) => {
    consumed.push(event);
    if (event === 'throws') throw new Error('isolated event failure');
  }, controller.signal, { baseDelayMs: 1, maxDelayMs: 2 });
  assert.deepEqual(consumed, ['throws', 'continues']);
  assert.equal(subscriptions, 2);
});

test('starts a lazy stream read before projection reconciliation and buffers its first event', async () => {
  const controller = new AbortController();
  let attached = false;
  let releaseFirst;
  const consumed = [];
  await consumeWithReconnect(() => ({
    [Symbol.asyncIterator]() {
      attached = true;
      let sent = false;
      return {
        next() {
          if (!sent) {
            sent = true;
            return new Promise((resolveNext) => { releaseFirst = resolveNext; });
          }
          return Promise.resolve({ done: true });
        },
        return() { return Promise.resolve({ done: true }); },
      };
    },
  }), async (event) => {
    consumed.push(event);
    controller.abort();
  }, controller.signal, {
    onConnected: async () => {
      assert.equal(attached, true, 'iterator must be active before reading the durable projection');
      await new Promise((resolveTick) => setTimeout(resolveTick, 1));
      releaseFirst({ value: 'idle-during-reconcile', done: false });
    },
  });
  assert.deepEqual(consumed, ['idle-during-reconcile']);
});

test('aborts a hung child process within the configured deadline', async () => {
  const started = Date.now();
  const result = await spawnBounded(process.execPath, ['-e', 'setTimeout(() => {}, 5000)'], {
    stdio: ['ignore', 'ignore', 'ignore'],
  }, { timeoutMs: 30 });
  assert.equal(result, null);
  assert.ok(Date.now() - started < 1000, 'subprocess timeout should bound collection latency');
});
