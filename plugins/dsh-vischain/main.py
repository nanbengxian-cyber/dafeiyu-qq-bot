# dsh-vischain —— 识图模型的「主用 + 备用」链
#
# 要解决什么
# ----------
# 用户要求把 claude-opus-5 / claude-opus-5-thinking 放到识图第一序列，
# 原来的 glm-4.6v-flash 降为备用。但框架只给了**一个**格子：
#   provider_settings.default_image_caption_provider_id  (astr_main_agent.py:1032)
# 它是个字符串，不是列表 —— 没有「主用挂了换备用」这回事。
#
# 而这条线路必须有备用，不是可选项。实测 justwoker 站（Cloudflare 后面）
# 带图请求稳定约 20% 概率被边缘节点弹回 403 code 1010：
#   claude-opus-5           成功 8/10
#   claude-opus-5-thinking  成功 7/10
# 没有备用的话，群友每发五张图就有一张换来「[Image Captioning Failed]」。
#
# 为什么串起来几乎不花钱
# ----------------------
# 那个 403 是**瞬时**返回的（实测 min/中位/max 全是 0.0s，边缘节点在收完
# 请求体之前就拒了），而成功一次要 5.6~10.2s。所以「主用秒挂 → 换备用」
# 的代价约等于零，而「主用挂了就放弃」的代价是整张图看不见。
# 同模型立刻重试也能救：8 次里 6 次一遍过、2 次重试救回、0 次三连死。
# 所以每档先原地重试，再换下一档。
#
# 为什么是「顶替 id」而不是「新建一个 id」
# ------------------------------------------
# 最直觉的做法是新建 vision-chain 这个 id，然后把配置指过去。但那样一来，
# 这个插件万一没加载起来，配置指向一个不存在的 provider —— 识图**整条腿断**
# （框架只会打一行 "Provider ... was not found" 然后跳过转述）。
# 现在的做法是：配置照旧指向真实存在的 vision-opus5，插件在启动后把
# provider_manager.inst_map["vision-opus5"] 换成链条包装器。插件没起来，
# 拿到的就是原装 opus-5 单打独斗（80% 成功），坏也只坏成「没有备用」，
# 不会坏成「没有识图」。降级要往「少个功能」倒，不能往「整条断」倒。
#
# 顺手把三条识图入口一起覆盖了
# ----------------------------
# 都走 get_provider_by_id / llm_generate → 都读 inst_map，所以换一处就够：
#   ① 框架转述当前消息自带的图 (_ensure_img_caption)
#   ② 框架转述被引用消息里的图 (_process_quote_message)
#   ③ dsh-imgctx 转述前几条消息里的图（PROVIDER_ID 留空时回落到同一个配置项）
# dsh-video 的视频抽帧识别走它自己的 DSH_VID_VIS_* HTTP 客户端，不经过
# provider 系统，不在本插件范围内。

import asyncio
import os
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.provider.provider import Provider

# 链条顺序：逗号分隔的 provider id，前面的优先。
# 默认值就是用户要的顺序：两个 opus-5 在前，glm 兜底。
CHAIN = [
    s.strip()
    for s in os.environ.get(
        "DSH_VIS_CHAIN", "vision-opus5,vision-opus5-thinking,zhipu-vision"
    ).split(",")
    if s.strip()
]
# 要被顶替的 provider id。必须与 default_image_caption_provider_id 一致，
# 且必须是**真实存在**的 provider —— 见上面「为什么是顶替」。
ALIAS = os.environ.get("DSH_VIS_ALIAS", "vision-opus5").strip()
# 同一档原地重试几次（不含首次）。只在瞬时错误上重试。
RETRY = int(os.environ.get("DSH_VIS_RETRY", "2"))
# 单次尝试的超时。实测成功最慢 10.2s，给 25s 留足余量；
# 超时也算瞬时错误，会换下一档而不是把整个回复拖死。
ATTEMPT_TIMEOUT = float(os.environ.get("DSH_VIS_TIMEOUT", "25"))
ENABLED = os.environ.get("DSH_VIS_CHAIN_ENABLE", "1") not in ("0", "false", "False", "")
# 看护间隔。WebUI 里改任何一个 provider 会走 provider_manager.reload()：
# 它 terminate + 重新 load，把 inst_map[id] 换成全新的裸实例 ——
# 顶替就被悄悄冲掉了（识图还能用，但备用链没了，回落到单模型约 80%）。
# 更阴的是非第一档被改时，链条手里攥着的是已 terminate 的旧实例。
# 所以按 id 定期核对身份，发现被换过就地重建。
WATCH_INTERVAL = float(os.environ.get("DSH_VIS_WATCH", "30"))

