import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

export function selectTurn(messages) {
  const finalIndex = messages.findLastIndex((message) => message.type === 'assistant' && message.time?.completed && message.finish === 'stop');
  const lastUserIndex = messages.findLastIndex((message) => message.type === 'user');
  if (lastUserIndex < 0) return null;
  if (lastUserIndex > finalIndex) {
    const previousUserIndex = finalIndex >= 0
      ? messages.findLastIndex((message, index) => index < finalIndex && message.type === 'user')
      : -1;
    return {
      user: messages[lastUserIndex],
      final: null,
      targetTurn: previousUserIndex >= 0 ? messages[previousUserIndex].id : undefined,
      segment: messages.slice(finalIndex >= 0 ? finalIndex + 1 : 0),
    };
  }
  const userIndex = messages.findLastIndex((message, index) => index < finalIndex && message.type === 'user');
  if (userIndex < 0) return null;
  const previousFinalIndex = messages.findLastIndex((message, index) => index < userIndex && message.type === 'assistant' && message.time?.completed && message.finish === 'stop');
  const previousUserIndex = previousFinalIndex >= 0
    ? messages.findLastIndex((message, index) => index < previousFinalIndex && message.type === 'user')
    : -1;
  return {
    user: messages[userIndex],
    final: messages[finalIndex],
    targetTurn: previousUserIndex >= 0 ? messages[previousUserIndex].id : undefined,
    segment: messages.slice(previousFinalIndex >= 0 ? previousFinalIndex + 1 : 0, finalIndex),
  };
}

export function messageText(message) {
  if (message.type === 'user') return message.text;
  if (message.type !== 'assistant' || !message.time?.completed || message.finish !== 'stop') return '';
  return message.content.filter((part) => part.type === 'text').map((part) => part.text).join('\n').trim();
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
