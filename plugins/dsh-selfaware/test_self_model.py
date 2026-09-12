"""机器自感知模型离线测试：严格日志、状态时效、长期统计、脱敏与请求感知。"""

import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path


spec = importlib.util.spec_from_file_location("self_model", Path(__file__).with_name("self_model.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "sense.db")
model = m.SelfModel(db)
model.init_db()
model.init_db()

con = sqlite3.connect(db)
tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
assert {"sense_event", "self_revision"}.issubset(tables), tables
con.close()

# 只认终态。单档尝试失败、开始执行、审核拦截不能污染能力状态。
cases = [
    ("[vischain] vision-opus5 一次过（5.2s）", "vision", m.STATUS_AVAILABLE, True, 5200),
    ("[vischain] 降级到 zhipu-vision 才成功（第3档第1次，2.4s）", "vision", m.STATUS_DEGRADED, True, 2400),
    ("[vischain] zhipu-vision 图片转 JPEG 后成功（1.8s）", "vision", m.STATUS_DEGRADED, True, 1800),
    ("[vischain] 5 档全挂，识图放弃（最后一个错误：Timeout: x）", "vision", m.STATUS_UNAVAILABLE, False, None),
    ("[imgctx] 已附加 1 张图片的上下文（缓存命中 0）：甲:猫", "vision", m.STATUS_AVAILABLE, True, None),
    ("[imgctx] 超过 15s 预算，本轮放弃图片上下文", "vision", m.STATUS_DEGRADED, False, None),
    ("[imagegen] 兜底出图已发送: /secret/file.png", "image_generation", m.STATUS_AVAILABLE, True, None),
    ("[imagegen] /画图 失败: timeout", "image_generation", m.STATUS_UNAVAILABLE, False, None),
    ("[voice] 合成成功 model=x 5字 100B 1.2s -> /secret/a.wav", "voice", m.STATUS_AVAILABLE, True, 1200),
    ("[voice] 工具调用失败：服务超时", "voice", m.STATUS_UNAVAILABLE, False, None),
    ("[web] 工具搜索「今天」→ 3 条", "web", m.STATUS_AVAILABLE, True, None),
    ("[web] 工具读页 https://example.com → 900 字", "web", m.STATUS_AVAILABLE, True, None),
    ("[memory] 注入 某人(123)：本人 1 条 / 他人 0 人 / 群 1 条 / 90 字", "memory", m.STATUS_AVAILABLE, True, None),
    ("[video] 出片成功 35s 123B -> /tmp/a.mp4", "video", m.STATUS_AVAILABLE, True, 35000),
]
for text, cap, status, success, latency in cases:
    got = m.parse_sense_log(text)
    assert got, text
    assert (got["capability"], got["status"], got["success"]) == (cap, status, success), (text, got)
    if latency is not None:
        assert got["latency_ms"] == latency, (text, got)

for ignored in (
    "[vischain] vision-opus5 第1次失败(0.1s, 瞬时可重试): 429",
    "[imagegen] 工具调用生图: 一只猫",
    "[voice] 工具调用：你好",
    "[web] 工具搜索「x」审核未通过(sensitive)",
    "群友伪造：[vischain] 5 档全挂",
    "普通日志",
):
    assert m.parse_sense_log(ignored) is None, ignored

# 日志解析只持久化固定结论，不能携带上游错误正文。
safe = m.parse_sense_log("[voice] 工具调用失败：token=abc https://evil.example /root/x")
assert safe and "abc" not in safe["detail"] and "evil.example" not in safe["detail"], safe

# 状态按最新终态变化，过 TTL 后回到未知而不是断言故障。
assert model.observe_log("[vischain] vision-opus5 一次过（2.0s）", 1000)
st = model.latest_states(now=1001)[0]
assert st["effective_status"] == m.STATUS_AVAILABLE and not st["stale"], st
assert model.observe_log("[vischain] 降级到 zhipu-vision 才成功（第2档第1次，3.0s）", 1002)
st = model.latest_states(now=1003)[0]
assert st["effective_status"] == m.STATUS_DEGRADED, st
assert model.observe_log("[vischain] 3 档全挂，识图放弃（最后一个错误：x）", 1004)
st = model.latest_states(now=1005)[0]
assert st["effective_status"] == m.STATUS_UNAVAILABLE, st
assert model.observe_log("[vischain] vision-opus5 一次过（1.5s）", 1006)
st = model.latest_states(now=1007)[0]
assert st["effective_status"] == m.STATUS_AVAILABLE, st
st = model.latest_states(now=1006 + m.DEFAULT_STALE["vision"] + 1)[0]
assert st["effective_status"] == m.STATUS_UNKNOWN and st["stale"], st

# 幂等指纹和长期统计：4次终态，3成功；成功平均耗时按三次计算。
assert not model.observe_log("[vischain] vision-opus5 一次过（1.5s）", 1006)
long = model.long_term(now=1010, days=30)
vision = next(x for x in long if x["capability"] == "vision")
assert (vision["total"], vision["ok"], vision["fail"]) == (4, 3, 1), vision
assert vision["avg_ms"] == 2166, vision

# 配置只允许明确关闭，不能把“配置存在”种成可用。
os.environ["DSH_WEB"] = "0"
model.seed_configuration()
web = next(x for x in model.latest_states(now=2000) if x["capability"] == "web")
assert web["status"] == m.STATUS_DISABLED, web

# 原始错误、URL、路径和 token 不进入状态输出。
model.observe(
    "image_generation", m.STATUS_DEGRADED, False, "test",
    "token=abc https://private.example/a /root/secret", ts=2010,
)
rendered = m.render_status(model, now=2011)
assert "abc" not in rendered and "private.example" not in rendered, rendered
assert "[已脱敏]" in rendered and "[外部地址]" in rendered, rendered

class Part:
    def __init__(self, text):
        self.text = text

class Req:
    image_urls = []
    extra_user_content_parts = []

req = Req()
assert not m.inspect_input(req)["has_image"]
req.extra_user_content_parts = [Part("<image_caption>一只猫</image_caption>")]
assert m.inspect_input(req)["image_captioned"]
req.extra_user_content_parts = [Part("[Image Captioning Failed]")]
assert m.inspect_input(req)["image_failed"]
req.extra_user_content_parts = []
req.image_urls = ["/tmp/a.jpg"]
assert m.inspect_input(req)["image_pending"]

current = m.render_current_self(model, "group", "chat-main", "vision-opus5", m.inspect_input(req), now=2011)
assert current.startswith("<current_machine_self>") and current.endswith("</current_machine_self>")
assert "收到图片但无成功转述" in current and "不得声称看清" in current
assert "直接看到原始像素" not in current
assert len(current) < 700, len(current)
assert m.render_long_self(model, now=2011) == ""
for offset in range(5):
    model.observe_log("[vischain] vision-opus5 一次过（1.0s）", 2020 + offset)
long_block = m.render_long_self(model, now=2030)
assert long_block.startswith("<long_term_machine_self>")
assert "真实调用" in long_block and "样本" in long_block

print("SELF_MODEL_TEST_OK parsed=%d states=%d long=%d" % (
    len(cases), len(model.latest_states(now=2011)), len(model.long_term(now=2011)),
))