# 一档被判「没通道」后多久内直接跳过。0=不缓存。
DEAD_TTL = float(os.environ.get("DSH_VIS_DEAD_TTL", "600"))

# 瞬时错误特征。403/1010 是 Cloudflare 指纹弹回，429 是限流，
# 都属于「同一档再试一次就可能成」。
_TRANSIENT = ("403", "1010", "429", "timeout", "timed out", "connection", "502", "503", "504")
# [patch:perm-v1 503 不等于瞬时]
# 永久错误：同一档再试一百次也不会变，重试只是白拖时间。
#
# 实测事故：一小时内 101 次上游重试，全是同一句
#   `503 - model_not_found: No available channel for model claude-opus-5-thinking`
# ——「这个模型在渠道里根本没有通道」是**配置**问题，不是抖动。但 503 在
# _TRANSIENT 里，于是每张图都要：2 档 × (1+RETRY) 次 × 各自约 2.4s ≈ 14 秒
# 全花在必然失败的两档上，才轮到真正能用的第三档。
#
# 教训与 dsh-imagegen 那条同源：**状态码是模糊的，响应体才说清了是什么**。
# 光看 503 分不出「服务在抖」和「模型不存在」，必须看 body 里的 code。
# 所以永久特征优先于瞬时特征。
_PERMANENT = (
    "model_not_found", "no available channel", "does not exist",
    "invalid_api_key", "invalid api key", "insufficient_quota",
    "unsupported", "code: 401", "error code: 404",
)
# pid -> 判定为「没通道」的时刻。只影响跳过顺序，不影响 fail-open。
_dead: dict[str, float] = {}


def _permanent(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in _PERMANENT)


def _transient(e: Exception) -> bool:
    if _permanent(e):
        return False
    s = str(e).lower()
    return any(k in s for k in _TRANSIENT) or isinstance(e, asyncio.TimeoutError)


