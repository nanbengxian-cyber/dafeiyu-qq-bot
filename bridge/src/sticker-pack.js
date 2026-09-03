// 本地表情包库（纯函数）——把「表情包集」目录下的分类 GIF 表情包索引成语义化条目。
//
// 职责：
// - 扫描目录：每级子目录视为一个“分类/表情包”，内部取第一个受支持的图片文件。
// - 产出稳定 id（local:<分类标签>）、分类标签、场景提示 hint、文件路径、mime、大小。
// - 持久化到 state/sticker-packs.json（记录使用次数/最近使用/自动学习笔记），并在扫描时回填文件元数据。
// - 纯函数，不做任何网络/发送；发送与看图逻辑在 bridge.js 复用。
//
// 设计原则：
// - 与「QQ 收藏表情」（sticker-lib.js，id 为 emoji_id/md5/url）完全隔离，id 命名空间带 `local:` 前缀，避免撞车。
// - AI 选表情主要靠“分类语义标签 + hint”，不依赖看图；看图工具是可选的确认手段。

import fs from 'node:fs';
import path from 'node:path';

// 分类 → “什么时候用”的语义场景建议（AI 选表情包的依据）。
export const CATEGORY_HINTS = {
  '装可怜': '撒娇、示弱、求原谅、装惨、求人帮忙',
  '假装没伤心': '假装坚强、表面没事但心里在意、嘴硬、嘴上的硬气',
  '送花': '夸人、表达赞赏/谢意、撩、哄人、恭喜',
  '送情书': '撩、表白、表达好感、玩笑式告白、逗、土味情话',
  '小丑': '自嘲、调侃、玩梗（“我才是小丑”）、被整活、尴尬化解',
  '嘲笑': '怼人、调侃、阴阳怪气、看戏、幸灾乐祸',
  '装酷': '耍帅、硬撑、故作镇定、装 X、装作不在乎',
  '装萌': '卖萌、撒娇、装可爱、软化语气、反差',
  '喷漆': '无语、震惊、被雷到、画风突变、场面尴尬',
  '熬夜': '夜猫子、熬夜/失眠话题、深夜感叹、熬不动了',
  '思考': '思考、疑惑、斟酌、假装认真、脑筋急转弯',
  '写日记': '记录、复盘、有感而发、写小作文、今天的事',
  '记录': '记录、打卡、见证、留档、翻旧账',
  '拍照': '拍照、记录瞬间、拍下证据/尴尬/好笑的一幕',
  '弹音乐': '玩梗、活跃气氛、自带 BGM、秀操作、越界整活',
  '摇铃': '提醒、召唤、开饭/上课/集合、搞怪、点名',
  '被敲打': '被教育/被锤、挨打、自嘲挨揍、认怂',
  '探头': '悄悄出现、围观、吃瓜、冒泡、暗中观察',
};

const EXT_TO_MIME = {
  '.gif': 'image/gif',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp'
};

export function nowIso() {
  return new Date().toISOString();
}

export function mimeFromExt(ext) {
  return EXT_TO_MIME[String(ext ?? '').toLowerCase()] || 'image/gif';
}

// 从文件夹名提取“短标签”：去掉 （）（）【】《》 等外层括弧与“表情包”尾缀。
export function categoryLabelFromDirname(dirName) {
  let s = String(dirName ?? '').trim();
  s = s.replace(/^[（(【\[《〈]+/, '').replace(/[）)】\]》〉]+$/, '');
  s = s.replace(/表情包$/u, '').trim();
  return s;
}

// 生成稳定 slug（保留中日韩文与数字，其余不合法字符用 _ 替代）。
export function slugFromLabel(label) {
  const s = String(label ?? '').trim();
  const slug = s.replace(/[^\p{L}\p{N}_-]+/gu, '_').replace(/^_+|_+$/g, '');
  return slug || 'sticker';
}

// 归一化一条本地表情条目（供加载/持久化/返回共用）。
export function normalizeLocalSticker(raw) {
  const entry = raw && typeof raw === 'object' ? raw : {};
  const id = String(entry.id || '').trim();
  if (!id) return null;
  return {
    id,
    category: String(entry.category || '').trim(),
    label: String(entry.label || entry.category || '').trim(),
    hint: String(entry.hint || '').trim(),
    file: String(entry.file || '').trim(),
    mime: String(entry.mime || 'image/gif').trim(),
    size: Math.max(0, Number(entry.size) || 0),
    useCount: Math.max(0, Number(entry.useCount) || 0),
    lastUsedAt: Number(entry.lastUsedAt) || 0,
    lastContext: String(entry.lastContext || '').slice(0, 200),
    createdAt: String(entry.createdAt || nowIso()),
    updatedAt: String(entry.updatedAt || nowIso())
  };
}

