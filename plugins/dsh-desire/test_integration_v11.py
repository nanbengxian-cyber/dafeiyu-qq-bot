# -*- coding: utf-8 -*-
"""旧库迁移、selfaware 游标、保护性感知与幂等集成测试（容器内运行）。"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

root = Path(__file__).parent
pkg = types.ModuleType("desire_pkg")
pkg.__path__ = [str(root)]
sys.modules["desire_pkg"] = pkg

# 最小 AstrBot stub，只加载 Store/Bridge，不启动框架。
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.star = types.SimpleNamespace(Star=object, Context=object)
api_event = types.ModuleType("astrbot.api.event")
api_event.AstrMessageEvent = object
class DummyFilter:
    PlatformAdapterType = types.SimpleNamespace(ALL="all")
    @staticmethod
    def platform_adapter_type(*a, **k): return lambda f: f
    @staticmethod
    def on_llm_request(*a, **k): return lambda f: f
    @staticmethod
    def command(*a, **k): return lambda f: f
api_event.filter = DummyFilter
core = types.ModuleType("astrbot.core")
core.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
agent_message = types.ModuleType("astrbot.core.agent.message")
agent_message.TextPart = type("TextPart", (), {"__init__": lambda self, text="": setattr(self, "text", text)})
message_type = types.ModuleType("astrbot.core.platform.message_type")
message_type.MessageType = types.SimpleNamespace(GROUP_MESSAGE="group")
for name, mod in {
    "astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": api_event,
    "astrbot.core": core, "astrbot.core.agent": types.ModuleType("astrbot.core.agent"),
    "astrbot.core.agent.message": agent_message,
    "astrbot.core.platform": types.ModuleType("astrbot.core.platform"),
    "astrbot.core.platform.message_type": message_type,
}.items(): sys.modules[name] = mod

with tempfile.TemporaryDirectory() as td:
    desire = Path(td) / "desire.db"
    aware = Path(td) / "selfaware.db"
    # 构造 1.0 旧库，验证升级不丢旧状态。
    con = sqlite3.connect(desire)
    con.executescript("""
    CREATE TABLE schema_version(version INTEGER NOT NULL); INSERT INTO schema_version VALUES(1);
    CREATE TABLE drive_state(group_id TEXT NOT NULL,drive TEXT NOT NULL,intensity REAL NOT NULL,
      baseline REAL NOT NULL,phase TEXT NOT NULL,updated_at REAL NOT NULL,active_since REAL NOT NULL DEFAULT 0,
      cooldown_until REAL NOT NULL DEFAULT 0,expressions_today INTEGER NOT NULL DEFAULT 0,
      expression_day TEXT NOT NULL DEFAULT '',last_signal TEXT NOT NULL DEFAULT '',PRIMARY KEY(group_id,drive));
    CREATE TABLE signal_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT NOT NULL,group_id TEXT NOT NULL,
      drive TEXT NOT NULL,delta REAL NOT NULL,kind TEXT NOT NULL,created_at REAL NOT NULL,UNIQUE(event_key,drive));
    INSERT INTO drive_state VALUES('g','play',44,16,'latent',1000,0,0,0,'','old');
    """)
    con.commit(); con.close()
    os.environ["DSH_DESIRE_DB"] = str(desire)
    os.environ["DSH_DESIRE_SELFAWARE_DB"] = str(aware)
    spec = importlib.util.spec_from_file_location("desire_pkg.main", root / "main.py")
    main = importlib.util.module_from_spec(spec); sys.modules[spec.name] = main; spec.loader.exec_module(main)
    store = main.DesireStore(desire)
    con = sqlite3.connect(desire)
    assert con.execute("select version from schema_version").fetchone()[0] == 3
    assert con.execute("select intensity from drive_state where group_id='g' and drive='play'").fetchone()[0] == 44
    assert con.execute("select name from sqlite_master where name='bridge_cursor'").fetchone()
    con.close()

    # 构造 revision 流：首次桥接跳过历史，新转变只消费一次，重启也不重放。
    con = sqlite3.connect(aware)
    con.execute("CREATE TABLE self_revision(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL NOT NULL,layer TEXT NOT NULL,subject TEXT NOT NULL,old_value TEXT NOT NULL DEFAULT '',new_value TEXT NOT NULL,reason TEXT NOT NULL)")
    now = time.time()
    con.execute("INSERT INTO self_revision(ts,layer,subject,old_value,new_value,reason) VALUES(?,?,?,?,?,?)", (now-30,'short','vision','available','degraded','old'))
    con.commit(); con.close()
    bridge = main.SelfAwareBridge(aware, store)
    assert bridge.changes(now) == []
    con = sqlite3.connect(aware)
    con.execute("INSERT INTO self_revision(ts,layer,subject,old_value,new_value,reason) VALUES(?,?,?,?,?,?)", (now,'short','vision','degraded','unavailable','new'))
    con.commit(); con.close()
    changes = bridge.changes(now+1)
    assert len(changes) == 1
    signal_set = changes[0][1]
    assert {x.drive for x in signal_set} == {'competence', 'nociception', 'fear'}
    assert all(x.delta > 0 for x in signal_set)
    assert bridge.changes(now+2) == []
    bridge2 = main.SelfAwareBridge(aware, store)
    assert bridge2.changes(now+3) == []

    # 恢复降低状态；过期转变不增强。
    con = sqlite3.connect(aware)
    con.execute("INSERT INTO self_revision(ts,layer,subject,old_value,new_value,reason) VALUES(?,?,?,?,?,?)", (now+4,'short','vision','unavailable','available','recovered'))
    con.execute("INSERT INTO self_revision(ts,layer,subject,old_value,new_value,reason) VALUES(?,?,?,?,?,?)", (now-9999,'short','web','available','unavailable','stale'))
    con.commit(); con.close()
    changes = bridge2.changes(now+5)
    assert len(changes) == 1 and {x.drive for x in changes[0][1]} == {'competence', 'nociception', 'fear'}, changes
    assert all(x.delta < 0 for x in changes[0][1]), changes

    # Store 事件幂等与群隔离。
    state = store.apply('g', 'event:1', 'continuity', 15, 'existence_threat', now)
    assert state and store.apply('g', 'event:1', 'continuity', 15, 'existence_threat', now) is None
    assert store.load_group('g2', now)[0]['intensity'] == main.BASELINES['continuity']
    print('DESIRE_V11_INTEGRATION_OK')
