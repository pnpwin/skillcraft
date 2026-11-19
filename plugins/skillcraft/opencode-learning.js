import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';

export function selectTurn(messages) {
  const turns = completedTurns(messages);
  const lastUserIndex = messages.findLastIndex((message) => message.type === 'user');
  if (lastUserIndex < 0) return null;
  const latestCompleted = turns.at(-1);
  if (latestCompleted && messages.findIndex((message) => message.id === latestCompleted.user.id) === lastUserIndex) return latestCompleted;
  const previousFinalIndex = messages.findLastIndex((message) => message.type === 'assistant' && message.time?.completed && message.finish === 'stop');
  const previousUserIndex = previousFinalIndex >= 0
    ? messages.findLastIndex((message, index) => index < previousFinalIndex && message.type === 'user')
    : -1;
  const latestPriorUserIndex = messages.findLastIndex((message, index) => index < lastUserIndex && message.type === 'user');
  return {
    user: messages[lastUserIndex],
    final: null,
    targetTurn: previousUserIndex >= 0 && previousUserIndex === latestPriorUserIndex ? messages[previousUserIndex].id : undefined,
    segment: messages.slice(previousFinalIndex >= 0 ? previousFinalIndex + 1 : 0),
  };
}

export function completedTurns(messages) {
  const users = messages.flatMap((message, index) => message.type === 'user' ? [index] : []);
  const turns = [];
  let previousFinalIndex = -1;
  for (let userNumber = 0; userNumber < users.length; userNumber += 1) {
    const userIndex = users[userNumber];
    const nextUserIndex = users[userNumber + 1] ?? messages.length;
    const finalIndex = messages.findLastIndex((message, index) => index > userIndex && index < nextUserIndex
      && message.type === 'assistant' && message.time?.completed && message.finish === 'stop');
    if (finalIndex < 0) continue;
    const previousUserIndex = userNumber > 0 ? users[userNumber - 1] : -1;
    const previousTurnCompleted = previousUserIndex >= 0
      && messages.slice(previousUserIndex + 1, userIndex).some((message) => message.type === 'assistant' && message.time?.completed && message.finish === 'stop');
    turns.push({
      user: messages[userIndex],
      final: messages[finalIndex],
      targetTurn: previousTurnCompleted ? messages[previousUserIndex].id : undefined,
      segment: messages.slice(userIndex, finalIndex),
    });
    previousFinalIndex = finalIndex;
  }
  return turns;
}

export function initialCursor(messages, includePending = false) {
  if (includePending) return completedTurns(messages).at(-1)?.user.id;
  return messages.findLast((message) => message.type === 'user')?.id;
}

export function turnsAfter(messages, cursor) {
  const cursorIndex = cursor === undefined ? -1 : messages.findIndex((message) => message.type === 'user' && message.id === cursor);
  if (cursor !== undefined && cursorIndex < 0) return [];
  const completedByUser = new Map(completedTurns(messages).map((turn) => [turn.user.id, turn]));
  const users = messages.flatMap((message, index) => message.type === 'user' && index > cursorIndex ? [{ message, index }] : []);
  const turns = [];
  for (let userNumber = 0; userNumber < users.length; userNumber += 1) {
    const { message: user, index: userIndex } = users[userNumber];
    const completed = completedByUser.get(user.id);
    if (completed) {
      turns.push(completed);
      continue;
    }
    const nextUserIndex = users[userNumber + 1]?.index ?? messages.length;
    const previousFinal = messages.findLastIndex((item, index) => index < userIndex && item.type === 'assistant' && item.time?.completed && item.finish === 'stop');
    const previousUser = previousFinal >= 0 ? messages.findLastIndex((item, index) => index < previousFinal && item.type === 'user') : -1;
    const latestPriorUser = messages.findLastIndex((item, index) => index < userIndex && item.type === 'user');
    turns.push({
      user,
      final: null,
      targetTurn: previousUser >= 0 && previousUser === latestPriorUser ? messages[previousUser].id : undefined,
      segment: messages.slice(previousFinal >= 0 ? previousFinal + 1 : 0, nextUserIndex),
    });
  }
  return turns;
}

export async function reconcileProjection(messages, state, capture) {
  for (const turn of turnsAfter(messages, state.cursor)) {
    const key = `${turn.user.id}/${turn.final?.id ?? 'pending'}`;
    if (turn.final && state.completed.has(key)) continue;
    if (!turn.final && state.pendingSeen.has(key)) continue;
    const accepted = await capture(turn);
    if (!accepted) break;
    if (turn.final) {
      state.completed.add(key);
      state.cursor = turn.user.id;
      state.pendingSeen.delete(`${turn.user.id}/pending`);
    } else state.pendingSeen.add(key);
  }
}