// 扫描目录：每个子目录一个分类，取其中第一个受支持图片文件。
export function scanLocalStickerDir(dir) {
  const root = String(dir ?? '').trim();
  if (!root) return [];
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) return [];
  const out = [];
  const seen = new Set();
  let names;
  try {
    names = fs.readdirSync(root).sort();
  } catch {
    return [];
  }
  for (const name of names) {
    const full = path.join(root, name);
    let stat;
    try { stat = fs.statSync(full); } catch { continue; }
    if (!stat.isDirectory()) continue;
    const label = categoryLabelFromDirname(name);
    if (!label) continue;
    let cat = label;
    if (seen.has(cat)) {
      let i = 2;
      while (seen.has(cat + '_' + i)) i += 1;
      cat = cat + '_' + i;
    }
    let file = '';
    let mime = '';
    let size = 0;
    let files;
    try { files = fs.readdirSync(full).sort(); } catch { continue; }
    for (const fn of files) {
      if (fn.startsWith('.')) continue;
      const ext = path.extname(fn).toLowerCase();
      if (!EXT_TO_MIME[ext]) continue;
      const fp = path.join(full, fn);
      try {
        const st = fs.statSync(fp);
        if (st.isFile()) { file = fp; mime = mimeFromExt(ext); size = st.size; break; }
      } catch { continue; }
    }
    if (!file) continue;
    seen.add(cat);
    out.push({
      id: `local:${slugFromLabel(cat)}`,
      category: cat,
      label: cat,
      hint: CATEGORY_HINTS[cat] || '',
      file,
      mime,
      size
    });
  }
  return out;
}

// 读取已持久化的本地表情库。
export function loadLocalPacks(file) {
  try {
    let text = fs.readFileSync(file, 'utf8');
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    const parsed = JSON.parse(text);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(normalizeLocalSticker).filter(Boolean);
  } catch {
    return [];
  }
}

export function saveLocalPacks(file, entries) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.${Math.random().toString(36).slice(2)}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(entries, null, 2), { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(tmp, file);
  } catch (error) {
    // 持久化失败不阻断主流程（发送/看图仍可用内存态）。
    if (typeof console !== 'undefined') console.error(`[sticker-pack] 保存本地表情库失败: ${error?.message ?? error}`);
  }
}

// 合并“扫描到的目录条目”与“持久化的使用统计”：以 id 对齐，回填文件元数据，保留 useCount/lastUsedAt/lastContext/note。
export function mergeLocalPacks(persisted, scanned) {
  const byId = new Map();
  for (const p of persisted.map(normalizeLocalSticker).filter(Boolean)) byId.set(p.id, p);
  const out = [];
  for (const s of scanned.map(normalizeLocalSticker).filter(Boolean)) {
    const old = byId.get(s.id);
    out.push(normalizeLocalSticker({
      ...s,
      useCount: old?.useCount || 0,
      lastUsedAt: old?.lastUsedAt || 0,
      lastContext: old?.lastContext || '',
      note: old?.note || '',
      createdAt: old?.createdAt || nowIso(),
      updatedAt: nowIso()
    }));
  }
  return out.filter(Boolean);
}

// 通过 id / category / label / 文件路径查找本地表情。
export function findLocalSticker(entries, ref) {
  const raw = String(ref ?? '').trim();
  if (!raw) return null;
  const norm = raw.replace(/^local:/i, '');
  return (Array.isArray(entries) ? entries : []).find((e) => {
    if (!e) return false;
    if (e.id === raw) return true;
    if (e.category === raw || e.category === norm) return true;
    if (e.label === raw || e.label === norm) return true;
    if (String(e.file) === raw) return true;
    return false;
  }) || null;
}

// 格式化给 AI 看的本地表情列表；query 匹配 label/category/hint/id。
export function formatLocalPackList(entries, query = '', limit = 48) {
  const list = (Array.isArray(entries) ? entries : []).map(normalizeLocalSticker).filter(Boolean);
  const q = String(query ?? '').trim().toLowerCase();
  const filtered = q
    ? list.filter((e) => {
        const haystack = [e.id, e.category, e.label, e.hint].join(' ').toLowerCase();
        return haystack.includes(q);
      })
    : list;
  const max = Math.max(1, Math.min(500, Number(limit) || 48));
  const items = filtered.slice(0, max).map((e) => ({
    id: e.id,
    category: e.category,
    label: e.label,
    hint: e.hint,
    mime: e.mime,
    size: e.size,
    useCount: e.useCount || 0,
    lastUsedAt: e.lastUsedAt || 0
  }));
  const groups = {};
  for (const it of items) {
    const key = it.category || '未分类';
    groups[key] = (groups[key] || 0) + 1;
  }
  return {
    total: list.length,
    matched: filtered.length,
    truncated: filtered.length > max,
    stickers: items,
    groups
  };
}

