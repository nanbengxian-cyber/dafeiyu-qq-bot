/**
 * qq-mode-console — 在线状态页（GET /qq-bridge/status，JSON：/qq-bridge/status.json）。
 *
 * 为什么是独立页面而不是设置页里的开关：
 *   本移动版 Web 前端不渲染插件注册的通用 settings 命名空间（前端 bundle 里没有
 *   namespaces 渲染逻辑，settings.describe 返回了 qq-mode 也不会显示）。所以「在设置里
 *   看状态」在这个构建上做不到，改为在同一个 DSH Web 里提供一个专用页面 —— 与
 *   dsh-github 的令牌页同一套做法（webServer.register + 本机回环）。
 *
 * 为什么需要三层判定：
 *   ① 桥接进程活着   —— 进程被 Android 后台清理时不写任何日志，只能靠心跳时间戳判断；
 *   ② QQ 侧真的连上 —— 进程活着不代表 WebSocket 连着（PC 关机时会一直退避重连）；
 *   ③ DSH 能回话     —— 前两层都好，DSH 引擎挂了照样不说话。
 *   任一层断了「他就不说话」，所以三层都要单独显示，不能合成一个「在线」。
 *
 * 「是否真正联通」用主动探测回答：直接向 OneBot HTTP 端口发 get_login_info，
 * 拿到 QQ 号才算真通 —— 这是唯一能证明「手机 ↔ PC ↔ QQ」整条链路活着的方式。
 *
 * @module qq-mode-console/status-page
 */
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';

const PAGE_PATH = '/qq-bridge/status';
const JSON_PATH = '/qq-bridge/status.json';
const PROBE_TIMEOUT_MS = 6000;
/** 心跳超过这个时长即判定桥接进程已死（心跳周期 5s，给 4 倍余量）。 */
const HEARTBEAT_DEAD_MS = 20000;

function esc(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

/** 相对时间，例如「12 秒前」「3 分钟前」。 */
function ago(ts) {
  if (!ts || typeof ts !== 'number') return '从未';
  const sec = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (sec < 60) return `${sec} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分钟前`;
  return `${Math.floor(sec / 86400)} 天前`;
}

function fmtTime(ts) {
  if (!ts || typeof ts !== 'number') return '—';
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 进程是否存活（/proc/<pid> 是否存在；Android 上无需权限即可读）。 */
function pidAlive(pid) {
  if (!pid || typeof pid !== 'number') return false;
  try {
    return fs.existsSync(`/proc/${pid}`);
  } catch {
    return false;
  }
}

/**
 * 主动探测 OneBot HTTP：POST get_login_info。
 * 只有拿到 retcode=0 才算「真正联通」。
 */
function probeOneBot(httpUrl, accessToken) {
  return new Promise((resolve) => {
    let target;
    try {
      target = new URL('/get_login_info', httpUrl);
    } catch {
      resolve({ ok: false, error: 'httpUrl 配置无效' });
      return;
    }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') {
      resolve({ ok: false, error: 'httpUrl 协议必须是 http/https' });
      return;
    }
    const body = Buffer.from('{}', 'utf8');
    const started = Date.now();
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port || 80,
        path: target.pathname,
        method: 'POST',
        timeout: PROBE_TIMEOUT_MS,
        headers: {
          'content-type': 'application/json',
          'content-length': body.length,
          ...(accessToken ? { authorization: `Bearer ${accessToken}` } : {})
        }
      },
      (res) => {
        const chunks = [];
        let size = 0;
        res.on('data', (c) => {
          size += c.length;
          if (size > 64 * 1024) {
            res.destroy();
            return;
          }
          chunks.push(c);
        });
        res.on('end', () => {
          const ms = Date.now() - started;
          // 426 是把 httpUrl 误填成 WebSocket 端口的典型症状，单独提示
          if (res.statusCode === 426) {
            resolve({ ok: false, ms, error: 'HTTP 426：httpUrl 指向了 WebSocket 端口（应为 HTTP 端口）' });
            return;
          }
          if (res.statusCode === 401 || res.statusCode === 403) {
            resolve({ ok: false, ms, error: `HTTP ${res.statusCode}：accessToken 不匹配` });
            return;
          }
          let parsed;
          try {
            parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          } catch {
            resolve({ ok: false, ms, error: `HTTP ${res.statusCode}：响应不是合法 JSON` });
            return;
          }
          if (parsed?.retcode === 0 && parsed?.data) {
            resolve({
              ok: true,
              ms,
              userId: parsed.data.user_id != null ? String(parsed.data.user_id) : null,
              nickname: parsed.data.nickname != null ? String(parsed.data.nickname) : null
            });
          } else {
            resolve({ ok: false, ms, error: `OneBot 返回 retcode=${parsed?.retcode ?? '?'}` });
          }
        });
      }
    );
    req.on('timeout', () => {
      req.destroy();
      resolve({ ok: false, error: `探测超时（${PROBE_TIMEOUT_MS}ms）：PC 可能未开机或防火墙拦截` });
    });
    req.on('error', (err) => {
      const code = err?.code ?? '';
      const hint =
        code === 'ECONNREFUSED'
          ? '连接被拒绝：PC 上 SnowLuma 没在运行'
          : code === 'EHOSTUNREACH' || code === 'ENETUNREACH'
            ? '主机不可达：PC 不在同一网络或已关机'
            : code === 'ETIMEDOUT'
              ? '连接超时：PC 可能已关机'
              : String(err?.message ?? err);
      resolve({ ok: false, error: hint });
    });
    req.end(body);
  });
}

