import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { messageText, selectTurn, verifiedSkills } from '../../plugins/skillcraft/opencode-learning.js';

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
