import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, unlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { consumeWithReconnect, initialCursor, messageText, reconcileProjection, selectTurn, spawnBounded, turnsAfter, verifiedSkills } from '../../plugins/skillcraft/src/opencode/learning.js';

test('failed stop does not complete a turn or become a feedback target during reconciliation', async () => {
  const messages = [
    { id: 'u1', type: 'user', text: 'first' },
    { id: 'tool1', type: 'assistant', time: { completed: 1 }, finish: 'tool-calls', content: [] },
    { id: 'bad', type: 'assistant', time: { completed: 2 }, finish: 'stop', error: { type: 'provider', message: 'failed' }, content: [{ type: 'text', text: 'partial' }] },
    { id: 'u2', type: 'user', text: 'next' },
    { id: 'a2', type: 'assistant', time: { completed: 3 }, finish: 'stop', metadata: { attempt: 2 }, content: [{ type: 'text', text: 'recovered' }] },
  ];
  assert.equal(messageText(messages[2]), '');
  const turns = turnsAfter(messages, undefined);
  assert.deepEqual(turns.map(t => [t.user.id, t.final?.id, t.targetTurn]), [['u1', undefined, undefined], ['u2', 'a2', undefined]]);
  const state = { cursor: undefined, completed: new Set(), pendingSeen: new Set() };
  const captured = [];
  await reconcileProjection(messages, state, async turn => { captured.push([turn.user.id, turn.final?.id]); return true; });
  await reconcileProjection(messages, state, async turn => { captured.push([turn.user.id, turn.final?.id]); return true; });
  assert.deepEqual(captured, [['u1', undefined], ['u2', 'a2']]);
});

test('a newer failed assistant supersedes a persisted successful-looking stop', () => {
  const messages = [
    { id: 'u1', type: 'user', text: 'first' },
    { id: 'old', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [] },
    { id: 'bad', type: 'assistant', time: { completed: 2 }, finish: 'error', error: { type: 'provider', message: 'failed' }, content: [] },
    { id: 'u2', type: 'user', text: 'next' },
  ];
  const turns = turnsAfter(messages, undefined);
  assert.equal(turns[0].final, null);
  assert.equal(turns[1].targetTurn, undefined);
  assert.equal(selectTurn(messages).targetTurn, undefined);
});

test('a newer unfinished assistant delays completion until its own final answer', () => {
  const user = { id: 'u1', type: 'user', text: 'first' };
  const old = { id: 'old', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [] };
  const current = { id: 'current', type: 'assistant', time: {}, content: [] };
  assert.equal(selectTurn([user, old, current]).final, null);
  const finished = { ...current, time: { completed: 2 }, finish: 'stop' };
  assert.equal(selectTurn([user, old, finished]).final.id, 'current');
});

test('keeps cursor and adjacent feedback semantics across several pending turns', () => {
  const messages = [
    { id: 'u0', type: 'user', text: 'first' },
    { id: 'a0', type: 'assistant', time: { completed: 1 }, finish: 'stop', content: [] },
    ...Array.from({ length: 100 }, (_, index) => ({ id: `u${index + 1}`, type: 'user', text: 'pending' })),
  ];
  const turns = turnsAfter(messages, 'u0');
  assert.equal(turns.length, 100);
  assert.equal(turns[0].targetTurn, 'u0');
  assert.ok(turns.slice(1).every((turn) => turn.targetTurn === undefined && turn.final === null));
  assert.equal(selectTurn(messages).targetTurn, undefined);
  assert.deepEqual(turnsAfter(messages, 'missing'), []);
});

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