/** 探测桥接自己的本地控制台（证明 HTTP 服务确实在监听）。 */
function probeConsole(port, token) {
  return new Promise((resolve) => {
    const started = Date.now();
    const req = http.request(
      {
        hostname: '127.0.0.1',
        port,
        path: '/api/status',
        method: 'GET',
        timeout: 4000,
        headers: token ? { 'x-console-token': token } : {}
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const ms = Date.now() - started;
          try {
            const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
            resolve({ ok: res.statusCode === 200, ms, status: res.statusCode, body: parsed });
          } catch {
            resolve({ ok: false, ms, status: res.statusCode, error: '响应不是合法 JSON' });
          }
        });
      }
    );
    req.on('timeout', () => {
      req.destroy();
      resolve({ ok: false, error: '本地控制台无响应' });
    });
    req.on('error', (err) => resolve({ ok: false, error: String(err?.code ?? err?.message ?? err) }));
    req.end();
  });
}

/**
 * 汇总三层状态 + 主动探测结果。
 * @param {string} bridgeRoot - qq-bridge 仓库根目录。
 */
export async function collectStatus(bridgeRoot) {
  const stateDir = path.join(bridgeRoot, 'state');
  const status = readJson(path.join(stateDir, 'bridge-status.json'));
  const cfg = readJson(path.join(bridgeRoot, 'config.json')) ?? {};
  let consoleToken = '';
  try {
    consoleToken = fs.readFileSync(path.join(stateDir, 'console-token'), 'utf8').trim();
  } catch {}

  const httpUrl = status?.httpUrl || cfg.snowluma?.httpUrl || '';
  const wsUrl = status?.wsUrl || cfg.snowluma?.wsUrl || '';
  const consolePort = status?.consolePort || cfg.consolePort || 3100;

  // ① 桥接进程：心跳新鲜度是主判据，pid 存活作为交叉验证
  const hbAge = status?.heartbeatAt ? Date.now() - status.heartbeatAt : Number.POSITIVE_INFINITY;
  const procAlive = Number.isFinite(hbAge) && hbAge < HEARTBEAT_DEAD_MS;
  const procPidAlive = pidAlive(status?.pid);

  // ②③ 主动探测：OneBot 往返 + 本地控制台
  const [onebot, consoleProbe] = await Promise.all([
    httpUrl ? probeOneBot(httpUrl, cfg.snowluma?.accessToken ?? '') : Promise.resolve({ ok: false, error: '未配置 httpUrl' }),
    probeConsole(consolePort, consoleToken)
  ]);

  // 控制台活着就用它的实时值（比落盘心跳更新），否则退回状态文件
  const live = consoleProbe.ok ? consoleProbe.body : null;
  const qqConnected = live ? live.qqConnected === true : status?.qqConnected === true;
  const dshReady = live ? live.dshReady === true : status?.dshReady === true;
  const mode = live?.mode ?? status?.mode ?? null;

  // 总判定：按「最先断掉的那一层」给结论，避免笼统的「不在线」
  let verdict;
  let verdictKind;
  if (!procAlive && !consoleProbe.ok) {
    verdict = '桥接进程已停止 —— 他完全不会说话';
    verdictKind = 'dead';
  } else if (!onebot.ok) {
    verdict = 'QQ 侧不通 —— 桥接活着，但连不上 PC 上的 SnowLuma';
    verdictKind = 'dead';
  } else if (!qqConnected) {
    verdict = 'QQ 端口通了但 WebSocket 未连上 —— 正在重连中';
    verdictKind = 'warn';
  } else if (!dshReady) {
    verdict = 'DSH 引擎未就绪 —— 消息收得到但没法生成回复';
    verdictKind = 'warn';
  } else {
    verdict = '全链路正常 —— 他在线且能说话';
    verdictKind = 'ok';
  }

  return {
    verdict,
    verdictKind,
    checkedAt: Date.now(),
    process: {
      alive: procAlive,
      pidAlive: procPidAlive,
      pid: status?.pid ?? null,
      startedAt: status?.startedAt ?? null,
      heartbeatAt: status?.heartbeatAt ?? null,
      heartbeatAgeMs: Number.isFinite(hbAge) ? hbAge : null
    },
    qq: {
      connected: qqConnected,
      connectedAt: status?.qqConnectedAt ?? null,
      disconnectedAt: status?.qqDisconnectedAt ?? null,
      lastError: status?.qqLastError ?? null,
      selfId: onebot.userId ?? status?.selfId ?? null,
      selfNickname: onebot.nickname ?? status?.selfNickname ?? null,
      wsUrl,
      httpUrl
    },
    probe: onebot,
    consoleProbe: { ok: consoleProbe.ok, ms: consoleProbe.ms ?? null, error: consoleProbe.error ?? null, port: consolePort },
    dsh: { ready: dshReady, mode },
    traffic: {
      lastInboundAt: status?.lastInboundAt ?? null,
      lastOutboundAt: status?.lastOutboundAt ?? null
    }
  };
}

