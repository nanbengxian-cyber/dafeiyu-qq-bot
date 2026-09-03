// 视觉/图片链路自检脚本：
// 1. 校验配置默认模型为 DeepSeek-V4-Flash-Vision-Exp
// 2. 校验 safe-fetch 的 SSRF 防护（本机地址应被拒绝）
// 3. 若桥接运行中，校验 /api/images/message 端点可用（用二代 agent token）
// 运行：node scripts/test-vision-media.mjs
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { safeFetchBuffer } from '../src/safe-fetch.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

function readJson(file) {
  let text = fs.readFileSync(file, 'utf8');
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  return JSON.parse(text);
}

const config = readJson(path.join(ROOT, 'config.json'));
let passed = 0;
let failed = 0;

function check(name, cond, extra = '') {
  if (cond) {
    passed += 1;
    console.log(`✅ ${name}${extra ? ' — ' + extra : ''}`);
  } else {
    failed += 1;
    console.error(`❌ ${name}${extra ? ' — ' + extra : ''}`);
  }
}

// 1. 配置 + 真实模型能力
// 上游原版把「视觉模型」写死成 deepseek-v4-flash-vision-exp，本机没有这个路由。
// 真正决定能不能看图的是 DSH 侧 llm-pi-ai 里该模型的 input 模态是否含 image：
// dsh-mcp-client 落 attachment 前会查 resolveModelInfo().inputModalities，
// 不含 image 就把图片投影成 "[image unavailable: ... does not declare image input]"。
const provider = String(config.dsh?.provider ?? '');
const model = String(config.dsh?.model ?? '');
check('config.dsh.provider / model 已配置', Boolean(provider && model), `${provider}/${model}`);

let visionOk = false;
let visionNote = '';
try {
  const baseUrl = String(config.dsh?.baseUrl || 'http://127.0.0.1:3080').replace(/\/+$/, '');
  const res = await fetch(`${baseUrl}/api/settings.describe`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ type: 'client-request', rpcId: 'vision-check', method: 'settings.describe', payload: { ns: 'llm-pi-ai' } }),
    signal: AbortSignal.timeout(15000)
  });
  const body = await res.json();
  const ns = body?.result?.value?.namespaces?.find((n) => n.ns === 'llm-pi-ai');
  const entry = ns?.value?.providers?.[provider]?.models?.find((m) => m.id === model);
  if (!entry) {
    visionNote = `llm-pi-ai 里没有 ${provider}/${model} 的模型条目（可能是 DSH 自带目录路由，需人工确认）`;
  } else {
    visionOk = Array.isArray(entry.input) && entry.input.includes('image');
    visionNote = `input=${JSON.stringify(entry.input ?? [])}`;
    if (!visionOk) visionNote += '；请在 ~/.dsh/settings.yaml 给该模型加 input: [text, image]';
  }
} catch (error) {
  visionNote = `无法查询 DSH settings（DSH 未运行？）：${error?.message ?? error}`;
}
check('当前模型在 DSH 里声明了 image 输入', visionOk, visionNote);

check('config.socialV2.tools.getImages 默认开启', config.socialV2?.tools?.getImages !== false);

// 2. safe-fetch SSRF
try {
  await safeFetchBuffer('http://127.0.0.1/');
  check('safe-fetch 拒绝本机地址', false, '未抛出异常');
} catch (error) {
  check('safe-fetch 拒绝本机地址', /内网|本机|禁止/.test(String(error?.message ?? error)), error?.message);
}

// 3. 桥接端点（可选）
const consoleTokenFile = path.join(ROOT, 'state', 'console-token');
const socialV2File = path.join(ROOT, 'state', 'social-v2.json');
if (fs.existsSync(consoleTokenFile) && fs.existsSync(socialV2File)) {
  try {
    const consoleToken = fs.readFileSync(consoleTokenFile, 'utf8').trim();
    const social = JSON.parse(fs.readFileSync(socialV2File, 'utf8'));
    const conv = social?.conversations ? Object.entries(social.conversations)[0] : null;
    if (conv) {
      const [key, st] = conv;
      const agentToken = st?.agentToken;
      const messageId = st?.recentMessages?.find((m) => m && !m.isSelf)?.messageId || st?.recentMessages?.[0]?.messageId;
      if (agentToken && messageId) {
        const url = `http://127.0.0.1:${config.consolePort || 3100}/api/images/message?key=${encodeURIComponent(key)}&messageId=${encodeURIComponent(String(messageId))}`;
        const res = await fetch(url, { headers: { 'x-agent-token': agentToken, 'x-console-token': consoleToken }, signal: AbortSignal.timeout(10000) });
        const body = await res.json().catch(() => ({}));
        check('桥接 /api/images/message 端点可访问', res.ok === true && body.ok === true, `HTTP ${res.status}`);
      } else {
        console.log('⚠️ 跳过桥接端点检查：没有可用 agentToken/messageId');
      }
    }
  } catch (error) {
    console.log(`⚠️ 桥接端点检查跳过：${error?.message ?? error}`);
  }
} else {
  console.log('⚠️ 跳过桥接端点检查：缺少 state/console-token 或 state/social-v2.json');
}

console.log(`\n结果：${passed} 通过，${failed} 失败`);
process.exit(failed > 0 ? 1 : 0);
