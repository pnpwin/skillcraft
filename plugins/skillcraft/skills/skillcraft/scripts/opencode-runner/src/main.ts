import { OpenCode, type ModelRef, type SessionMessageInfo } from '@opencode/client';
import { appendFile, mkdir, readFile, writeFile, realpath, readdir } from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { Events, isBusy } from './events.ts';
import { connect } from './connection.ts';

type Request = {
  schema_version: 1; operation?: 'run' | 'probe'; directory: string; output: string;
  prompt: string; model: ModelRef; agent: string; timeout_seconds: number;
  mode: 'quality' | 'trigger' | 'judge' | 'optimizer';
  skill_id?: string; skill_path?: string; skill_sha256?: string; attach_skill?: boolean;
  permissions: { action: string; resource: string; effect: 'allow' | 'deny' | 'ask' }[];
  controlled: boolean; run_id: string;
  allow_delegation?: boolean;
};
const hash = (s: string) => createHash('sha256').update(s).digest('hex');
async function json(file: string, value: unknown) {
  await writeFile(file, JSON.stringify(value, null, 2) + '\n');
}
async function exists(file: string) {
  try { await realpath(file); return true; } catch { return false; }
}

type Client = ReturnType<typeof OpenCode.make>;
async function descendants(client: Client, sessionID: string) {
  const result: Awaited<ReturnType<Client['session']['list']>>['data'] = [];
  const seen = new Set([sessionID]);
  const visit = async (parentID: string) => {
    let cursor: string | undefined;
    do {
      const page = await client.session.list({ parentID, ...(cursor ? { cursor } : {}) });
      for (const child of page.data) {
        if (seen.has(child.id)) continue;
        seen.add(child.id); result.push(child); await visit(child.id);
      }
      cursor = page.cursor.next ?? undefined;
    } while (cursor);
  };
  await visit(sessionID);
  return result;
}
async function messages(client: Client, sessionID: string): Promise<SessionMessageInfo[]> {
  const all: SessionMessageInfo[] = [];
  let cursor: string | undefined;
  const seen = new Set<string>();
  do {
    const page = await client.message.list({ sessionID, limit: 100, ...(cursor ? { cursor } : { order: 'asc' as const }) }, { signal: AbortSignal.timeout(15000) });
    all.push(...page.data);
    cursor = page.cursor.next ?? undefined;
    if (cursor && seen.has(cursor)) throw Error('Repeated message cursor');
    if (cursor) seen.add(cursor);
  } while (cursor);
  return all;
}

