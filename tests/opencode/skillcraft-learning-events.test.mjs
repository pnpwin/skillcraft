import assert from 'node:assert/strict';
import test from 'node:test';
import { initialCursor, learningEventPhase, learningEventTypes } from '../../plugins/skillcraft/opencode-learning.js';

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
    ['start', 'start', 'finish', 'finish', 'finish']);
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