export function messageText(message) {
  if (message.type === 'user') return message.text;
  if (message.type !== 'assistant' || !message.time?.completed || message.finish !== 'stop') return '';
  return message.content.filter((part) => part.type === 'text').map((part) => part.text).join('\n').trim();
}

export function spawnBounded(command, args, options, { timeoutMs = 10_000, input, captureStdout = false } = {}) {
  return new Promise((resolveResult) => {
    let settled = false;
    let stdout = '';
    let forceKillTimer;
    const child = spawn(command, args, options);
    let timeoutTimer;
    const finish = (result, preserveForceKill = false) => {
      if (settled) {
        clearTimeout(forceKillTimer);
        return;
      }
      settled = true;
      clearTimeout(timeoutTimer);
      if (!preserveForceKill) clearTimeout(forceKillTimer);
      resolveResult(result);
    };
    timeoutTimer = setTimeout(() => {
      child.kill('SIGTERM');
      forceKillTimer = setTimeout(() => child.kill('SIGKILL'), 200);
      forceKillTimer.unref?.();
      finish(null, true);
    }, timeoutMs);
    timeoutTimer.unref?.();
    if (captureStdout) {
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => { stdout += chunk; });
    }
    child.once('error', () => finish(null));
    child.once('close', (code) => finish({ code, stdout }));
    if (input !== undefined) {
      child.stdin.on('error', () => {});
      child.stdin.end(input);
    }
  });
}

export async function verifiedSkills(skillMessages, catalog, inventory) {
  const skillsByID = new Map(catalog.map((entry) => [entry.id, entry]));
  const enrolledByPath = new Map(inventory.map((entry) => [entry.path, entry]));
  const loaded = [];
  for (const attached of skillMessages) {
    const entry = skillsByID.get(attached.skill);
    if (!entry?.path) continue;
    try {
      const bytes = await readFile(entry.path);
      const source = bytes.toString('utf8');
      const frontmatter = /^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/.exec(source);
      const currentBody = frontmatter ? source.slice(frontmatter[0].length) : source;
      const enrolled = enrolledByPath.get(entry.path);
      const hash = createHash('sha256').update(bytes).digest('hex');
      if (!enrolled || enrolled.hash !== hash || attached.text !== entry.content || attached.text !== currentBody) continue;
      loaded.push({ path: entry.path, hash, snapshot_hash: enrolled.snapshot_hash });
    } catch {
      // An attachment without a readable source cannot certify a file binding.
    }
  }
  return loaded;
}

export async function consumeWithReconnect(subscribe, consume, signal, {
  baseDelayMs = 100,
  maxDelayMs = 5000,
  onConnected,
} = {}) {
  let delayMs = baseDelayMs;
  while (!signal.aborted) {
    const connectionAbort = new AbortController();
    const abortConnection = () => connectionAbort.abort();
    signal.addEventListener('abort', abortConnection, { once: true });
    let iterator;
    let streamEnded = false;
    try {
      const events = subscribe(connectionAbort.signal);
      iterator = events[Symbol.asyncIterator]();
      // Attach next() before reconciliation: SDK streams are lazy iterables.
      // Convert rejection to data immediately so it cannot become unhandled
      // while onConnected is still reading the durable projection.
      let nextResult = iterator.next().then(
        (result) => ({ result }),
        (error) => ({ error }),
      );
      if (onConnected) await onConnected();
      while (!signal.aborted) {
        const outcome = await nextResult;
        if (outcome.error) throw outcome.error;
        const { value: event, done } = outcome.result;
        if (done) {
          streamEnded = true;
          break;
        }
        if (signal.aborted) return;
        try {
          await consume(event);
        } catch {
          // A single event must not take down the subscription.
        }
        if (!signal.aborted) {
          nextResult = iterator.next().then(
            (result) => ({ result }),
            (error) => ({ error }),
          );
        }
      }
    } catch {
      // Reconnect after a dropped or failed event stream.
    } finally {
      signal.removeEventListener('abort', abortConnection);
      if (iterator && !streamEnded) {
        connectionAbort.abort();
        try {
          await iterator.return?.();
        } catch {
          // Closing a failed stream is best-effort.
        }
      }
    }
    if (signal.aborted) return;
    await new Promise((resolveDelay) => {
      if (signal.aborted) return resolveDelay();
      const timer = setTimeout(done, delayMs);
      function done() {
        signal.removeEventListener('abort', onAbort);
        resolveDelay();
      }
      function onAbort() {
        clearTimeout(timer);
        done();
      }
      signal.addEventListener('abort', onAbort, { once: true });
    });
    delayMs = Math.min(maxDelayMs, Math.max(baseDelayMs, delayMs * 2));
  }
}
