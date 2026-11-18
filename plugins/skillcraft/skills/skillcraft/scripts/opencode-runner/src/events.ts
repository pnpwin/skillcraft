import type { SessionLogItem, TokenUsageInfo, SessionMessageInfo } from '@opencode/client';
export type { SessionLogItem, SessionMessageInfo } from '@opencode/client';

type Usage = { tokens: TokenUsageInfo; total_tokens: number; cost: number | null; records: number };
export function isBusy(time: { busy?: number; idle?: number }): boolean {
  return time.busy !== undefined && (time.idle === undefined || time.busy > time.idle);
}
function emptyUsage(): Usage {
  return { tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, total_tokens: 0, cost: 0, records: 0 };
}

/** Reduce only durable records. Session totals are deliberately never added. */
export class Events {
  after = 0;
  seen = new Set<string>();
  calls = new Map<string, { name?: string; input?: Record<string, unknown>; status?: string }>();
  outcome: string | null = null;
  error: unknown = null;
  policyBlocked = false;
  executor: Usage | null = null;
  maintenance: Usage | null = null;
  stepIDs = new Set<string>();
  projectionFallback = false;
  accept(event: SessionLogItem): boolean {
    if (event.type === 'log.synced') {
      this.after = Math.max(this.after, event.seq ?? 0);
      return false;
    }
    if (this.seen.has(event.id) || event.durable.seq <= this.after) return false;
    this.seen.add(event.id);
    this.after = event.durable.seq;
    const d = event.data;
    if (event.type.startsWith('session.tool.') && 'assistantMessageID' in d && 'id' in d) {
      const key = `${d.sessionID}/${d.assistantMessageID}/${d.id}`;
      const call = this.calls.get(key) ?? {};
      if (event.type === 'session.tool.input.started') call.name = event.data.name;
      if (event.type === 'session.tool.called') call.input = event.data.input;
      if (event.type === 'session.tool.success') call.status = 'success';
      if (event.type === 'session.tool.failed') {
        call.status = 'failed';
        if (/permission|denied|rejected/i.test(JSON.stringify(event.data.error))) this.policyBlocked = true;
      }
      this.calls.set(key, call);
    }
    if (event.type === 'session.execution.started') this.outcome = null;
    if (event.type === 'session.execution.succeeded') this.outcome = 'completed';
    if (event.type === 'session.execution.interrupted') this.outcome = 'interrupted';
    if (event.type === 'session.execution.failed') {
      this.outcome = 'execution_error';
      this.error = event.data.error;
    }
    if (event.type === 'session.step.ended' || event.type === 'session.usage.recorded') {
      if (event.type === 'session.step.ended') {
        if (this.stepIDs.has(event.data.assistantMessageID)) return true;
        this.stepIDs.add(event.data.assistantMessageID);
      }
      const field = event.type === 'session.step.ended' ? 'executor' : 'maintenance';
      const u = this[field] ??= emptyUsage();
      const { tokens: t, cost } = event.data;
      u.tokens.input += t.input;
      u.tokens.output += t.output;
      u.tokens.reasoning += t.reasoning;
      u.tokens.cache.read += t.cache.read;
      u.tokens.cache.write += t.cache.write;
      u.total_tokens += t.input + t.output + t.reasoning + t.cache.read + t.cache.write;
      if (u.cost !== null) u.cost += cost;
      u.records++;
    }
    return true;
  }
  /** V2.2.6 can return only log.synced on replay. Reconcile durable projections. */
  reconcile(messages: SessionMessageInfo[], sessionID: string) {
    for (const m of messages) {
      if (m.type === 'idle') {
        this.outcome = m.outcome === 'succeeded' ? 'completed' : m.outcome === 'failed' ? 'execution_error' : 'interrupted';
        this.error = m.error ?? null;
      }
      if (m.type !== 'assistant') continue;
      for (const p of m.content) {
        if (p.type !== 'tool') continue;
        const state = p.state;
        const call = { name: p.name, input: 'input' in state && typeof state.input === 'object' ? state.input : undefined,
          status: state.status === 'completed' ? 'success' : state.status === 'error' ? 'failed' : state.status };
        this.calls.set(`${sessionID}/${m.id}/${p.id}`, call);
        if (state.status === 'error' && /permission|denied|rejected/i.test(JSON.stringify(state))) this.policyBlocked = true;
      }
      if (m.time.completed && m.tokens && !this.stepIDs.has(m.id)) {
        this.stepIDs.add(m.id);
        this.projectionFallback = true;
        const u = this.executor ??= emptyUsage();
        const t = m.tokens;
        u.tokens.input += t.input; u.tokens.output += t.output; u.tokens.reasoning += t.reasoning;
        u.tokens.cache.read += t.cache.read; u.tokens.cache.write += t.cache.write;
        u.total_tokens += t.input + t.output + t.reasoning + t.cache.read + t.cache.write;
        u.cost = u.cost === null || m.cost === undefined ? null : u.cost + m.cost;
        u.records++;
      }
    }
  }
  triggered(id: string | undefined, complete: boolean): boolean | null {
    if (!id) return null;
    if ([...this.calls.values()].some(c => c.name === 'skill' && c.input?.id === id && c.status === 'success')) return true;
    return complete ? false : null;
  }
}
