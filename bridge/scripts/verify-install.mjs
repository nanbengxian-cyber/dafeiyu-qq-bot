// 插件启用验证脚本（一次性，可重复运行）：
//   1. host.describe 探活
//   2. settings.describe 断言 qq-mode 命名空间存在且 schema 含四个模式
//   3. agentPreset.list 断言 qq-chat / qq-chat-v2 已注册且 trust=user
//   4. 用 qq-chat-v2 preset 建一个临时会话，让 agent 列出自己看到的 QQ MCP 工具
//      → 证明三个 MCP server 真的连上了、工具进了 agent 的工具面
//      → 同时确认 dev_* 等开发工具被 qq-tool-restrict 隐藏
// 用法：node scripts/verify-install.mjs [baseUrl]
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { NodeApiClient, unwrap, createTurnCollector } from '../src/dsh-client.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

const ok = (m) => console.log(`✅ ${m}`);
const bad = (m) => { console.error(`❌ ${m}`); process.exitCode = 1; };

async function main() {
  const baseUrl = process.argv[2] ?? 'http://127.0.0.1:3080';
  const api = new NodeApiClient(baseUrl);

  const desc = unwrap(await api.host.describe({}), 'host.describe');
  ok(`DSH 可达：provider=${desc.provider} model=${desc.model}`);

  // 1) qq-mode 设置命名空间
  const settings = unwrap(await api.settings.describe({}), 'settings.describe');
  const qq = settings.namespaces.find((n) => n.ns === 'qq-mode');
  if (!qq) bad('设置里没有 qq-mode 命名空间 —— qq-mode-console 插件未加载');
  else {
    const modes = Object.values(qq.schema?.refs ?? {}).filter((r) => r.type === 'const').map((r) => r.value);
    const want = ['chat', 'closed-agent', 'reserved', 'reserved2'];
    const missing = want.filter((m) => !modes.includes(m));
    if (missing.length) bad(`qq-mode schema 缺模式：${missing.join(', ')}`);
    else ok(`qq-mode 命名空间已注册：当前 mode=${qq.value?.mode}，applies=${qq.applies}，四个模式齐全`);
  }

  // 2) 两个 preset
  const { presets } = unwrap(await api.agentPresets.list({}), 'agentPreset.list');
  for (const id of ['qq-chat', 'qq-chat-v2']) {
    const p = presets.find((x) => x.id === id);
    if (!p) bad(`preset 缺失：${id}`);
    else if (p.trust !== 'user') bad(`preset ${id} trust=${p.trust}（应为 user）`);
    else ok(`preset 已注册：${id}（${p.name}）`);
  }

  // 3) 让一个 qq-chat-v2 会话自己报告工具面
  const cwd = path.join(ROOT, 'state', 'verify-install');
  fs.mkdirSync(cwd, { recursive: true });
  const created = unwrap(await api.sessions.create({ cwd, agentPreset: 'qq-chat-v2' }), 'session.create');
  const sessionId = created.sessionId;
  ok(`临时会话已创建：${sessionId}（preset=${created.agentPreset ?? 'qq-chat-v2'}）`);

  const collector = createTurnCollector();
  let resolveOpened;
  const opened = new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('等待 WebSocket open 超时（10s）')), 10_000);
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
        if (frame.type === 'stream/error') {
          clearTimeout(timer);
          reject(new Error('事件流错误: ' + JSON.stringify(frame.error)));
          return;
        }
      }
    })().catch((e) => { clearTimeout(timer); reject(e); });
  });
  await opened;

  const prompt = [
    '这是一次安装自检。除了下面明确要求的一步，不要调用任何其他工具，绝对不要发送任何 QQ 消息。',
    '第一步：如果你的工具列表里有 mcp__router__search_and_activate（MCP 懒加载路由），',
    '就用 query 为 "QQ 发送消息 获取未读消息 snowluma" 调用它一次，把 QQ 工具披露出来；',
    '如果没有这个路由工具，跳过这一步。',
    '第二步：只用纯文本回答两行：',
    '第一行：TOOLS=<你现在的工具列表里所有以 mcp__ 开头的工具名，用逗号分隔；没有就写 none>',
    '第二行：DEV=<你的工具列表里所有以 dev_ 开头的工具名，用逗号分隔；没有就写 none>',
  ].join('\n');
  unwrap(await api.sessions.prompt({ sessionId, mode: 'queue', content: [{ type: 'text', text: prompt }] }), 'session.prompt');

  const ended = await done;
  const text = ended.text || '';
  console.log('\n—— agent 自报工具面 ——');
  console.log(text.trim());
  console.log('———————————————\n');

  const names = [...text.matchAll(/mcp__[A-Za-z0-9_-]+/g)].map((m) => m[0]);
  const lazy = names.some((n) => n.startsWith('mcp__router__'));
  if (lazy) ok('MCP 懒加载路由已放行（mcp__router__search_and_activate）—— QQ 工具按需披露');
  for (const prefix of ['snowluma__', 'snowluma-host__', 'web-search-safe__']) {
    const label = prefix.slice(0, -2);
    if (names.some((n) => n.startsWith(`mcp__${prefix}`))) ok(`MCP server 工具已进入 agent 工具面：${label}`);
    else if (lazy) console.log(`ℹ️  ${label}：本轮未披露（懒加载下属正常，一次路由只命中一个 server）`);
    else bad(`MCP server 工具缺失：${label}（检查 cordis.patch.yml 路径与 DSH 是否重启过）`);
  }
  if (/DEV=\s*none/i.test(text)) ok('开发工具（dev_*）已被 qq-tool-restrict 隐藏');
  else if (/dev_/.test(text)) bad('agent 仍能看到 dev_* 开发工具 —— qq-tool-restrict 未生效');
  else console.log('ℹ️  未能从回复中判定 dev_* 是否隐藏（回复格式与预期不符）');

  console.log(process.exitCode ? '\n⚠️ 存在未通过项，见上面 ❌' : '\n🎉 插件与 MCP 全部正常启用');
  process.exit(process.exitCode ?? 0);
}

main().catch((error) => {
  console.error('❌ 验证脚本失败:', error?.message ?? error);
  process.exit(1);
});
