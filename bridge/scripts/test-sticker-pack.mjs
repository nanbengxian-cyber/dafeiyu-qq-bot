// 本地表情包体系自检：
// 1) sticker-pack 纯函数（扫描/标签/slug/合并/查找/格式化/上下文/使用统计）
// 2) 配置项与 MCP 工具描述存在性
// 3) 真实扫描「表情包集」目录，验证 id/标签/大小/mime 正确
// 可选 live：QQ_BRIDGE_TEST_PACK_LIVE=1 时读取一张 GIF 并构造 base64 image 段（不发送）
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  categoryLabelFromDirname,
  slugFromLabel,
  scanLocalStickerDir,
  mergeLocalPacks,
  findLocalSticker,
  formatLocalPackList,
  buildLocalPackContext,
  buildLocalPackStrategyHint,
  markLocalPackUsed
} from '../src/sticker-pack.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const PACK_DIR = path.resolve(ROOT, '..', '表情包集1');

let pass = 0;
let fail = 0;
function ok(cond, name) {
  if (cond) { pass += 1; console.log(`  ✅ ${name}`); }
  else { fail += 1; console.error(`  ❌ ${name}`); }
}

console.log('## 纯函数');
ok(categoryLabelFromDirname('（装可怜表情包）') === '装可怜', 'categoryLabelFromDirname 去括弧+尾缀');
ok(categoryLabelFromDirname('送情书表情包') === '送情书', 'categoryLabelFromDirname 无括弧');
ok(slugFromLabel('装可怜') === '装可怜', 'slugFromLabel 保留中文');
ok(slugFromLabel('送 情 书') === '送_情_书', 'slugFromLabel 空格转下划线');
ok(slugFromLabel('') === 'sticker', 'slugFromLabel 空标签回退');

const scanned = scanLocalStickerDir(PACK_DIR);
ok(scanned.length === 18, `扫描「表情包集1」得到 ${scanned.length} 个分类（期望 18）`);
ok(scanned.every((e) => e.id.startsWith('local:')), '所有条目 id 带 local: 前缀');
ok(scanned.every((e) => e.mime === 'image/gif'), '所有条目 mime 为 image/gif');
ok(scanned.every((e) => e.size > 0), '所有条目 size > 0');
ok(scanned.some((e) => e.category === '装可怜'), '含「装可怜」分类');
ok(scanned.every((e) => e.hint), '每个分类都有场景提示 hint');

const merged = mergeLocalPacks([], scanned);
ok(merged.length === 18, 'mergeLocalPacks 保留全部条目');
ok(findLocalSticker(merged, '装可怜')?.id === 'local:装可怜', 'findLocalSticker 按标签查找');
ok(findLocalSticker(merged, 'local:小丑')?.category === '小丑', 'findLocalSticker 按 id 查找');
ok(findLocalSticker(merged, '不存在') === null, 'findLocalSticker 找不到返回 null');

const list = formatLocalPackList(merged, '', 100);
ok(list.total === 18 && list.stickers.length === 18, 'formatLocalPackList 总数');
ok(Object.keys(list.groups).length === 18, 'formatLocalPackList 分组数');
const q = formatLocalPackList(merged, '撒娇', 100);
ok(q.matched > 0 && q.stickers.some((s) => s.label === '装可怜'), 'formatLocalPackList 按 hint 搜索');

const ctx = buildLocalPackContext(merged, 12);
ok(ctx.includes('表情包集') && ctx.includes('18 个分类') && ctx.includes('qq_list_local_stickers'), 'buildLocalPackContext 含总数/工具名');
ok(ctx.includes('小丑') || ctx.includes('装可怜'), 'buildLocalPackContext 列出实际分类');
ok(buildLocalPackStrategyHint().includes('qq_send_local_sticker'), '策略提示包含发送工具');
ok(buildLocalPackStrategyHint().includes('节奏提醒') && /50%/.test(buildLocalPackStrategyHint()), '策略提示包含“每3句50%”节奏');

