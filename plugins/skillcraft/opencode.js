import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { Plugin, Skill } from '@opencode/plugin';
import { parse } from 'yaml';
import { consumeWithReconnect, initialCursor, messageText, reconcileProjection, selectTurn, spawnBounded, verifiedSkills } from './opencode-learning.js';

const skillID = Skill.ID.make('skillcraft');
const skillPath = fileURLToPath(new URL('./skills/skillcraft/SKILL.md', import.meta.url));
const learningPath = fileURLToPath(new URL('./skills/skillcraft/scripts/learning.py', import.meta.url));

function pythonExecutable() {
  const configured = process.env.SKILLCRAFT_VENV;
  const dataDirectory = process.env.XDG_DATA_HOME || resolve(homedir(), '.local/share');
  const candidate = configured
    ? resolve(configured, 'bin/python')
    : resolve(dataDirectory, 'skillcraft/venv/bin/python');
  return existsSync(candidate) ? candidate : (process.env.PYTHON || 'python3');
}

async function enabled(project) {
  let directory = resolve(project);
  while (true) {
    try {
      const config = JSON.parse(await readFile(resolve(directory, '.skillcraft/learning.json'), 'utf8'));
      return config?.schema_version === 1 && config.enabled === true && (!Array.isArray(config.sources) || config.sources.includes('sessions'));
    } catch (error) {
      if (error?.code !== 'ENOENT' && error?.code !== 'ENOTDIR') return false;
    }
    const parent = dirname(directory);
    if (parent === directory) return false;
    directory = parent;
  }
}

export async function observe(input) {
  const result = await spawnBounded(pythonExecutable(), ['-B', learningPath, 'observe'], {
    cwd: input.project,
    stdio: ['pipe', 'ignore', 'ignore'],
    env: process.env,
  }, { input: JSON.stringify(input) });
  return result?.code === 0;
}