class ChainProvider(Provider):
    """按顺序试多个真实 provider，第一个成功的就返回。

    只包装 text_chat（识图转述唯一走的方法）。其余方法委托给第一档，
    这样 provider_config / get_model / test 这些框架会读的东西行为不变。
    """

    def __init__(self, links: list[Provider], settings: dict):
        self.links = links
        head = links[0]
        # 用第一档的配置做自己的配置：框架会读 modalities（必须含 image，
        # 否则 _provider_supports_modality 判否）、id、max_context_tokens。
        super().__init__(head.provider_config, settings)
        # 每档的战绩，/识图状态 用它给出真实命中率而不是猜
        self.stat: dict[str, dict] = {
            lk.provider_config.get("id", "?"): {"ok": 0, "fail": 0, "sec": 0.0}
            for lk in links
        }

    # ---------------------------------------------------------------- 委托
    def get_model(self) -> str:
        return self.links[0].get_model()

    def get_models(self):
        return self.links[0].get_models()

    def set_model(self, model_name: str) -> None:
        self.links[0].set_model(model_name)

    def get_keys(self):
        return self.links[0].get_keys()

    def get_current_key(self) -> str:
        return self.links[0].get_current_key()

    def set_key(self, key: str) -> None:
        self.links[0].set_key(key)

    def meta(self):
        return self.links[0].meta()

    def pop_record(self, context: list):
        return self.links[0].pop_record(context)

    async def test(self, timeout: float = 45.0) -> None:
        return await self.links[0].test(timeout=timeout)

    def text_chat_stream(self, *a, **kw):
        # 识图转述从不流式；流式请求直接交给第一档，不在这里搞链条。
        return self.links[0].text_chat_stream(*a, **kw)

    # ---------------------------------------------------------------- 链条
    def _order(self) -> list:
        """把最近判定「没通道」的档排到最后，而不是删掉。

        删掉就等于自己给自己造了个单点：渠道恢复了也永远试不到。
        排后面则是「先试可能通的，仍然全试一遍」—— fail-open。
        """
        if DEAD_TTL <= 0:
            return list(self.links)
        now = time.time()
        alive, dead = [], []
        for lk in self.links:
            pid = lk.provider_config.get("id", "?")
            t = _dead.get(pid, 0.0)
            if t and now - t < DEAD_TTL:
                dead.append(lk)
            else:
                if t:
                    _dead.pop(pid, None)   # 过期了，恢复正常顺序
                alive.append(lk)
        if dead:
            # 必须留痕：不打日志的话，下次「识图怎么换档了」又只能靠猜。
            logger.info(
                "[vischain] 本轮先跳过 %s（%.0f 分钟内判定为无通道，仍会兜底再试）",
                ",".join(lk.provider_config.get("id", "?") for lk in dead),
                DEAD_TTL / 60.0,
            )
        return alive + dead

    async def text_chat(self, *args, **kwargs):
        last = None
        links = self._order()
        for lk in links:
            pid = lk.provider_config.get("id", "?")
            st = self.stat.setdefault(pid, {"ok": 0, "fail": 0, "sec": 0.0})
            for attempt in range(RETRY + 1):
                t0 = time.time()
                try:
                    resp = await asyncio.wait_for(
                        lk.text_chat(*args, **kwargs), timeout=ATTEMPT_TIMEOUT
                    )
                except Exception as e:  # noqa: BLE001
                    dt = time.time() - t0
                    st["fail"] += 1
                    last = e
                    perm = _permanent(e)
                    tr = _transient(e)
                    if perm:
                        _dead[pid] = time.time()
                    logger.warning(
                        "[vischain] %s 第%d次失败(%.1fs, %s): %s",
                        pid,
                        attempt + 1,
                        dt,
                        "没通道／永久错误，直接换下一档并记 %.0f 分钟"
                        % (DEAD_TTL / 60.0) if perm
                        else ("瞬时可重试" if tr else "非瞬时，直接换下一档"),
                        str(e)[:120],
                    )
                    if not tr:
                        break  # 非瞬时错误原地重试没意义，换档
                    continue
                dt = time.time() - t0
                st["ok"] += 1
                st["sec"] += dt
                _dead.pop(pid, None)     # 成功一次就洗掉黑名单
                # 不触发/降级路径也要留痕：走到第几档、试了几次，
                # 否则以后排查「识图怎么慢了」只能靠猜。
                if lk is self.links[0] and attempt == 0:
                    logger.info("[vischain] %s 一次过（%.1fs）", pid, dt)
                else:
                    logger.info(
                        "[vischain] 降级到 %s 才成功（第%d档第%d次，%.1fs）",
                        pid,
                        self.links.index(lk) + 1,
                        attempt + 1,
                        dt,
                    )
                return resp
        logger.error("[vischain] %d 档全挂，识图放弃", len(self.links))
        if last is not None:
            raise last
        raise RuntimeError("vischain: 没有可用的识图 provider")


