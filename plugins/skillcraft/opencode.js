import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { Plugin, Skill } from '@opencode/plugin';
import { parse } from 'yaml';
import { messageText, selectTurn, verifiedSkills } from './opencode-learning.js';

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

function observe(input) {
  return new Promise((resolve) => {
    const python = spawn(pythonExecutable(), ['-B', learningPath, 'observe'], {
      cwd: input.project,
      stdio: ['pipe', 'ignore', 'ignore'],
      env: process.env,
    });
    python.once('error', () => resolve(false));
    python.once('close', (code) => resolve(code === 0));
    python.stdin.on('error', () => {});
    python.stdin.end(JSON.stringify(input));
  });
}

function inventory(project) {
  return new Promise((resolveInventory) => {
    const python = spawn(pythonExecutable(), ['-B', learningPath, 'inventory', project], {
      cwd: project,
      stdio: ['ignore', 'pipe', 'ignore'],
      env: process.env,
    });
    let stdout = '';
    python.stdout.setEncoding('utf8');
    python.stdout.on('data', (chunk) => { stdout += chunk; });
    python.once('error', () => resolveInventory(null));
    python.once('close', (code) => {
      if (code !== 0) return resolveInventory(null);
      try {
        const data = JSON.parse(stdout);
        resolveInventory(Array.isArray(data) ? data : null);
      } catch {
        resolveInventory(null);
      }
    });
  });
}

async function captureIdle(ctx, sessionID, seen, baselines) {
  const session = await ctx.session.get({ sessionID });
  if (!session?.id || session.parentID || session.metadata?.skillcraft === true) return;
  const project = session.location.directory;
  if (!(await enabled(project))) return;
  const before = baselines.get(sessionID);
  const now = await inventory(project);
  if (!now) return;
  const baselineByPath = new Map((before || []).map((entry) => [entry.path, entry.snapshot_hash]));
  const stable = now.filter((entry) => baselineByPath.get(entry.path) === entry.snapshot_hash);
  const messages = await ctx.session.context({ sessionID });
  const selected = selectTurn(messages);
  if (!selected) return;
  const { user, final, targetTurn, segment } = selected;
  const pair = `${user.id}/${final?.id ?? 'pending'}`;
  if (seen.get(sessionID) === pair) return;
  if (!final) {
    const userRecorded = await observe({ project, harness: 'opencode', session: sessionID, turn: user.id, role: 'user', text: messageText(user), loaded: [], target_turn: targetTurn,
      diagnostic: targetTurn ? 'The current OpenCode turn ended without a completed assistant message.' : 'No preceding completed assistant turn is available for feedback association.' });
    if (userRecorded) seen.set(sessionID, pair);
    return;
  }
  const skillMessages = segment.filter((message) => message.type === 'skill');
  const catalog = await ctx.skill.list({ location: { directory: project } });
  const loaded = await verifiedSkills(skillMessages, catalog.data, stable);

  const diagnostic = loaded.length ? undefined : !before
    ? 'No approved skill snapshot was recorded at turn start; message captured without skill attribution.'
    : stable.length < now.length
      ? 'The enrolled skill package changed during the turn; message captured without skill attribution.'
      : 'No readable native skill attachment matching the current installed skill body was confirmed in the persisted OpenCode session projection.';
  const userRecorded = await observe({ project, harness: 'opencode', session: sessionID, turn: user.id, role: 'user', text: messageText(user), loaded: [], target_turn: targetTurn,
    diagnostic: targetTurn ? undefined : 'No preceding completed assistant turn is available for feedback association.' });
  const assistantRecorded = await observe({ project, harness: 'opencode', session: sessionID, turn: user.id, role: 'assistant', text: messageText(final), loaded, diagnostic });
  if (userRecorded && assistantRecorded) seen.set(sessionID, pair);
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
    const seen = new Map();
    const baselines = new Map();
    const events = ctx.event.subscribe({ types: ['session.status', 'session.idle'], signal: abort.signal });
    void (async () => {
      try {
        for await (const event of events) {
          if (event.type === 'session.status') {
            const sessionID = event.data.sessionID;
            const session = await ctx.session.get({ sessionID });
            if (!session?.id || session.parentID || session.metadata?.skillcraft === true) continue;
            const project = session.location.directory;
            if (event.data.status.type === 'busy') {
              baselines.set(sessionID, await enabled(project) ? await inventory(project) : null);
            } else if (event.data.status.type === 'idle') {
              await captureIdle(ctx, sessionID, seen, baselines).catch(() => {});
            }
            continue;
          }
          if (event.type === 'session.idle') await captureIdle(ctx, event.data.sessionID, seen, baselines).catch(() => {});
        }
      } catch {
        // A stopped or disconnected event stream should not affect the host.
      }
    })();
    return () => abort.abort();
  },
});
