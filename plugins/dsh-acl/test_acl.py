# test_acl.py —— dsh-acl 纯逻辑测试（不依赖 astrbot，容器内可直接跑）
#
# 覆盖：
#   A 指令匹配（与框架 CommandFilter 同构，含前缀陷阱）
#   B 分档
#   C 授权判定（QQ 号 + 群内身份）
#   D 真实场景回归（本群 owner／admin／普通成员各发一遍）
#   E env 覆盖语义
#
# 为什么要单独测匹配：门卫的匹配规则必须和框架逐字一致，否则会出现
# 「门卫认为不是指令 → 放行 → 框架认为是指令 → 越权执行」这种漏放。

import re
import sys

fails = []


def check(n, got, want):
    if got == want:
        print(f"  PASS {n}")
    else:
        print(f"  FAIL {n}\n       got={got!r}\n       want={want!r}")
        fails.append(n)


# ---------------------------------------------------------------- 被测逻辑
# 与 main.py 保持同构。改 main.py 的这几个函数必须同步改这里。

OWNERS = {"2774000001"}

OWNER_CMDS = {
    "reset", "new", "stop", "set", "unset",
    "忘记群记忆", "记忆状态", "群记忆", "音色", "做视频", "欢迎测试",
}
ADMIN_CMDS = {
    "画图状态", "语音状态", "视频状态", "识图状态", "图片上下文", "联网状态",
    "贴纸状态", "说话风格", "艾特模式", "上下文状态", "插话判断", "禁言状态",
    "防骗状态", "戳一戳状态", "欢迎状态", "聊天记录状态", "stats",
}
_MANAGED = sorted(OWNER_CMDS | ADMIN_CMDS, key=len, reverse=True)


def norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def match_command(text, managed=None):
    t = norm(text)
    if not t:
        return ""
    for cmd in managed if managed is not None else _MANAGED:
        if t == cmd or t.startswith(cmd + " "):
            return cmd
    return ""


def level_of(cmd, owner_cmds=None, admin_cmds=None):
    oc = OWNER_CMDS if owner_cmds is None else owner_cmds
    ac = ADMIN_CMDS if admin_cmds is None else admin_cmds
    if cmd in oc:
        return "owner"
    if cmd in ac:
        return "admin"
    return "all"


def allowed(level, uid, role, owners=None):
    ow = OWNERS if owners is None else owners
    if level == "all":
        return True
    is_owner = uid in ow or role == "owner"
    if level == "owner":
        return is_owner
    if level == "admin":
        return is_owner or role == "admin"
    return True


# ---------------------------------------------------------------- A 匹配
print("A 指令匹配（与 CommandFilter 同构）")

# message_str 在 waking_check 里已经剥掉了 `/` 和第一个 @机器人，
# 所以门卫看到的永远是不带斜杠的裸指令名。
check("A1 裸指令完全匹配", match_command("记忆状态"), "记忆状态")
check("A2 带参数（空格分隔）", match_command("音色 派蒙"), "音色")
check("A3 多余空白被归一", match_command("  记忆状态  "), "记忆状态")
check("A4 中间多个空格", match_command("音色   派蒙"), "音色")

# ★ 最容易错的一条 ★
# `记住我` 和 `记住` 有前缀关系。如果匹配时不要求「指令名 + 空格」，
# `记住我` 会被 `记住` 抢走。这里 `记住`/`记住我` 都不是被管指令，
# 所以两条都该返回空 —— 但规则本身必须正确，用被管指令验证：
check("A5 前缀不算命中（记忆状态 vs 记忆状态吗）",
      match_command("记忆状态吗"), "")
check("A6 无空格的粘连不算命中", match_command("音色派蒙"), "")
check("A7 不是被管指令返回空", match_command("我的档案"), "")
check("A8 忘记我不被误判成忘记群记忆", match_command("忘记我"), "")
check("A9 空消息", match_command(""), "")
check("A10 纯空白", match_command("   "), "")