class Main(star.Star):
    def __init__(self, context):
        self.context = context
        self.chain: ChainProvider | None = None
        self.note = "未初始化"
        self._watch: asyncio.Task | None = None
        self._reinstalls = 0

    # ---------------------------------------------------------------- 安装
    def _live(self, pm, pid):
        """取 pid 当前的**真实**实例（剥掉自己那层壳，防套娃）。"""
        inst = pm.inst_map.get(pid)
        while isinstance(inst, ChainProvider):
            inst = inst.links[0] if inst.links else None
        return inst

    def _ensure(self, pm, quiet=False) -> bool:
        """确保 inst_map[ALIAS] 是当前配置对应的链条。已经对了就什么都不做。

        返回 True 表示这次动过手（首装或重装）。
        """
        cur = pm.inst_map.get(ALIAS)
        # 逐档核对身份：只要有一档不是 inst_map 里那个活实例，就得重建。
        # 只比 ALIAS 一个不够 —— 单独改 zhipu-vision 时 ALIAS 没被碰，
        # 但链条第三档已经是 terminate 掉的死实例了。
        if isinstance(cur, ChainProvider) and cur is self.chain:
            # _live 已经会剥掉壳，所以 ALIAS 那一档取到的就是壳里的真实实例，
            # 不需要特别处理。（早先版本在这里写了 CHAIN.index(ALIAS)，
            # 一旦有人把 ALIAS 设成不在 CHAIN 里的 id 就每 30s 抛一次 ValueError。）
            want = [lk for lk in (self._live(pm, pid) for pid in CHAIN) if lk is not None]
            if want == list(cur.links):
                return False

        links, missing = [], []
        for pid in CHAIN:
            inst = self._live(pm, pid)
            (links.append(inst) if inst is not None else missing.append(pid))
        if not links:
            self.note = "链条里一个 provider 都没找到：%s" % ",".join(CHAIN)
            logger.error("[vischain] %s，识图保持原样", self.note)
            return False
        if ALIAS not in pm.inst_map:
            self.note = "顶替目标 %s 不存在" % ALIAS
            logger.error("[vischain] %s，识图保持原样", self.note)
            return False

        old = self.chain
        self.chain = ChainProvider(links, self.context.get_config() or {})
        if old is not None:
            # 战绩接着数，重装不该把统计清零（否则 /识图状态 会骗人）
            for pid, s in old.stat.items():
                if pid in self.chain.stat:
                    self.chain.stat[pid] = s
        pm.inst_map[ALIAS] = self.chain
        names = " → ".join(
            "%s(%s)" % (lk.provider_config.get("id"), lk.get_model()) for lk in links
        )
        self.note = names
        if not quiet:
            logger.info(
                "[vischain] 已接管识图 %s：%s%s（每档重试%d次，单次超时%.0fs）",
                ALIAS, names,
                "，缺失: " + ",".join(missing) if missing else "",
                RETRY, ATTEMPT_TIMEOUT,
            )
        return True

    @filter.on_astrbot_loaded()
    async def install(self):
        """在所有 provider 都实例化完之后再替换，否则拿不到真实实例。"""
        if not ENABLED:
            self.note = "总开关关闭"
            logger.info("[vischain] 总开关关闭，不接管识图")
            return
        try:
            pm = self.context.provider_manager
            self._ensure(pm)
            if self.chain is not None and WATCH_INTERVAL > 0:
                self._watch = asyncio.create_task(self._watchdog(pm))
        except Exception as e:  # noqa: BLE001
            self.note = "接管失败: %s" % e
            logger.error("[vischain] 接管失败，识图保持原样: %s", e)

    async def _watchdog(self, pm):
        """定期核对顶替是否还在。WebUI 改配置会把它冲掉，且不报错、不留痕。"""
        while True:
            try:
                await asyncio.sleep(WATCH_INTERVAL)
                if self._ensure(pm, quiet=True):
                    self._reinstalls += 1
                    logger.info(
                        "[vischain] 检测到 provider 被重载，已重新接管（第%d次）：%s",
                        self._reinstalls, self.note,
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("[vischain] 看护异常: %s", e)

    async def terminate(self):
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None

    @filter.command("识图状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/识图状态 —— 看识图链条与各档真实战绩。"""
        if self.chain is None:
            yield event.plain_result("识图链条没生效：%s" % self.note)
            return
        lines = ["识图顺序：" + self.note]
        if self._reinstalls:
            lines.append("（配置被改动后自动重新接管 %d 次）" % self._reinstalls)
        for i, lk in enumerate(self.chain.links, 1):
            pid = lk.provider_config.get("id", "?")
            s = self.chain.stat.get(pid, {})
            ok, fail = s.get("ok", 0), s.get("fail", 0)
            avg = (s.get("sec", 0.0) / ok) if ok else 0.0
            t = _dead.get(pid, 0.0)
            left = (DEAD_TTL - (time.time() - t)) if t else 0.0
            lines.append(
                "%d. %s 成功%d/失败%d%s%s"
                % (i, pid, ok, fail, ("，平均%.1fs" % avg) if ok else "",
                   ("，判定无通道还剩%.0f分钟" % (left / 60.0)) if left > 0 else "")
            )
        yield event.plain_result("\n".join(lines))
