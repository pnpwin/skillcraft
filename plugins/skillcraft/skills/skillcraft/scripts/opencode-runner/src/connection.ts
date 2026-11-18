import { OpenCode } from '@opencode/client';
import { Service } from '@opencode/client/service';

export async function discoverEndpoint() {
  const endpoint = await Service.discover();
  if (!endpoint) throw Error('No running OpenCode service. Start OpenCode or configure SKILLCRAFT_SERVER.');
  return { url: endpoint.url, headers: Service.headers(endpoint) };
}

export async function connect() {
  const boundedFetch: typeof fetch = (input, init) => fetch(input, { ...init,
    signal: AbortSignal.any([...(init?.signal ? [init.signal] : []), AbortSignal.timeout(15000)]) });
  const explicit = process.env.SKILLCRAFT_SERVER;
  if (explicit) {
    // Headers are an environment-only input, never serialized into run artifacts.
    const headers = JSON.parse(process.env.SKILLCRAFT_HEADERS ?? '{}');
    if (typeof headers !== 'object' || headers === null || Array.isArray(headers)) throw Error('Invalid SKILLCRAFT_HEADERS');
    return { client: OpenCode.make({ baseUrl: explicit, headers, fetch: boundedFetch }), url: explicit, headers };
  }
  const { url, headers } = await discoverEndpoint();
  return { client: OpenCode.make({ baseUrl: url, headers, fetch: boundedFetch }), url, headers };
}
