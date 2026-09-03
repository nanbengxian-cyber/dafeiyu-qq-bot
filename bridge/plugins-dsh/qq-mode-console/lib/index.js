// QQ 桥接模式控制台（host 插件，仅注册 settings 命名空间）。
//
// 通过 DSH 官方用户设置扩展点（ctx.settings.register）暴露一个
// `qq-mode` 命名空间。WebUI 的设置页会自动渲染该命名空间的配置卡片，
// 用户在那里切换桥接模式（chat / closed-agent / 仿真模式，内部标识 reserved），
// 桥接进程通过 DSH settings API 轮询读取。本插件不修改任何 WebUI 内核。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import z from '@deepseek-ai/schemastery';
import { registerStatusPage, STATUS_PAGE_PATH } from './status-page.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// 插件通过符号链接挂在 ~/.dsh/plugins/ 下，但 import.meta.url 解析出的是真实路径
// （…/qq-bridge/plugins/qq-mode-console/lib），所以向上三级即 qq-bridge 仓库根。
const BRIDGE_ROOT = path.resolve(__dirname, '..', '..', '..');
const DIAG = path.join(BRIDGE_ROOT, 'state', 'qq-mode-plugin.log');

function diag(msg) {
  try {
    fs.mkdirSync(path.dirname(DIAG), { recursive: true });
    fs.appendFileSync(DIAG, `[${new Date().toISOString()}] ${msg}\n`);
  } catch {}
}

export const name = 'qq-mode-console';
// 注意：cordis 的 inject 对象形式是「服务名 → intercept 配置」的映射，
// 没有 required/optional 语义 —— 写成 {required:[…],optional:[…]} 会被当成
// 两个名叫 required / optional 的服务去等待，插件永远不会启动。必须用数组。
// webServer 在本 Web 组合中始终存在（市场、壁纸插件都依赖它）；
// status-page 内部仍做了服务缺失兜底。
export const inject = ['settings', 'webServer'];

export const QQMODE_NAMESPACE = 'qq-mode';

export const QqModeSchema = z.object({
  mode: z.union([z.const('chat'), z.const('closed-agent'), z.const('reserved'), z.const('reserved2')]).default('chat'),
  ownerQQ: z.string().description('管理员 QQ（ownerQQ）；留空表示不通过 DSH 设置覆盖 config.json'),
});

export function apply(ctx, config = {}) {
  diag(`apply called, settings=${typeof ctx.settings} inject=${JSON.stringify(ctx._inject)}`);

  // 在线状态页：独立注册，不受 settings 注册结果影响。
  // 放在最前面是因为它是本次的主要用途 —— 即使 settings 命名空间重复注册被跳过，
  // 状态页也必须可用。
  try {
    registerStatusPage(ctx, BRIDGE_ROOT);
    diag(`status page registered at ${STATUS_PAGE_PATH} (bridgeRoot=${BRIDGE_ROOT})`);
    console.log(`[qq-mode-console] 在线状态页：http://127.0.0.1:3080${STATUS_PAGE_PATH}`);
  } catch (error) {
    // 状态页注册失败不应拖垮插件（例如路由重复），只记录
    diag(`status page register failed: ${error?.stack ?? error}`);
    ctx.logger?.warn?.('qq-mode-console: 在线状态页注册失败：%s', String(error?.message ?? error));
  }

  try {
    const settings = ctx.settings;
    if (!settings || typeof settings.register !== 'function') {
      diag('settings service unavailable');
      return;
    }
    const scope = settings.register(QQMODE_NAMESPACE, QqModeSchema, {
      base: { mode: 'reserved2' },
      applies: 'live'
    });
    diag(`registered ${QQMODE_NAMESPACE} scope=${typeof scope}`);
    console.log(`[qq-mode-console] active (namespace=${QQMODE_NAMESPACE})`);
  } catch (error) {
    if (/already registered/i.test(String(error?.message ?? error))) {
      diag(`qq-mode namespace already registered, skip`);
      return;
    }
    diag(`register threw: ${error?.stack ?? error}`);
    throw error;
  }
}
