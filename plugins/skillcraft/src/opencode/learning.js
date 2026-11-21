import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFile, realpath } from 'node:fs/promises';
import { dirname, isAbsolute, relative, sep } from 'node:path';

export const learningEventTypes = [
  'session.inbox.enqueued', 'session.inbox.delivered',
  'session.execution.succeeded', 'session.execution.failed', 'session.execution.interrupted',
  'session.status', 'session.idle',
];

export function learningEventPhase(event) {
  switch (event.type) {
    case 'session.inbox.enqueued':
      return event.data.item.type === 'user' ? 'start' : null;
    case 'session.inbox.delivered':
      return 'start';
    case 'session.execution.succeeded':
      return 'finish';
    case 'session.execution.failed':
    case 'session.execution.interrupted':
      return 'abandon';
    case 'session.idle':
      return 'finish';
    case 'session.status':
      if (event.data.status.type === 'busy') return 'start';
      if (event.data.status.type === 'idle') return 'finish';
      return null;
    default:
      return null;
  }
}

function successfulAssistant(message) {
  return message.type === 'assistant' && message.time?.completed
    && typeof message.finish === 'string' && !['error', 'content-filter'].includes(message.finish)
    && !message.error && !message.metadata?.error;
}

function finalAssistant(message) {
  return successfulAssistant(message) && message.finish === 'stop';
}

export function completedModelSteps(segment) {
  const ids = new Set();
  for (const message of segment) {
    if (!successfulAssistant(message)) continue;
    if (typeof message.id === 'string' && message.id) ids.add(message.id);
  }
  return ids.size;
}

export function selectTurn(messages) {
  const turns = completedTurns(messages);
  const lastUserIndex = messages.findLastIndex((message) => message.type === 'user');
  if (lastUserIndex < 0) return null;
  const latestCompleted = turns.at(-1);
  if (latestCompleted && messages.findIndex((message) => message.id === latestCompleted.user.id) === lastUserIndex) return latestCompleted;
  const prior = latestCompleted;
  const previousFinalIndex = prior ? messages.findIndex((message) => message.id === prior.final.id) : -1;
  const previousUserIndex = prior ? messages.findIndex((message) => message.id === prior.user.id) : -1;
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
  for (let userNumber = 0; userNumber < users.length; userNumber += 1) {
    const userIndex = users[userNumber];
    const nextUserIndex = users[userNumber + 1] ?? messages.length;
    let finalIndex = nextUserIndex - 1;
    while (finalIndex > userIndex && messages[finalIndex].type !== 'assistant') finalIndex -= 1;
    if (finalIndex === userIndex || !finalAssistant(messages[finalIndex])) continue;
    const previousUserIndex = userNumber > 0 ? users[userNumber - 1] : -1;
    const previousTurnCompleted = previousUserIndex >= 0
      && turns.at(-1)?.user.id === messages[previousUserIndex].id;
    turns.push({
      user: messages[userIndex],
      final: messages[finalIndex],
      targetTurn: previousTurnCompleted ? messages[previousUserIndex].id : undefined,
      segment: messages.slice(userIndex, finalIndex),
    });
  }
  return turns;
}

export function initialCursor(messages, includePending = false, inboxID) {
  if (inboxID) {
    const index = messages.findIndex((message) => message.type === 'user' && message.id === inboxID);
    if (index >= 0) return messages.findLast((message, i) => i < index && message.type === 'user')?.id;
  }
  if (includePending) return completedTurns(messages).at(-1)?.user.id;
  return messages.findLast((message) => message.type === 'user')?.id;
}

