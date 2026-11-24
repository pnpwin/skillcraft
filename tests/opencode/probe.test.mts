import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const runner = fileURLToPath(new URL(
  '../../plugins/skillcraft/skills/skillcraft/scripts/opencode-runner/', import.meta.url,
));
const execute = promisify(execFile);

test('compatibility and publication probes keep their distinct evidence without a session', async t => {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), 'opencode-probe-')));
  t.after(() => rm(root, { recursive: true, force: true }));
  const skill = path.join(root, '.agents/skills/example/SKILL.md');
  const body = 'Synthetic skill contents';
  await mkdir(path.dirname(skill), { recursive: true });
  await writeFile(skill, body);

  const requests: string[] = [];
  const openapi = { paths: Object.fromEntries([
    '/api/session', '/api/skill', '/api/session/{sessionID}/prompt',
    '/api/experimental/session/{sessionID}/log', '/api/experimental/job',
  ].map(endpoint => [endpoint, {}])) };
  const responses: Record<string, unknown> = {
    '/api/info': { version: '2.3.0' },
    '/openapi.json': openapi,
    '/api/skill': { data: [
      { id: 'example', path: skill, description: 'Example', content: body, autoinvoke: true },
      { id: 'opencode', path: '/builtin/opencode/SKILL.md', content: 'Built-in', autoinvoke: true },
    ] },
    '/api/plugin': { data: [{ id: 'builtin', source: { type: 'builtin' }, state: { status: 'active' } }] },
    '/api/config': [{ type: 'document', path: path.join(root, 'global.json'), info: { model: 'test/model' } }],
  };
  const server = createServer((req, res) => {
    const endpoint = new URL(req.url!, 'http://localhost').pathname;
    requests.push(endpoint);
    const value = responses[endpoint];
    res.writeHead(req.method === 'GET' && value ? 200 : 404, { 'content-type': 'application/json' });
    res.end(JSON.stringify(value ?? {}));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const address = server.address();
  assert.ok(address && typeof address === 'object');

  async function probe(operation: string, controlled: boolean) {
    const output = path.join(root, operation);
    const request = output + '.json';
    await writeFile(request, JSON.stringify({
      schema_version: 1, operation, directory: root, output, prompt: '',
      model: { providerID: 'test', id: 'model' }, agent: 'test', timeout_seconds: 15,
      mode: 'quality', skill_id: 'example', skill_path: skill,
      skill_sha256: createHash('sha256').update(body).digest('hex'),
      controlled, permissions: [], run_id: operation,
    }));
    await execute(process.execPath, [path.join(runner, 'node_modules/tsx/dist/cli.mjs'),
      path.join(runner, 'src/main.ts'), request], {
      env: { ...process.env, SKILLCRAFT_SERVER: `http://127.0.0.1:${address.port}`, SKILLCRAFT_HEADERS: '{}' },
      timeout: 30000,
    });
    return JSON.parse(await readFile(path.join(output, 'environment.json'), 'utf8'));
  }

  const compatibility = await probe('probe', true);
  assert.equal(compatibility.controlled, true);
  assert.deepEqual(compatibility.background.map((skill: { id: string }) => skill.id), ['opencode']);
  assert.match(compatibility.configuration_fingerprint, /^[0-9a-f]{64}$/);
  assert.deepEqual(new Set(requests), new Set(['/api/info', '/openapi.json', '/api/skill', '/api/plugin', '/api/config']));

  requests.length = 0;
  const publication = await probe('catalog_probe', false);
  assert.equal(publication.catalog, 'skill.list');
  assert.equal(publication.candidate.realpath, skill);
  assert.deepEqual(publication.plugins, ['builtin']);
  assert.deepEqual(new Set(requests), new Set(['/api/info', '/openapi.json', '/api/skill', '/api/plugin']));

  responses['/api/plugin'] = { data: [{ id: 'external', source: { type: 'local' }, state: { status: 'active' } }] };
  await assert.rejects(probe('catalog_probe', false));
  const blocked = JSON.parse(await readFile(path.join(root, 'catalog_probe', 'run.json'), 'utf8'));
  assert.equal(blocked.status, 'preflight_error');
  assert.match(blocked.error, /Unexpected non-builtin plugin/);
  assert.equal(blocked.session_id, null);
});