// 生成注入 AI 的“可用本地表情包”摘要（按分类列出标签 + 一句适用场景，不暴露路径）。
export function buildLocalPackContext(entries, max = 12) {
  const list = (Array.isArray(entries) ? entries : []).map(normalizeLocalSticker).filter(Boolean);
  if (!list.length) return '';
  const top = [...list]
    .sort((a, b) => (b.useCount || 0) - (a.useCount || 0))
    .slice(0, Math.max(1, Math.min(50, Number(max) || 12)));
  const lines = top.map((e) => {
    const label = e.label || e.category || '未命名';
    const hint = e.hint ? `（${e.hint}）` : '';
    const used = e.useCount ? `用过${e.useCount}次` : '';
    return `- ${label}${hint}${used ? ` ${used}` : ''}`;
  });
  return `【本地表情包】你的表情包集里有 ${list.length} 个分类的表情（以下为 ${top.length} 个常见/近用的，完整列表请用 qq_list_local_stickers 查看）：\n${lines.join('\n')}`;
}

// “真人发表情包”策略提示（本地表情包与 QQ 收藏表情共用同一套软策略）。
export function buildLocalPackStrategyHint() {
  return [
    '【本地表情包策略：像真人一样用，不刷屏】',
    '- 合适时机：被戳中笑点/槽点、接梗、怼人、赞同、自嘲、安慰、无语、赢了/输了、告别/晚安、别人发了表情时回一张，都可以自然用。',
    '- 频率：普通闲聊不用每条都配；大约每 3~5 轮来一张就够，热闹/玩梗时可以更密，但不要连续刷屏。',
    '- 节奏提醒：你每连续说 3 句话左右，就应有一次（约 50% 概率）发一张表情包；被提醒“该发表情”时就用 qq_send_local_sticker 发一张，不要每次都发、也不要一直不发。',
    '- 选择：优先用分类标签（label）和场景提示（hint）能准确对上语境的；没有把握的先 qq_get_local_sticker_image 看图再决定，不要瞎发。',
    '- 发送：用 qq_send_local_sticker；一条消息只能是一张表情，不能在同一气泡里附带文字；想说的话先用 qq_send_message / qq_reply 作为单独气泡发出，再单独发表情。需要引用/点名时传 replyToMessageId / atUserId（群聊）。',
    '- 不要：在严肃/正式/敏感话题硬塞表情；不要每次都用同一个；不要一条消息里塞多个表情；不要把文字和表情混在同一个气泡里；不要把表情包当回复的唯一内容（偶尔可以，但别让群友觉得你在敷衍）。'
  ].join('\n');
}

// 记录一次“使用”，返回新数组。
export function markLocalPackUsed(entries, id, context = '') {
  const list = (Array.isArray(entries) ? entries : []).map(normalizeLocalSticker).filter(Boolean);
  const target = findLocalSticker(list, id);
  if (!target) return { entries: list, entry: null };
  const idx = list.findIndex((e) => e.id === target.id);
  const next = normalizeLocalSticker({
    ...target,
    useCount: (target.useCount || 0) + 1,
    lastUsedAt: Date.now(),
    lastContext: String(context || '').slice(0, 200),
    updatedAt: nowIso()
  });
  if (!next) return { entries: list, entry: null };
  list[idx] = next;
  return { entries: list, entry: next };
}

// 给本地表情写一条 AI 自动学习到的“含义/用法”笔记（不覆盖分类 hint）。
export function applyLocalPackNote(entries, id, patch = {}) {
  const list = (Array.isArray(entries) ? entries : []).map(normalizeLocalSticker).filter(Boolean);
  const target = findLocalSticker(list, id);
  if (!target) return { entries: list, entry: null };
  const idx = list.findIndex((e) => e.id === target.id);
  const note = patch.note !== undefined ? String(patch.note ?? '').trim() : (target.note || '');
  const next = normalizeLocalSticker({
    ...target,
    note,
    updatedAt: nowIso()
  });
  if (!next) return { entries: list, entry: null };
  list[idx] = next;
  return { entries: list, entry: next };
}