function row(label, ok, main, sub) {
  const badge = ok === true ? '<span class="b ok">正常</span>' : ok === false ? '<span class="b err">异常</span>' : '<span class="b warn">未知</span>';
  return `<div class="row"><div class="rl">${esc(label)}</div><div class="rm">${badge}<div class="rt">${main}</div>${sub ? `<div class="rs">${sub}</div>` : ''}</div></div>`;
}

function page(s) {
  const kindClass = s.verdictKind === 'ok' ? 'ok' : s.verdictKind === 'warn' ? 'warn' : 'err';
  const p = s.process;
  const q = s.qq;

  const procMain = p.alive
    ? `心跳 ${esc(ago(p.heartbeatAt))}（pid ${esc(p.pid ?? '?')}）`
    : `心跳已停止，最后一次 ${esc(ago(p.heartbeatAt))}`;
  const procSub = p.alive
    ? `启动于 ${esc(fmtTime(p.startedAt))}`
    : p.pidAlive
      ? `pid ${esc(p.pid ?? '?')} 仍在但心跳停了：进程可能卡死`
      : '进程不存在（很可能被系统后台清理掉了）';

  const qqMain = q.connected
    ? `WebSocket 已连接${q.selfNickname ? `，机器人：${esc(q.selfNickname)}` : ''}${q.selfId ? `（${esc(q.selfId)}）` : ''}`
    : 'WebSocket 未连接';
  const qqSub = q.connected
    ? `连接于 ${esc(fmtTime(q.connectedAt))} · ${esc(q.wsUrl)}`
    : `最后断开 ${esc(ago(q.disconnectedAt))}${q.lastError ? ` · 最近错误：${esc(q.lastError)}` : ''}`;

  const probeMain = s.probe.ok
    ? `往返成功，${esc(s.probe.ms ?? '?')}ms —— QQ 账号 ${esc(s.probe.nickname ?? '?')}（${esc(s.probe.userId ?? '?')}）`
    : `探测失败：${esc(s.probe.error ?? '未知原因')}`;
  const probeSub = `目标 ${esc(q.httpUrl || '未配置')}`;

  const dshMain = s.dsh.ready ? `引擎就绪，当前模式 ${esc(s.dsh.mode ?? '?')}` : '引擎未就绪（重启中或已停止）';

  const trafficMain = `最近收到消息：${esc(ago(s.traffic.lastInboundAt))} · 最近发出消息：${esc(ago(s.traffic.lastOutboundAt))}`;

  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QQ 桥接在线状态</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;padding:16px;font:15px/1.6 -apple-system,"PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;
background:#f5f6f8;color:#1a1a1a}
@media(prefers-color-scheme:dark){body{background:#16181c;color:#e8e8ea}}
.wrap{max-width:640px;margin:0 auto}
h1{font-size:19px;margin:0 0 4px}
.sub{font-size:13px;opacity:.6;margin-bottom:14px}
.verdict{padding:14px 16px;border-radius:12px;font-weight:600;font-size:16px;margin-bottom:16px;
border:1px solid transparent}
.verdict.ok{background:#e7f7ed;color:#0f6b35;border-color:#a8dfbd}
.verdict.warn{background:#fff5e0;color:#8a5a00;border-color:#f0d089}
.verdict.err{background:#fdeaea;color:#9b1c1c;border-color:#f2b8b8}
@media(prefers-color-scheme:dark){
.verdict.ok{background:#10331f;color:#7ee0a4;border-color:#1f5c36}
.verdict.warn{background:#33280f;color:#f0c265;border-color:#5c481f}
.verdict.err{background:#331414;color:#f08d8d;border-color:#5c2020}}
.card{background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e3e5e9}
@media(prefers-color-scheme:dark){.card{background:#1f2228;border-color:#2c3037}}
.row{display:flex;gap:12px;padding:13px 16px;border-top:1px solid #eceef1}
.row:first-child{border-top:none}
@media(prefers-color-scheme:dark){.row{border-color:#2c3037}}
.rl{flex:0 0 88px;font-size:13px;opacity:.65;padding-top:2px}
.rm{flex:1;min-width:0}
.rt{font-size:14px;margin-top:3px;word-break:break-all}
.rs{font-size:12px;opacity:.55;margin-top:3px;word-break:break-all}
.b{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;font-weight:600;vertical-align:1px}
.b.ok{background:#d6f2e0;color:#0f6b35}
.b.err{background:#fadcdc;color:#9b1c1c}
.b.warn{background:#fbecc9;color:#8a5a00}
@media(prefers-color-scheme:dark){
.b.ok{background:#1c4f2e;color:#8ee8ad}.b.err{background:#5c2020;color:#f5a3a3}.b.warn{background:#5c481f;color:#f2cc7a}}
.foot{margin-top:14px;font-size:12px;opacity:.55;text-align:center}
.btn{display:block;width:100%;margin-top:14px;padding:11px;border-radius:10px;border:1px solid #d4d7dd;
background:#fff;color:inherit;font:inherit;font-weight:600;cursor:pointer}
@media(prefers-color-scheme:dark){.btn{background:#252a31;border-color:#363b44}}
.tip{margin-top:12px;padding:11px 13px;border-radius:10px;background:#eef1f5;font-size:12.5px;line-height:1.7}
@media(prefers-color-scheme:dark){.tip{background:#21252b}}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}
</style></head><body><div class="wrap">
<h1>QQ 桥接在线状态</h1>
<div class="sub">检测时间 ${esc(fmtTime(s.checkedAt))} · 每 10 秒自动刷新</div>
<div class="verdict ${kindClass}">${esc(s.verdict)}</div>
<div class="card">
${row('桥接进程', p.alive, procMain, procSub)}
${row('真实联通', s.probe.ok, probeMain, probeSub)}
${row('QQ 连接', q.connected, qqMain, qqSub)}
${row('DSH 引擎', s.dsh.ready, dshMain, `本地控制台 ${s.consoleProbe.ok ? `可用（${esc(s.consoleProbe.ms ?? '?')}ms）` : `不可用：${esc(s.consoleProbe.error ?? '?')}`} · 端口 ${esc(s.consoleProbe.port)}`)}
${row('消息往来', null, trafficMain, '')}
</div>
<button class="btn" onclick="location.reload()">立即重新检测</button>
<div class="tip"><b>三层都要绿才会说话：</b>桥接进程活着 → QQ 侧真的连上 PC → DSH 引擎能生成回复。<br>
「真实联通」是主动向 PC 的 OneBot 端口发一次 <code>get_login_info</code>，能拿回 QQ 号才算整条链路通。<br>
JSON 接口：<code>${esc(JSON_PATH)}</code></div>
<div class="foot">qq-mode-console · ${esc(PAGE_PATH)}</div>
</div>
<script>setTimeout(function(){location.reload()},10000)</script>
</body></html>`;
}

/**
 * 在 webServer 上注册状态页。webServer 缺失时告警并跳过。
 * @param {object} ctx - cordis 上下文。
 * @param {string} bridgeRoot - qq-bridge 仓库根目录。
 */
export function registerStatusPage(ctx, bridgeRoot) {
  const webServer = ctx.get?.('webServer') ?? ctx.webServer;
  if (webServer === void 0 || typeof webServer.register !== 'function') {
    ctx.logger?.warn?.('qq-mode-console: webServer 服务不可用，跳过在线状态页注册');
    return;
  }

  const dispose1 = webServer.register({
    kind: 'exact',
    path: PAGE_PATH,
    handler: async (req, res) => {
      try {
        const s = await collectStatus(bridgeRoot);
        const html = page(s);
        res.writeHead(200, {
          'content-type': 'text/html; charset=utf-8',
          'cache-control': 'no-store',
          'x-content-type-options': 'nosniff'
        });
        res.end(html);
      } catch (error) {
        res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' });
        res.end(`状态页出错：${error?.message ?? error}`);
      }
    }
  });

  const dispose2 = webServer.register({
    kind: 'exact',
    path: JSON_PATH,
    handler: async (req, res) => {
      try {
        const s = await collectStatus(bridgeRoot);
        res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
        res.end(JSON.stringify(s, null, 2));
      } catch (error) {
        res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify({ error: String(error?.message ?? error) }));
      }
    }
  });

  return () => {
    dispose1?.();
    dispose2?.();
  };
}

export { PAGE_PATH as STATUS_PAGE_PATH, JSON_PATH as STATUS_JSON_PATH };