const used = markLocalPackUsed(merged, 'local:送花', '夸人');
ok(used.entry.useCount === 1 && used.entry.lastContext === '夸人', 'markLocalPackUsed 记录使用');

console.log('## 配置文件');
{
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8'));
  const tools = cfg.socialV2?.tools ?? {};
  ok(tools.listLocalStickers === true, 'config listLocalStickers=true');
  ok(tools.getLocalStickerImage === true, 'config getLocalStickerImage=true');
  ok(tools.sendLocalSticker === true, 'config sendLocalSticker=true');
  ok(cfg.socialV2?.stickerPack?.enabled === true, 'config stickerPack.enabled=true');
  ok(cfg.socialV2?.stickerPack?.maxBytes > 0, 'config stickerPack.maxBytes 已设置');
  ok(Array.isArray(cfg.socialV2?.stickerPack?.allowedExt), 'config stickerPack.allowedExt 为数组');
  ok(cfg.socialV2?.stickerPack?.cadence?.enabled === true, 'config stickerPack.cadence.enabled=true');
  ok(Number(cfg.socialV2?.stickerPack?.cadence?.probability) === 0.5, 'config stickerPack.cadence.probability=0.5');
  ok(Number(cfg.socialV2?.stickerPack?.cadence?.everyMessages) === 3, 'config stickerPack.cadence.everyMessages=3');
}

console.log('## MCP 工具注册存在性');
{
  const mcp = fs.readFileSync(path.join(ROOT, 'src', 'mcp-snowluma-safe.js'), 'utf8');
  for (const name of ['qq_list_local_stickers', 'qq_get_local_sticker_image', 'qq_send_local_sticker']) {
    ok(mcp.includes(`'${name}'`) || mcp.includes(`"${name}"`), `MCP 工具 ${name} 已注册`);
  }
}

console.log('## bridge 路由/函数存在性');
{
  const bridge = fs.readFileSync(path.join(ROOT, 'src', 'bridge.js'), 'utf8');
  for (const s of [
    '/api/socialV2/local-sticker-list',
    '/api/socialV2/local-sticker-image',
    '/api/socialV2/send-local-sticker',
    'sendLocalStickerV2',
    'getLocalStickerData',
    'syncLocalPackLibrary',
    'localStickerPackEnabled',
    "scanLocalStickerDir"
  ]) {
    ok(bridge.includes(s), `bridge 包含 ${s}`);
  }
}

console.log('## agent 预设');
{
  const preset = fs.readFileSync(path.join(ROOT, 'dsh', 'agent-presets', 'qq-chat-v2', 'agent.cordis.yml'), 'utf8');
  for (const s of ['qq_list_local_stickers', 'qq_get_local_sticker_image', 'qq_send_local_sticker', '26.1']) {
    ok(preset.includes(s), `preset 包含 ${s}`);
  }
}

if (process.env.QQ_BRIDGE_TEST_PACK_LIVE === '1') {
  console.log('## live：读 GIF 构造 base64 image 段（不发送）');
  const entry = findLocalSticker(merged, '装可怜');
  ok(!!entry, '找到「装可怜」条目');
  if (entry && fs.existsSync(entry.file)) {
    const buf = fs.readFileSync(entry.file);
    const b64 = buf.toString('base64');
    ok(buf.length > 0, `读取 GIF ${Math.round(entry.size / 1024)}KB`);
    ok(b64.length > 0 && b64.length * 3 / 4 >= buf.length - 4, 'base64 编码有效');
    const seg = { type: 'image', data: { file: `base64://${b64}` } };
    ok(seg.data.file.startsWith('base64://'), '构造 base64:// image 段成功');
    const mime = buf.toString('ascii', 0, 6) === 'GIF89a' || buf.toString('ascii', 0, 6) === 'GIF87a' ? 'image/gif' : 'unknown';
    ok(mime === 'image/gif', 'GIF 魔数校验为 image/gif');
  } else {
    ok(false, '「装可怜」文件不存在，无法 live 验证');
  }
}

console.log(`\n结果：${pass} 通过，${fail} 失败`);
if (fail > 0) process.exit(1);