async function audit(client: Client, r: Request) {
  const location = { directory: r.directory };
  // Location plugin activation is asynchronous; an initial empty catalog is not authoritative.
  const readyDeadline = Date.now() + 15000;
  while (true) {
    const registry = await client.skill.list({ location });
    const plugins = await client.plugin.list({ location });
    if (plugins.data.length && registry.data.some(s => s.id === 'opencode') && (!r.skill_path || registry.data.some(s => s.id === r.skill_id))) break;
    if (Date.now() >= readyDeadline) throw Error('Location registry did not activate or candidate is missing');
    await delay(150);
  }
  const [skills, config, plugins] = await Promise.all([
    client.skill.list({ location }), client.config.get({ location }), client.plugin.list({ location }),
  ]);
  const candidate = skills.data.find(s => s.id === r.skill_id);
  if (r.skill_path) {
    if (!candidate || await realpath(candidate.path) !== await realpath(r.skill_path)) throw Error('Candidate missing or shadowed in skill catalog');
    if (hash(await readFile(candidate.path, 'utf8')) !== r.skill_sha256) throw Error('Candidate snapshot hash mismatch');
    if (r.mode === 'trigger' && (candidate.autoinvoke === false || !candidate.description)) throw Error('Candidate is not advertised');
  } else if (candidate) throw Error('Baseline candidate leaked through another source');
  const concerns: string[] = [];
  for (const entry of config) {
    if (entry.type === 'document') {
      if (entry.path === path.join(r.directory, '.opencode', 'opencode.json')) continue;
      const keys = Object.keys(entry.info).filter(k => !['$schema', 'providers', 'model', 'update', 'username'].includes(k));
      if (keys.length) concerns.push(`External config affects evaluation: ${entry.path ?? 'inline'} (${keys.join(', ')})`);
    } else {
      for (const name of ['AGENTS.md', 'agents', 'plugins', 'commands']) {
        const p = path.join(entry.path, name);
        if (await exists(p)) {
          if (name === 'AGENTS.md' || (await readdir(p)).length) concerns.push(`External instruction/extension source: ${p}`);
        }
      }
    }
  }
  for (let p = r.directory; ; p = path.dirname(p)) {
    if (await exists(path.join(p, 'AGENTS.md'))) concerns.push(`Inherited AGENTS.md: ${p}`);
    if (path.dirname(p) === p) break;
  }
  for (const plugin of plugins.data) {
    if (plugin.source.type !== 'builtin') concerns.push(`External plugin: ${plugin.id ?? 'unnamed'}`);
    if (plugin.state.status === 'failed') concerns.push(`Failed plugin: ${plugin.id ?? 'unnamed'}`);
  }
  const background = skills.data.filter(s => s.id !== r.skill_id).map(s => ({ id: s.id, hash: hash(s.content), autoinvoke: s.autoinvoke })).sort((a, b) => a.id.localeCompare(b.id));
  const configuration_fingerprint = hash(JSON.stringify(config.filter(e => e.type === 'document' && e.path !== path.join(r.directory, '.opencode', 'opencode.json'))));
  if (r.controlled && concerns.length) throw Error(`Isolation preflight failed: ${concerns.join('; ')}`);
  return { controlled: r.controlled && !concerns.length, concerns, background, configuration_fingerprint, candidate: candidate ? { id: candidate.id, path: candidate.path, description: candidate.description, autoinvoke: candidate.autoinvoke } : null,
    plugins: plugins.data.map(p => p.id).sort(), configuration_sources: config.map(e => ({ type: e.type, path: e.path })) };
}

async function cancel(client: Client, sessionID: string) {
  const actions: unknown[] = [];
  // The default profile denies delegation; opt-in profiles may create owned workers.
  const visit = async (id: string) => {
    let cursor: string | undefined;
    do {
      const children = await client.session.list({ parentID: id, ...(cursor ? { cursor } : {}) });
      for (const child of children.data) await visit(child.id);
      cursor = children.cursor.next ?? undefined;
    } while (cursor);
    actions.push(await client.session.interrupt({ sessionID: id, resume: false }, { signal: AbortSignal.timeout(10000) }));
  };
  await visit(sessionID);
  const jobs = await client.experimental.job.list({ sessionID, descendants: true });
  for (const job of jobs) {
    if (job.status !== 'running') continue;
    if (job.kind === 'shell') {
      const owner = await client.session.get({ sessionID: job.sessionID });
      await client.shell.remove({ id: job.shellID, location: { directory: owner.location.directory } });
    }
    if (job.kind === 'subagent') await client.session.interrupt({ sessionID: job.childSessionID, resume: false });
  }
  // Stop requests settle asynchronously; confirm durable job status before reporting.
  const deadline = Date.now() + 5000;
  let remaining = await client.experimental.job.list({ sessionID, descendants: true, status: 'running' });
  while (remaining.length && Date.now() < deadline) {
    await delay(100);
    remaining = await client.experimental.job.list({ sessionID, descendants: true, status: 'running' });
  }
  return { actions, remaining_jobs: remaining.map(j => j.id), complete: remaining.length === 0 };
}

