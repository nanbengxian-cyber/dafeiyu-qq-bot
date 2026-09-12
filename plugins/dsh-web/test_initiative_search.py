"""dsh-web 主动话题搜索联动的静态/契约测试。"""

from pathlib import Path

src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")

assert "async def attach_initiative_search" in src
assert '@filter.on_llm_request(priority=1700)' in src
assert 'event.get_extra("dsh_initiate")' in src
assert 'event.get_extra("dsh_initiate_search_done")' in src
assert 'event.set_extra("dsh_initiate_search_done", True)' in src
assert 'event.set_extra("dsh_initiate_search_ok", True)' in src
assert "_drop_sensitive(res)" in src
assert "await _moderate(" in src
assert "退回本地候选" in src
assert "不要念标题列表、不要贴网址" in src
assert "没有日期就别说‘刚刚/今天/最近’" in src
assert "<initiative_search_context>" in src
# 主动搜索失败只能回退，不能 stop_event 或生成公开错误回复。
body = src.split("async def attach_initiative_search", 1)[1].split("# ------------------------------------------------ 触发 1", 1)[0]
assert "stop_event" not in body
assert "plain_result" not in body
print("INITIATE_WEB_SEARCH_TEST_OK")
