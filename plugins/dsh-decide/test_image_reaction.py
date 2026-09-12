"""纯图片自然反应逻辑回归测试。"""

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "image_reaction_logic", HERE / "image_reaction_logic.py"
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class Part:
    def __init__(self, text):
        self.text = text


assert m.meaningful_image_text("[图片]") == ""
assert m.meaningful_image_text("[动画表情]") == ""
assert m.meaningful_image_text("[CQ:image,file=x.jpg]") == ""
assert m.meaningful_image_text("[图片] 你看这个怎么样？") == "你看这个怎么样？"
assert m.meaningful_image_text("配这个图") == "配这个图"
assert m.is_image_only(True, "[图片]")
assert m.is_image_only(True, "[动画表情]")
assert not m.is_image_only(False, "[图片]")
assert not m.is_image_only(True, "[图片] 这是谁？")

parts = [
    Part("<image_caption> 一只猫趴在电脑上 </image_caption>"),
    Part("<recent_image_context>旧图内容</recent_image_context>"),
]
assert m.current_image_caption(parts) == "一只猫趴在电脑上"
assert m.current_image_caption([Part("<recent_image_context>旧图</recent_image_context>")]) == ""

rates = {"reply": .12, "about": .30, "open": .58, "banter": .78, "none": .92}
assert m.image_dive_rate({"replying_to_bot": True, "ack": False}, rates) == .12
assert m.image_dive_rate({"about_bot": True}, rates) == .30
assert m.image_dive_rate({"open": True}, rates) == .58
assert m.image_dive_rate({"banter": True}, rates) == .78
assert m.image_dive_rate({}, rates) == .92
assert m.image_dive_rate({"open": True}, {"open": 5}) == 1.0
assert m.image_dive_rate({"open": True}, {"open": -1}) == 0.0

src = (HERE / "main.py").read_text(encoding="utf-8")
assert 'event.set_extra("dsh_image_only", pure_image)' in src
assert 'current_image_caption(' in src
assert 'if pure_image and IMAGE_DIVE and act == "回话":' in src
assert "逐项点评或长篇分析" in src
assert "图片本身你看不了" not in src  # 不在决策层伪造视觉状态

print("IMAGE_REACTION_TEST_OK")