# 长度倒序保证长指令优先。构造一对有前缀关系的被管指令来验证。
m = sorted({"记住", "记住我"}, key=len, reverse=True)
check("A11 长指令优先（记住我 不被 记住 抢走）",
      match_command("记住我", m), "记住我")
check("A12 短指令带参数仍匹配短的",
      match_command("记住 我喜欢篮球", m), "记住")

# 英文指令
check("A13 英文指令", match_command("reset"), "reset")
check("A14 英文指令带参数", match_command("set k v"), "set")
check("A15 resetxxx 不算 reset", match_command("resetxxx"), "")

# ---------------------------------------------------------------- B 分档
print("\nB 分档")
check("B1 reset 是 owner 级", level_of("reset"), "owner")
check("B2 记忆状态 是 owner 级", level_of("记忆状态"), "owner")
check("B3 做视频 是 owner 级", level_of("做视频"), "owner")
check("B4 禁言状态 是 admin 级", level_of("禁言状态"), "admin")
check("B5 stats 是 admin 级", level_of("stats"), "admin")
check("B6 忘记我 不受管（自己的数据）", level_of("忘记我"), "all")
check("B7 我的档案 不受管", level_of("我的档案"), "all")
check("B8 画图 不受管（有自然语言路径，管不住）", level_of("画图"), "all")
check("B9 help 不受管", level_of("help"), "all")

# ---------------------------------------------------------------- C 授权
print("\nC 授权判定（QQ 号 + 群内身份）")

OWNER_UID = "2774000001"      # 群主，在 OWNERS 里
ADMIN_UID = "3311610000"      # 群管理，不在 OWNERS 里
MEMBER_UID = "3590729000"     # 普通成员
BOT_UID = "100000002"        # 机器人自己也是 admin

check("C1 群主用 owner 级", allowed("owner", OWNER_UID, "owner"), True)
check("C2 群管理用 owner 级 → 不行", allowed("owner", ADMIN_UID, "admin"), False)
check("C3 普通成员用 owner 级 → 不行", allowed("owner", MEMBER_UID, "member"), False)
check("C4 群主用 admin 级", allowed("admin", OWNER_UID, "owner"), True)
check("C5 群管理用 admin 级", allowed("admin", ADMIN_UID, "admin"), True)
check("C6 普通成员用 admin 级 → 不行", allowed("admin", MEMBER_UID, "member"), False)
check("C7 all 级人人可用", allowed("all", MEMBER_UID, "member"), True)

# ★ 身份凭证是 QQ 号，不是昵称 ★
# 群里有重名（多个带「喵」的、多个单字名），群名片还能随时改。
# 所以只要 uid 在 OWNERS 里就放行，哪怕 role 读成了 member。
check("C8 QQ 号在 OWNERS 里，role 读不到也放行",
      allowed("owner", OWNER_UID, "member"), True)
# 反向：role 是 owner 但 uid 不在表里（换群部署忘改 env）也放行，
# 免得把真群主锁在门外。
check("C9 role==owner 但不在 OWNERS 里也放行",
      allowed("owner", "9999999", "owner"), True)
# 冒名顶替防线：昵称一样但 QQ 号不同 → 拦住。
check("C10 同名不同号：不放行",
      allowed("owner", "1234567", "member"), False)

# role 取不到时按 member（宁可判严）
check("C11 role 为空按 member 处理", allowed("admin", MEMBER_UID, ""), False)
check("C12 role 是垃圾值按 member 处理",
      allowed("owner", MEMBER_UID, "garbage"), False)

# 机器人自己在本群是 admin，但它不该被门卫处理（main.py 里 uid==me 直接 return）。
check("C13 机器人的 admin 身份能过 admin 级（但实际不会走到这）",
      allowed("admin", BOT_UID, "admin"), True)

# ---------------------------------------------------------------- D 场景回归
print("\nD 真实场景（一条消息走完整条链）")


