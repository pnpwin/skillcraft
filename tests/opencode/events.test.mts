import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Events, isBusy, type SessionLogItem, type SessionMessageInfo } from '../../plugins/skillcraft/skills/skillcraft/scripts/opencode-runner/src/events.ts';

const tokens = { input: 11, output: 7, reasoning: 3, cache: { read: 5, write: 2 } };
test('historical busy timestamp is not current activity after idle', () => {
  assert.equal(isBusy({ busy: 10, idle: 20 }), false);
  assert.equal(isBusy({ busy: 30, idle: 20 }), true);
  assert.equal(isBusy({ busy: 10 }), true);
  assert.equal(isBusy({}), false);
});
function event(seq: number, type: string, extra: object = {}): SessionLogItem {
  return { id: `ev-${seq}`, created: seq, type, durable: { aggregateID: 'ses_test', seq, version: 1 },
    data: { sessionID: 'ses_test', assistantMessageID: 'msg_test', id: 'call_test', ...extra } } as SessionLogItem;
}

test('replayed events, watermark and reconnection do not double count usage', () => {
  const e = new Events();
  const first = event(1, 'session.step.ended', { tokens, cost: 0.01, finish: 'stop' });
  assert.equal(e.accept(first), true);
  assert.equal(e.accept(first), false);
  e.accept({ type: 'log.synced', aggregateID: 'ses_test', seq: 3 });
  assert.equal(e.after, 3);
  assert.equal(e.accept(event(2, 'session.step.ended', { tokens, cost: 1 })), false);
  e.accept(event(4, 'session.usage.recorded', { source: 'title', tokens, cost: 0.02 }));
  assert.equal(e.executor?.total_tokens, 28);
  assert.equal(e.maintenance?.total_tokens, 28);
  assert.equal(e.executor?.cost, 0.01);
});

test('other tools and failed skill loads are not successful triggering', () => {
  const e = new Events();
  e.accept(event(1, 'session.tool.input.started', { name: 'read' }));
  e.accept(event(2, 'session.tool.success', { content: [], executed: true }));
  assert.equal(e.triggered('candidate', false), null);
  e.accept(event(3, 'session.tool.input.started', { name: 'skill', id: 'skill-1' }));
  e.accept(event(4, 'session.tool.called', { id: 'skill-1', input: { id: 'candidate' }, executed: true }));
  e.accept(event(5, 'session.tool.failed', { id: 'skill-1', error: { message: 'permission denied' }, executed: true }));
  assert.equal(e.triggered('candidate', true), null);
  assert.equal(e.policyBlocked, true);
  e.accept(event(6, 'session.tool.input.started', { name: 'skill', id: 'skill-2' }));
  e.accept(event(7, 'session.tool.called', { id: 'skill-2', input: { id: 'candidate' }, executed: true }));
  e.accept(event(8, 'session.tool.success', { id: 'skill-2', content: [], executed: true }));
  assert.equal(e.triggered('candidate', false), true);
});

test('an unexecuted skill result does not prove a load or a skip', () => {
  const e = new Events();
  e.accept(event(1, 'session.tool.input.started', { name: 'skill' }));
  e.accept(event(2, 'session.tool.called', { input: { id: 'candidate' }, executed: false }));
  e.accept(event(3, 'session.tool.success', { content: [], executed: false }));
  assert.equal(e.triggered('candidate', true), null);
  e.reconcile([{ id: 'msg_test', type: 'assistant', time: { created: 1, completed: 2 },
    model: { providerID: 'test', id: 'test' }, agent: 'test',
    content: [{ type: 'tool', id: 'call_test', name: 'skill', time: { created: 1 },
      state: { status: 'completed', input: { id: 'candidate' }, content: [] } }] }] as SessionMessageInfo[], 'ses_test');
  assert.equal(e.triggered('candidate', true), null);
  assert.equal(e.triggered('another-candidate', true), null);
});

test('durable message recovery confirms tools and deduplicates late step events', () => {
  const e = new Events();
  const messages = [{ id: 'msg_test', type: 'assistant', time: { created: 1, completed: 2 }, model: { providerID: 'test', id: 'test' }, agent: 'test',
    tokens, cost: 0.01, content: [{ type: 'tool', id: 'tool-1', name: 'skill', time: { created: 1 }, state: { status: 'completed', input: { id: 'candidate' }, content: [{ type: 'text', text: 'loaded' }] } }] },
    { id: 'idle', type: 'idle', time: { created: 3 }, outcome: 'succeeded' }] as SessionMessageInfo[];
  e.reconcile(messages, 'ses_test');
  e.reconcile(messages, 'ses_test');
  e.accept(event(4, 'session.step.ended', { tokens, cost: 0.01, finish: 'stop' }));
  assert.equal(e.executor?.total_tokens, 28);
  assert.equal(e.triggered('candidate', true), true);
  assert.equal(e.outcome, 'completed');
  assert.equal(e.projectionFallback, true);
});

test('tool IDs are scoped to messages and sessions', () => {
  const e = new Events();
  e.accept(event(1, 'session.tool.input.started', { name: 'skill' }));
  e.accept(event(2, 'session.tool.called', { input: { id: 'candidate' } }));
  e.accept(event(3, 'session.tool.success', { assistantMessageID: 'different', content: [], executed: true }));
  assert.equal(e.triggered('candidate', true), null);
});

test('missing projected cost remains unknown while tokens are usable', () => {
  const e = new Events();
  e.reconcile([{ id: 'msg_unknown_cost', type: 'assistant', time: { created: 1, completed: 2 },
    model: { providerID: 'test', id: 'test' }, agent: 'test', tokens, content: [] }], 'ses_test');
  assert.equal(e.executor?.total_tokens, 28);
  assert.equal(e.executor?.cost, null);
});
