// 让一个指定 preset 的会话把自己完整工具面报出来（调试用）。
// 用法：node scripts/dump-tools.mjs [preset] [baseUrl]
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { NodeApiClient, unwrap, createTurnCollector } from '../src/dsh-client.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

async function main() {
  const preset = process.argv[2] ?? 'qq-chat-v2';
  const baseUrl = process.argv[3] ?? 'http://127.0.0.1:3080';
  const api = new NodeApiClient(baseUrl);
  unwrap(await api.host.describe({}), 'host.describe');

  const cwd = path.join(ROOT, 'state', 'verify-install');
  fs.mkdirSync(cwd, { recursive: true });
  const created = unwrap(await api.sessions.create({ cwd, agentPreset: preset }), 'session.create');
  const sessionId = created.sessionId;
  console.log(`会话 ${sessionId}（preset=${created.agentPreset ?? preset}）`);

  const collector = createTurnCollector();
  let resolveOpened;
  const opened = new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('WebSocket open 超时')), 10_000);
    resolveOpened = () => { clearTimeout(t); resolve(); };
  });
  const done = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('等待回复超时（120s）')), 120_000);
    (async () => {
      for await (const env of api.events.mux({}, undefined, () => resolveOpened())) {
        const frame = env.payload;
        if (frame.type === 'session/event' && frame.sessionId === sessionId) {
          const ended = collector.push(frame.event);
          if (ended) { clearTimeout(timer); resolve(ended); return; }
        }
        if (frame.type === 'stream/error') { clearTimeout(timer); reject(new Error(JSON.stringify(frame.error))); return; }
      }
    })().catch((e) => { clearTimeout(timer); reject(e); });
  });
  await opened;

  const prompt = '不要调用任何工具。把你当前可用的每一个工具名逐行列出来（一行一个，只写名字，不要解释、不要分组、不要省略）。';
  unwrap(await api.sessions.prompt({ sessionId, mode: 'queue', content: [{ type: 'text', text: prompt }] }), 'session.prompt');
  const ended = await done;
  console.log('---- 工具面 ----');
  console.log((ended.text || '（无文本）').trim());
  process.exit(0);
}

main().catch((e) => { console.error('失败:', e?.message ?? e); process.exit(1); });