def gate(text, uid, role, at_or_wake=True):
    """模拟门卫：返回 'pass' / 'deny' / 'ignore'。"""
    if not at_or_wake:
        return "ignore"          # 没 @ 也没带前缀，指令本来不会触发
    cmd = match_command(text)
    if not cmd:
        return "ignore"          # 不是被管指令
    return "pass" if allowed(level_of(cmd), uid, role) else "deny"


check("D1 群主 /reset", gate("reset", OWNER_UID, "owner"), "pass")
check("D2 成员 /reset → 拦", gate("reset", MEMBER_UID, "member"), "deny")
check("D3 群管理 /reset → 拦（清的是全群历史）",
      gate("reset", ADMIN_UID, "admin"), "deny")
check("D4 群管理 /禁言状态", gate("禁言状态", ADMIN_UID, "admin"), "pass")
check("D5 成员 /禁言状态 → 拦", gate("禁言状态", MEMBER_UID, "member"), "deny")
# /忘记我 刻意不进被管表，所以门卫的答复是「不管」而不是「放行」——
# 对用户效果一样（指令照常执行），但这里要断言真实语义：
# 只有被管指令才会走到授权判定。
check("D6 成员 /忘记我 → 门卫不管（自己的退出权，刻意不设限）",
      gate("忘记我", MEMBER_UID, "member"), "ignore")
check("D7 成员 /我的档案 → 门卫不管",
      gate("我的档案", MEMBER_UID, "member"), "ignore")
check("D8 成员 /画图 猫 → 门卫不管",
      gate("画图 猫", MEMBER_UID, "member"), "ignore")

# ★ 不能误伤闲聊 ★
# 群友闲聊里说「记忆状态」（没带斜杠、没 @），指令本来不触发，
# 门卫也必须放过，否则凭空回一句「只有群主能用」。
check("D9 闲聊里提到指令名 → 门卫不管",
      gate("记忆状态", MEMBER_UID, "member", at_or_wake=False), "ignore")
check("D10 成员 /音色 派蒙 → 拦",
      gate("音色 派蒙", MEMBER_UID, "member"), "deny")
check("D11 群主 /音色 派蒙", gate("音色 派蒙", OWNER_UID, "owner"), "pass")
check("D12 成员 /做视频 → 拦",
      gate("做视频 鲸鱼翻身", MEMBER_UID, "member"), "deny")

# ---------------------------------------------------------------- E env 覆盖
print("\nE env 覆盖语义")

# DSH_ACL_ALL_CMDS 把默认限制放开：优先级最高。
oc2 = OWNER_CMDS - {"记忆状态"}
ac2 = ADMIN_CMDS - {"记忆状态"}
check("E1 放开后降为 all", level_of("记忆状态", oc2, ac2), "all")
check("E2 放开后成员可用", allowed(level_of("记忆状态", oc2, ac2), MEMBER_UID, "member"), True)

# DSH_ACL_OWNER_CMDS 追加：把一条 admin 级提到 owner 级。
oc3 = OWNER_CMDS | {"禁言状态"}
check("E3 追加后 禁言状态 变 owner 级", level_of("禁言状态", oc3, ADMIN_CMDS), "owner")
check("E4 追加后群管理用不了",
      allowed(level_of("禁言状态", oc3, ADMIN_CMDS), ADMIN_UID, "admin"), False)

# 多个 owner（比如群主 + 自己的小号）
check("E5 多 owner", allowed("owner", "111", "member", {"111", "222"}), True)
check("E6 多 owner 之外的人", allowed("owner", "333", "member", {"111", "222"}), False)

# OWNERS 为空时只认群内 role
check("E7 OWNERS 为空：role==owner 仍放行",
      allowed("owner", "111", "owner", set()), True)
check("E8 OWNERS 为空：普通成员不行",
      allowed("owner", "111", "member", set()), False)

# ---------------------------------------------------------------- 汇总
print()
if fails:
    print("FAILED %d: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL PASS")