export function turnsAfter(messages, cursor) {
  const cursorIndex = cursor === undefined ? -1 : messages.findIndex((message) => message.type === 'user' && message.id === cursor);
  if (cursor !== undefined && cursorIndex < 0) return [];
  const completedByUser = new Map(completedTurns(messages).map((turn) => [turn.user.id, turn]));
  const indexes = new Map(messages.map((message, index) => [message, index]));
  const users = messages.flatMap((message, index) => message.type === 'user' ? [{ message, index }] : []);
  const turns = [];
  let prior;
  for (let userNumber = 0; userNumber < users.length; userNumber += 1) {
    const { message: user, index: userIndex } = users[userNumber];
    const completed = completedByUser.get(user.id);
    if (userIndex <= cursorIndex) {
      if (completed) prior = completed;
      continue;
    }
    if (completed) {
      turns.push(completed);
      prior = completed;
      continue;
    }
    const nextUserIndex = users[userNumber + 1]?.index ?? messages.length;
    const previousFinal = prior ? indexes.get(prior.final) : -1;
    const latestPriorUser = users[userNumber - 1]?.message;
    turns.push({
      user,
      final: null,
      targetTurn: prior && prior.user === latestPriorUser ? prior.user.id : undefined,
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
  if (!finalAssistant(message)) return '';
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

function wrappedSkillMatches(text, entry) {
  if (typeof text !== 'string' || typeof entry.content !== 'string' || !entry.path) return false;
  const name = entry.name ?? entry.id;
  if (typeof name !== 'string' || !name) return false;
  const base = dirname(entry.path);
  const prefix = `<skill_content name="${name}">\n# Skill: ${name}\n\n${entry.content.trim()}\n\nBase directory for this skill: ${base}\nRelative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.\nNote: file list is sampled.\n\n<skill_files>\n`;
  if (!text.startsWith(prefix) || !text.endsWith('\n</skill_files>\n</skill_content>')) return false;
  const filesText = text.slice(prefix.length, -'\n</skill_files>\n</skill_content>'.length);
  const fileLines = filesText === '' ? [] : filesText.split('\n');
  if (fileLines.length > 10 || fileLines.some((line) => !/^<file>[^<>]+<\/file>$/.test(line))) return false;
  const seen = new Set();
  for (const line of fileLines) {
    const file = line.slice('<file>'.length, -'</file>'.length);
    if (!isAbsolute(file) || file.endsWith(`${sep}SKILL.md`) || seen.has(file)) return false;
    const pathFromBase = relative(dirname(entry.path), file);
    if (!pathFromBase || pathFromBase === '..' || pathFromBase.startsWith(`..${sep}`) || isAbsolute(pathFromBase)) return false;
    seen.add(file);
  }
  return true;
}

function nativeSkillAttachments(segment) {
  const attachments = [];
  for (const message of segment) {
    if (message.type === 'skill' && typeof message.skill === 'string') {
      attachments.push({ id: message.skill, text: message.text, raw: true });
    }
    if (message.type === 'user' && Array.isArray(message.skills)) {
      for (const skill of message.skills) {
        if (skill && typeof skill.id === 'string') attachments.push({ id: skill.id, name: skill.name, text: skill.text });
      }
    }
    if (message.type === 'assistant' && Array.isArray(message.content)) {
      for (const part of message.content) {
        if (part?.type !== 'tool' || part.name !== 'skill'
          || part.state?.status !== 'completed' || typeof part.state.input?.id !== 'string'
          || !Array.isArray(part.state.content)) continue;
        const texts = part.state.content.filter((item) => item?.type === 'text' && typeof item.text === 'string');
        if (texts.length === 1 && texts.length === part.state.content.length) {
          attachments.push({ id: part.state.input.id, text: texts[0].text });
        }
      }
    }
  }
  return attachments;
}

export async function verifiedSkills(segment, catalog, inventory) {
  const skillsByID = new Map();
  for (const entry of catalog) {
    if (typeof entry.id !== 'string' || !entry.id) continue;
    const matches = skillsByID.get(entry.id) ?? [];
    matches.push(entry);
    skillsByID.set(entry.id, matches);
  }
  const enrolledByPath = new Map();
  for (const entry of inventory) {
    const matches = enrolledByPath.get(entry.path) ?? [];
    matches.push(entry);
    enrolledByPath.set(entry.path, matches);
  }
  const hasLinkedSkills = inventory.some((entry) => entry.linked === true);
  const loaded = [];
  const seen = new Set();
  for (const attached of nativeSkillAttachments(segment)) {
    const entries = skillsByID.get(attached.id);
    if (entries?.length !== 1) continue;
    const entry = entries[0];
    if (!entry?.path || typeof entry.content !== 'string') continue;
    try {
      let sourcePath = entry.path;
      let matches = enrolledByPath.get(sourcePath);
      if (!matches && hasLinkedSkills && isAbsolute(sourcePath)) {
        sourcePath = await realpath(sourcePath);
        matches = enrolledByPath.get(sourcePath);
        if (matches?.[0]?.linked !== true) continue;
      }
      if (matches?.length !== 1) continue;
      const enrolled = matches[0];
      const bytes = await readFile(sourcePath);
      const source = bytes.toString('utf8');
      const frontmatter = /^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/.exec(source);
      const currentBody = frontmatter ? source.slice(frontmatter[0].length) : source;
      const hash = createHash('sha256').update(bytes).digest('hex');
      if (enrolled.hash !== hash || entry.content !== currentBody) continue;
      if (attached.name !== undefined && attached.name !== (entry.name ?? entry.id)) continue;
      if (attached.raw ? attached.text !== currentBody : !wrappedSkillMatches(attached.text, entry)) continue;
      const key = `${sourcePath}\0${hash}\0${enrolled.snapshot_hash}`;
      if (seen.has(key)) continue;
      seen.add(key);
      loaded.push({ path: sourcePath, hash, snapshot_hash: enrolled.snapshot_hash });
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