async function run(r: Request) {
  if (r.schema_version !== 1 || !path.isAbsolute(r.directory) || !path.isAbsolute(r.output) || !r.model?.id || !r.agent || !(r.timeout_seconds > 0)) throw Error('Invalid runner request');
  await mkdir(r.output, { recursive: true });
  const out = (name: string) => path.join(r.output, name);
  await writeFile(out('events.jsonl'), '');
  const started = Date.now();
  let sessionID: string | undefined;
  let promptID: string | undefined;
  let client: Client | undefined;
  let status = 'preflight_error';
  let error: unknown = null;
  let cancellation: unknown = null;
  let environment: unknown = null;
  let all: SessionMessageInfo[] = [];
  const childUsage: Record<string, Events['executor']> = {};
  let stopped = false;
  const stop = () => { stopped = true; };
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  const events = new Events();
  try {
    const connection = await connect();
    client = connection.client;
    const info = await client.server.info();
    if (typeof info.version !== 'string' || !info.version.trim()) throw Error('OpenCode did not report its server version');
    const response = await fetch(new URL('/openapi.json', connection.url), { headers: connection.headers, signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw Error(`OpenAPI HTTP ${response.status}`);
    const spec = await response.json() as { paths: Record<string, unknown> };
    for (const endpoint of ['/api/session', '/api/skill', '/api/session/{sessionID}/prompt', '/api/experimental/session/{sessionID}/log', '/api/experimental/job']) {
      if (!spec.paths[endpoint]) throw Error(`Missing endpoint: ${endpoint}`);
    }
    environment = { ...await audit(client, r), server_version: info.version };
    await json(out('environment.json'), { version: info.version, ...environment as object });
    if (r.operation === 'probe') {
      status = 'completed';
    } else {
      const session = await client.session.create({ location: { directory: r.directory }, model: r.model, agent: r.agent,
        title: `skill-eval ${r.run_id}`, metadata: { skillcraft: true, run_id: r.run_id }, permissions: r.permissions });
      sessionID = session.id;
      await json(out('session.json'), { session_id: sessionID });
      await client.session.update({ sessionID, archived: true });
      const admitted = await client.session.prompt({ sessionID, text: r.prompt, ...(r.attach_skill ? { skills: [{ id: r.skill_id! }] } : {}) });
      promptID = admitted.id;
      await json(out('session.json'), { session_id: sessionID, prompt_id: promptID });
      status = 'running';
      const deadline = started + r.timeout_seconds * 1000;
      let failures = 0;
      while (true) {
        if (stopped || Date.now() >= deadline) { status = stopped ? 'cancelled' : 'timeout'; break; }
        try {
          // Bounded durable reads reconnect using the exclusive cursor; never rely on live-only subscriptions.
          for await (const event of client.experimental.session.log({ sessionID, after: events.after, follow: false }, { signal: AbortSignal.timeout(Math.max(1, Math.min(15000, deadline - Date.now()))) })) {
            const previousSeq = events.after;
            const accepted = events.accept(event);
            if (accepted || (event.type === 'log.synced' && events.after > previousSeq)) await appendFile(out('events.jsonl'), JSON.stringify(event) + '\n');
          }
          failures = 0;
        } catch (e) {
          if (++failures >= 3) throw e;
          await delay(250);
          continue;
        }
        const children = r.allow_delegation ? await descendants(client, sessionID) : [];
        const ownedIDs = [sessionID, ...children.map(c => c.id)];
        const pending = (await Promise.all(ownedIDs.map(id => client!.permission.list({ sessionID: id })))).flat();
        if (pending.length) {
          for (const p of pending) await client.permission.reply({ sessionID: p.sessionID, requestID: p.id, decision: 'reject', message: 'Non-interactive eval: permission blocked' });
          status = 'permission_blocked'; break;
        }
        const forms = (await Promise.all(ownedIDs.map(id => client!.session.form.list({ sessionID: id })))).flat();
        const waiting = forms;
        if (waiting.length) {
          for (const f of waiting) await client.session.form.cancel({ sessionID: f.sessionID, formID: f.id });
          status = 'question_blocked'; break;
        }
        all = await messages(client, sessionID);
        events.reconcile(all, sessionID);
        if (events.outcome) {
          const inbox = await client.session.inbox.list({ sessionID });
          const jobs = await client.experimental.job.list({ sessionID, descendants: true, status: 'running' });
          if (inbox.length === 0 && jobs.length === 0 && !children.some(c => isBusy(c.time))) {
            all = await messages(client, sessionID);
            if (!all.some(m => m.type === 'user' && m.id === promptID)) throw Error('Prompt not found in completed session');
            status = events.policyBlocked ? 'permission_blocked' : events.outcome;
            error = events.error;
            break;
          }
        }
        await delay(300);
      }
      if (status !== 'completed') cancellation = await cancel(client, sessionID);
      all = await messages(client, sessionID);
      events.reconcile(all, sessionID);
      if (r.attach_skill && status === 'completed' && !all.some(m => (m.type === 'skill' && m.skill === r.skill_id) || (m.type === 'user' && m.skills?.some(s => s.id === r.skill_id)))) {
        status = 'attachment_error'; error = 'Native skill attachment not confirmed';
      }
      const instructions = await client.experimental.session.instructions.entry.list({ sessionID });
      await json(out('instructions.json'), instructions);
    }
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
    if (status === 'running' || status === 'completed') status = 'transport_error';
    if (client && sessionID) {
      try { cancellation = await cancel(client, sessionID); } catch (e) { cancellation = { complete: false, error: String(e) }; }
      try { all = await messages(client, sessionID); } catch { /* Keep already captured transcript. */ }
    }
  } finally {
    process.removeListener('SIGTERM', stop); process.removeListener('SIGINT', stop);
  }
  if (r.allow_delegation && client && sessionID) {
    try {
      for (const child of await descendants(client, sessionID)) {
        const projected = await messages(client, child.id);
        const childEvents = new Events(); childEvents.reconcile(projected, child.id);
        childUsage[child.id] = childEvents.executor;
        await mkdir(out('children'), { recursive: true });
        await json(out(`children/${child.id}.json`), { session: child, messages: projected, usage: childEvents.executor });
      }
    } catch (e) { status = 'transport_error'; error = `Child evidence collection failed: ${String(e)}`; }
  }
  const family = [events.executor, ...Object.values(childUsage)];
  const executor = family.every(u => u !== null) ? family.reduce((sum, u) => {
    if (!sum || !u) return sum;
    sum.tokens.input += u.tokens.input; sum.tokens.output += u.tokens.output; sum.tokens.reasoning += u.tokens.reasoning;
    sum.tokens.cache.read += u.tokens.cache.read; sum.tokens.cache.write += u.tokens.cache.write;
    sum.total_tokens += u.total_tokens; sum.records += u.records;
    sum.cost = sum.cost === null || u.cost === null ? null : sum.cost + u.cost;
    return sum;
  }, { tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, total_tokens: 0, records: 0, cost: 0 } as Events['executor']) : null;
  const lastAssistant = all.filter(m => m.type === 'assistant').at(-1);
  const texts = lastAssistant?.content.filter(p => p.type === 'text').map(p => p.text) ?? [];
  await json(out('messages.json'), all);
  await writeFile(out('response.txt'), texts.join('\n\n'));
  await writeFile(out('transcript.md'), `## Eval Prompt\n\n${r.prompt}\n\n## Messages\n\n` + all.map(m => `### ${m.type}\n\n\`\`\`json\n${JSON.stringify(m, null, 2)}\n\`\`\``).join('\n\n'));
  await json(out('usage.json'), { schema_version: 1, executor, root_executor: events.executor, child_executors: childUsage,
    maintenance: events.maintenance, role: r.mode,
    projection_fallback: events.projectionFallback, maintenance_complete: !events.projectionFallback });
  await json(out('timing.json'), { total_duration_seconds: (Date.now() - started) / 1000 });
  const result = { schema_version: 1, run_id: r.run_id, status, session_id: sessionID ?? null, prompt_id: promptID ?? null,
    triggered: events.triggered(r.skill_id, status === 'completed'), attached: r.attach_skill === true && status === 'completed',
    error, cancellation, last_seq: events.after, model: r.model, agent: r.agent, mode: r.mode, environment };
  await json(out('run.json'), result);
  console.log(JSON.stringify(result));
  process.exitCode = status === 'completed' ? 0 : 1;
}

try {
  const file = process.argv[2];
  if (!file) throw Error('Usage: tsx src/main.ts /absolute/request.json');
  await run(JSON.parse(await readFile(file, 'utf8')));
} catch (e) {
  console.error(e instanceof Error ? e.message : String(e));
  process.exitCode = 2;
}
