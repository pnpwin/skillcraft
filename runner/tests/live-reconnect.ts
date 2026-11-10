/** Live transport fault injection: explicit auth context and one interrupted log read. */
import { Service } from '@opencode/client/service';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const output = process.argv[2];
const model = process.argv[3];
if (!output || !model) throw Error('Usage: tsx tests/live-reconnect.ts OUTPUT MODEL');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const endpoint = await Service.discover();
if (!endpoint) throw Error('No running service');
const upstreamHeaders = Service.headers(endpoint);
let dropped = false;
let logReads = 0;
let unauthorized = 0;
let requests = 0;
const server = createServer(async (req, res) => {
  requests++;
  if (req.headers['x-eval-auth'] !== 'transport-test') {
    unauthorized++; res.writeHead(401); res.end(); return;
  }
  if (req.url?.includes('/log')) {
    logReads++;
    if (!dropped) { dropped = true; req.socket.destroy(); return; }
  }
  try {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    const body = Buffer.concat(chunks).toString('utf8');
    const response = await fetch(new URL(req.url!, endpoint.url), {
      method: req.method,
      headers: { ...upstreamHeaders, 'content-type': 'application/json' },
      ...(body ? { body } : {}), signal: AbortSignal.timeout(20000),
    });
    res.writeHead(response.status, { 'content-type': response.headers.get('content-type') ?? 'application/json' });
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch (e) { res.writeHead(502); res.end(String(e)); }
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const address = server.address();
if (!address || typeof address === 'string') throw Error('No proxy address');
try {
  const code = `import json; from scripts.runtime import run_model; r=run_model('Reply with exactly OK.',${JSON.stringify(path.resolve(output))},${JSON.stringify(model)},timeout=60); print(json.dumps(r))`;
  const proc = spawn(path.join(root, '.venv/bin/python'), ['-c', code], {
    cwd: root, env: { ...process.env, SKILLCRAFT_SERVER: `http://127.0.0.1:${address.port}`,
      SKILLCRAFT_HEADERS: JSON.stringify({ 'x-eval-auth': 'transport-test' }) },
  });
  let stdout = '', stderr = '';
  proc.stdout.on('data', c => { stdout += c; });
  proc.stderr.on('data', c => { stderr += c; });
  const [exit] = await once(proc, 'close');
  assert.equal(exit, 0, stderr);
  const result = JSON.parse(stdout);
  assert.equal(result.status, 'completed', JSON.stringify(result));
  assert.equal(unauthorized, 0);
  assert.equal(dropped, true);
  assert.ok(logReads >= 2);
  const proof = { ok: true, requests, unauthorized, dropped_log_connections: 1,
    log_reads: logReads, status: result.status, session_id: result.session_id };
  await writeFile(path.join(output, 'reconnect-proof.json'), JSON.stringify(proof, null, 2) + '\n');
  console.log(JSON.stringify(proof, null, 2));
} finally {
  server.closeAllConnections();
  server.close();
}