export async function inventory(project) {
  const result = await spawnBounded(pythonExecutable(), ['-B', learningPath, 'inventory', project], {
    cwd: project,
    stdio: ['ignore', 'pipe', 'ignore'],
    env: process.env,
  }, { captureStdout: true });
  if (result?.code !== 0) return null;
  try {
    const data = JSON.parse(result.stdout);
    return Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

function sessionState(messages, includePending = false) {
  return {
    cursor: initialCursor(messages, includePending),
    completed: new Set(),
    pendingSeen: new Set(),
    baseline: null,
  };
}

function baselineForTurn(state, turn, messages) {
  const baseline = state.baseline;
  if (!Array.isArray(baseline?.inventory)) return null;
  if (baseline.turnID === turn.user.id) return baseline;
  if (baseline.turnID) {
    const baselineIndex = messages.findIndex((message) => message.type === 'user' && message.id === baseline.turnID);
    const turnIndex = messages.findIndex((message) => message.type === 'user' && message.id === turn.user.id);
    if (baselineIndex >= 0 && turnIndex > baselineIndex) state.baseline = null;
    return null;
  }
  if (baseline.afterUserID === undefined) return baseline;
  const afterIndex = messages.findIndex((message) => message.type === 'user' && message.id === baseline.afterUserID);
  const turnIndex = messages.findIndex((message) => message.type === 'user' && message.id === turn.user.id);
  return afterIndex >= 0 && turnIndex > afterIndex ? baseline : null;
}

async function captureProjection(ctx, sessionID, state) {
  const session = await ctx.session.get({ sessionID });
  if (!session?.id || session.parentID || session.metadata?.skillcraft === true) return;
  const project = session.location.directory;
  if (!(await enabled(project))) return;
  const messages = await ctx.session.context({ sessionID });
  await reconcileProjection(messages, state, async (turn) => {
    const bound = baselineForTurn(state, turn, messages);
    const now = bound ? await inventory(project) : null;
    const baselineByPath = new Map((bound?.inventory || []).map((entry) => [entry.path, entry.snapshot_hash]));
    const stable = now ? now.filter((entry) => baselineByPath.get(entry.path) === entry.snapshot_hash) : [];
    let loaded = [];
    let diagnostic;
    if (!bound || !now) {
      diagnostic = 'No live skill inventory baseline was available for this OpenCode turn; captured without skill attribution.';
    } else if (turn.final) {
      const catalog = await ctx.skill.list({ location: { directory: project } });
      loaded = await verifiedSkills(turn.segment.filter((message) => message.type === 'skill'), catalog.data, stable);
      diagnostic = loaded.length ? undefined : stable.length < now.length
        ? 'The enrolled skill package changed during the turn; message captured without skill attribution.'
        : 'No readable native skill attachment matching the current installed skill body was confirmed in the persisted OpenCode session projection.';
    }
    const userRecorded = await observe({
      project, harness: 'opencode', session: sessionID, turn: turn.user.id, role: 'user',
      text: messageText(turn.user), loaded: [], target_turn: turn.targetTurn,
      diagnostic: turn.targetTurn ? undefined : 'No immediately preceding completed assistant turn is available for feedback association.',
    });
    if (!userRecorded) return false;
    if (!turn.final) {
      if (bound) state.baseline = { ...bound, turnID: turn.user.id, afterUserID: undefined };
      return true;
    }
    const assistantRecorded = await observe({
      project, harness: 'opencode', session: sessionID, turn: turn.user.id, role: 'assistant',
      text: messageText(turn.final), loaded, diagnostic,
    });
    if (assistantRecorded && bound && state.baseline === bound) state.baseline = null;
    return assistantRecorded;
  });
}

async function listSessions(ctx) {
  const sessions = [];
  let cursor;
  do {
    const page = await ctx.session.list({ directory: ctx.location.directory, limit: 100, ...(cursor ? { cursor } : {}) });
    sessions.push(...(page.data || []));
    cursor = page.cursor?.next;
  } while (cursor);
  return sessions;
}

export default Plugin.define({
  id: 'skillcraft',
  async setup(ctx) {
    const source = await readFile(skillPath, 'utf8');
    const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(source);
    if (!frontmatter) throw Error('SkillCraft is missing YAML frontmatter');
    const metadata = parse(frontmatter[1]);
    if (metadata?.name !== 'skillcraft' || typeof metadata.description !== 'string' || !metadata.description.trim()) {
      throw Error('SkillCraft frontmatter must have its package name and a description');
    }
    await ctx.skill.transform((editor) => {
      // Preserve a standalone skill already discovered in this location.
      if (editor.get(skillID)) return;
      editor.add({
        id: skillID,
        name: Skill.Name.make(metadata.name),
        description: metadata.description,
        path: Skill.Info.fields.path.make(skillPath),
        content: source.slice(frontmatter[0].length),
      });
    });
    const abort = new AbortController();
    const sessions = new Map();
    const inspectSession = async (sessionID) => {
      const session = await ctx.session.get({ sessionID });
      if (!session?.id || session.parentID || session.metadata?.skillcraft === true) return null;
      return { session, messages: await ctx.session.context({ sessionID }) };
    };
    const reconcileAll = async () => {
      const listed = await listSessions(ctx);
      for (const session of listed) {
        if (!session.id || session.parentID || session.metadata?.skillcraft === true) continue;
        const projection = await inspectSession(session.id);
        if (!projection) continue;
        let state = sessions.get(session.id);
        if (!state) {
          // Establish a durable start anchor on first sight; never backfill history.
          state = sessionState(projection.messages);
          sessions.set(session.id, state);
        } else {
          await captureProjection(ctx, session.id, state);
        }
      }
    };
    void consumeWithReconnect(
      (signal) => ctx.event.subscribe({ types: ['session.status', 'session.idle'], signal }),
      async (event) => {
        if (event.type === 'session.status') {
          const sessionID = event.data.sessionID;
          const projection = await inspectSession(sessionID);
          if (!projection) return;
          const { session, messages } = projection;
          const project = session.location.directory;
          if (event.data.status.type === 'busy') {
            let state = sessions.get(sessionID);
            if (!state) {
              state = sessionState(messages, true);
              sessions.set(sessionID, state);
            }
            const current = selectTurn(messages);
            const enrolled = await enabled(project) ? await inventory(project) : null;
            const latestUser = messages.findLast((message) => message.type === 'user');
            state.baseline = {
              inventory: Array.isArray(enrolled) ? enrolled : null,
              turnID: current && !current.final ? current.user.id : undefined,
              afterUserID: current && !current.final ? undefined : latestUser?.id,
            };
          } else if (event.data.status.type === 'idle') {
            let state = sessions.get(sessionID);
            if (!state) {
              state = sessionState(messages);
              sessions.set(sessionID, state);
            }
            await captureProjection(ctx, sessionID, state);
          }
          return;
        }
        if (event.type === 'session.idle') {
          const sessionID = event.data.sessionID;
          if (!sessions.has(sessionID)) {
            const projection = await inspectSession(sessionID);
            if (!projection) return;
            sessions.set(sessionID, sessionState(projection.messages));
          }
          await captureProjection(ctx, sessionID, sessions.get(sessionID));
        }
      },
      abort.signal,
      { onConnected: reconcileAll },
    );
    return () => abort.abort();
  },
});
