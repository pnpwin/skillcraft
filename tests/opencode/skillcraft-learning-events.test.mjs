import assert from 'node:assert/strict';
import test from 'node:test';
import { completedModelSteps, initialCursor, learningEventPhase, learningEventTypes } from '../../plugins/skillcraft/src/opencode/learning.js';

test('learns from native OpenCode execution and inbox events without a legacy status event', () => {
  const sessionID = 'test-session';
  const events = [
    { type: 'session.inbox.enqueued', data: { sessionID, inboxID: 'test-inbox', item: { type: 'user' } } },
    { type: 'session.inbox.delivered', data: { sessionID, inboxID: 'test-inbox' } },
    { type: 'session.execution.succeeded', data: { sessionID } },
    { type: 'session.execution.failed', data: { sessionID } },
    { type: 'session.execution.interrupted', data: { sessionID } },
  ];
  assert.deepEqual(events.filter(({ type }) => learningEventTypes.includes(type)).map(learningEventPhase),
    ['start', 'start', 'finish', 'abandon', 'abandon']);
  assert.equal(learningEventPhase({ type: 'session.inbox.enqueued', data: { item: { type: 'synthetic' } } }), null);
});

test('a delayed inbox event still anchors the cursor before its own completed turn', () => {
  const messages = [
    { type: 'user', id: 'earlier' },
    { type: 'assistant', id: 'earlier-answer', finish: 'stop', time: { completed: 1 } },
    { type: 'user', id: 'inbox-turn' },
    { type: 'assistant', id: 'fast-answer', finish: 'stop', time: { completed: 2 } },
    { type: 'user', id: 'later' },
  ];
  assert.equal(initialCursor(messages, true, 'inbox-turn'), 'earlier');
  assert.equal(initialCursor(messages, true, 'later'), 'inbox-turn');
});

test('retains legacy busy and idle compatibility', () => {
  const status = (type) => ({ type: 'session.status', data: { status: { type } } });
  assert.equal(learningEventPhase(status('busy')), 'start');
  assert.equal(learningEventPhase(status('idle')), 'finish');
  assert.equal(learningEventPhase(status('retry')), null);
  assert.equal(learningEventPhase({ type: 'session.idle' }), 'finish');
});

test('counts distinct successful completed model messages, including successful retries', () => {
  assert.equal(completedModelSteps([
    { type: 'assistant', id: 'one', time: { completed: 1 }, finish: 'tool-calls', content: [] },
    { type: 'assistant', id: 'two', time: { completed: 2 }, finish: 'stop', content: [] },
    { type: 'assistant', id: 'two', time: { completed: 2 }, finish: 'stop', content: [] },
    { type: 'assistant', id: 'failed', time: { completed: 3 }, finish: 'stop', error: { name: 'ProviderError' } },
    { type: 'assistant', id: 'retry', time: { completed: 4 }, finish: 'stop', metadata: { retry: true } },
    { type: 'assistant', id: 'attempt', time: { completed: 5 }, finish: 'stop', metadata: { attempt: 2 } },
    { type: 'assistant', id: 'streaming-only', finish: 'stop' },
    { type: 'user', id: 'prompt', text: 'ignored' },
    { type: 'assistant', id: 'error-finish', time: { completed: 6 }, finish: 'error' },
    { type: 'assistant', id: 'filtered', time: { completed: 7 }, finish: 'content-filter' },
  ]), 4);
  assert.equal(completedModelSteps([]), 0);
});