test('binds exact raw and wrapped native skill projections to current source and saved inventory', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'skillcraft-opencode-'));
  try {
    const file = join(directory, 'SKILL.md');
    const source = '---\nname: demo\n---\n# Demo\nExact body\n';
    const body = '# Demo\nExact body\n';
    await writeFile(file, source);
    const hash = createHash('sha256').update(await readFile(file)).digest('hex');
    const snapshotHash = 'snapshot-at-turn-start';
    const entry = { id: 'demo', name: 'demo', path: file, content: body };
    const inventory = [{ path: file, hash, snapshot_hash: snapshotHash }];
    const expected = [
      { path: file, hash, snapshot_hash: snapshotHash },
    ];
    assert.deepEqual(await verifiedSkills([{ type: 'skill', skill: 'demo', text: body }], [entry], inventory), expected);

    const base = directory;
    const wrapped = `<skill_content name="demo">\n# Skill: demo\n\n# Demo\nExact body\n\nBase directory for this skill: ${base}\nRelative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.\nNote: file list is sampled.\n\n<skill_files>\n\n</skill_files>\n</skill_content>`;
    const userProjection = { type: 'user', skills: [{ id: 'demo', name: 'demo', text: wrapped }] };
    const toolProjection = {
      type: 'assistant',
      content: [{ type: 'tool', name: 'skill', executed: true, state: {
        status: 'completed', input: { id: 'demo' }, content: [{ type: 'text', text: wrapped }],
      } }],
    };
    assert.deepEqual(await verifiedSkills([userProjection], [entry], inventory), expected);
    assert.deepEqual(await verifiedSkills([toolProjection], [entry], inventory), expected);
    assert.deepEqual(await verifiedSkills([userProjection, toolProjection], [entry], inventory), expected, 'duplicate native projections produce one attribution');
    assert.deepEqual(await verifiedSkills([{ ...userProjection, skills: [{ ...userProjection.skills[0], text: wrapped.replace('Exact body', 'tampered') }] }], [entry], inventory), []);
    assert.deepEqual(await verifiedSkills([{ ...userProjection, skills: [{ ...userProjection.skills[0], id: 'file:///demo' }] }], [entry], inventory), []);
    assert.deepEqual(await verifiedSkills([{ ...toolProjection, content: [{ ...toolProjection.content[0], executed: false }] }], [entry], inventory), expected, 'completed durable tool output remains evidence when executed is false');
    assert.deepEqual(await verifiedSkills([{ ...toolProjection, content: [{ ...toolProjection.content[0], state: { ...toolProjection.content[0].state, status: 'error' } }] }], [entry], inventory), []);
    assert.deepEqual(await verifiedSkills([{ type: 'assistant', content: [{ type: 'text', text: wrapped }] }], [entry], inventory), [], 'assistant prose is not native tool evidence');
    assert.deepEqual(await verifiedSkills([{ type: 'skill', skill: 'demo', text: 'changed body' }], [entry], inventory), []);
    assert.deepEqual(await verifiedSkills([{ type: 'skill', skill: 'demo', text: body }], [entry], [{ ...inventory[0], hash: 'changed' }]), []);
    assert.deepEqual(await verifiedSkills([{ type: 'skill', skill: 'demo', text: body }], [entry, entry], inventory), [], 'ambiguous catalog identities are rejected');
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('matches native skill aliases only to enrolled linked targets', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'skillcraft-opencode-linked-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const target = join(directory, 'target');
  const other = join(directory, 'other');
  const alias = join(directory, 'alias');
  const content = 'Exact skill instructions\n';
  for (const root of [target, other]) {
    await mkdir(root);
    await writeFile(join(root, 'SKILL.md'), content);
  }
  await symlink(target, alias, 'dir');
  const path = await realpath(join(target, 'SKILL.md'));
  const evidence = { path, hash: createHash('sha256').update(content).digest('hex'), snapshot_hash: 'version' };
  const catalog = [{ id: 'demo', path: join(alias, 'SKILL.md'), content }];
  const segment = [{ type: 'skill', skill: 'demo', text: content }];

  assert.deepEqual(await verifiedSkills(segment, catalog, [evidence]), []);
  const inventory = [{ ...evidence, linked: true }];
  assert.deepEqual(await verifiedSkills(segment, catalog, inventory), [evidence]);
  await unlink(alias);
  await symlink(other, alias, 'dir');
  assert.deepEqual(await verifiedSkills(segment, catalog, inventory), []);
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
