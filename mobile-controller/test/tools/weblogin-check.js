// 用真 JS 引擎验证「网页自动登录」——单测只能证明地址拼对了，
// 不能证明 NapCat 的登录页**真的会**因为这个地址而自动登录。
//
// 为什么值得单独跑：这段逻辑在 NapCat 自己的前端里（我们改不了、也不该改），
// 我们只是依赖它的行为。一旦它变了（升级 NapCat），App 会静默退回
// 「请输入token」—— 用户看到的是一个不知道填什么的输入框，
// 从现象根本推不出「是 NapCat 改了前端」。
//
// 所以这里把 NapCat 登录页的关键代码**逐字照抄**下来，喂给它 App 真正
// 生成的地址，断言：
//   * 地址带 token → 登录页自动提交（用户不用输任何东西）；
//   * 地址不带 token → 不自动提交（复现用户报的那个界面）。
//
// 代码出处（线上 NapCat 的 webui 前端 chunk，已核对）：
//   web_login-*.js:
//     const j=new URLSearchParams(window.location.search).get("token")
//     useEffect(()=>{if(j){C(!1),m();return}E().finally(()=>{C(!1)})},[])
//     m=async()=>{if(!i){g.error("请输入token");return} ... loginWithToken(i) ...}
//
// 用法：node weblogin-check.js "<App 生成的地址>"
// 需要 node；没有就跳过（不让缺工具卡住整个测试）。

const url = process.argv[2];
if (!url) {
  console.error("用法：node weblogin-check.js <地址>");
  process.exit(2);
}

let failures = 0;
function ok(cond, label) {
  console.log((cond ? "  \u2713 " : "  \u2717 ") + label);
  if (!cond) failures++;
}

// ── 照抄 NapCat 登录页的逻辑，只补上它依赖的外部东西 ──────────────────
//
// 参数 search 就是地址里的查询串。返回「有没有自动提交」等观察结果。
function napcatLoginPage(search) {
  let submitted = 0;        // 调了几次 loginWithToken
  let submittedWith = null; // 用的什么 token
  let errorShown = null;    // g.error(...) 的内容
  let passkeyTried = 0;     // 有没有走 passkey 分支

  // ① 取 token（逐字）
  const j = new URLSearchParams(search).get("token");
  // 输入框初值：useState(j||"")（逐字）
  const i = j || "";

  // 它依赖的外部函数
  const C = () => {};                                  // setState
  const E = () => { passkeyTried++; return Promise.resolve(); };
  const g = { error: (m) => { errorShown = m; } };
  const f = { loginWithToken: async (t) => { submitted++; submittedWith = t; return { Credential: "CRED" }; } };
  const x = () => {};                                  // navigate
  const h = () => {};                                  // 存 Credential

  // ② 提交函数 m（逐字）
  const m = async () => {
    if (!i) { g.error("请输入token"); return; }
    C(true);
    try {
      const t = await f.loginWithToken(i);
      if (t) {
        if (t.require2FA) { return; }
        t.Credential && h(t.Credential);
        x("/qq_login", { replace: true });
      }
    } catch (e) { g.error(e.message); }
  };

  // ③ 自动提交 effect（逐字；原代码 return undefined，这里包成 async 便于等待）
  const effect = async () => {
    if (j) { C(false), m(); return; }
    await E().finally(() => { C(false); });
  };

  return effect().then(() => ({ submitted, submittedWith, errorShown, passkeyTried }));
}

// ── 从 App 生成的地址里取出查询串 ─────────────────────────────────────
function queryOf(u) {
  const q = u.indexOf("?");
  return q < 0 ? "" : u.substring(q);
}

(async () => {
  const q = queryOf(url);
  const r = await napcatLoginPage(q);

  // ★ 核心断言：App 生成的地址必须让登录页自动提交
  ok(r.submitted === 1,
      "★ 带 token 的地址会让登录页自动提交（用户不用输任何东西）");
  ok(r.errorShown === null,
      "★ 没有弹出「请输入token」");
  ok(r.passkeyTried === 0,
      "★ 走了 token 分支（不是退化成 passkey 尝试）");

  // token 必须**原样**送达：编码错一个字符就会登录失败
  const expect = new URLSearchParams(q).get("token");
  ok(r.submittedWith === expect,
      "★ token 原样送达（编码正确，实际拿到 " +
      (r.submittedWith === null ? "null" : r.submittedWith.length + " 字符") + "）");

  // ── 反面：不带 token 时必须复现用户看到的界面 ──────────────────────
  // 这条是「测试真的在测东西」的证据：如果连不带 token 都自动提交，
  // 说明上面的断言是假通过（那段逻辑根本没按预期跑）。
  const bare = await napcatLoginPage("");
  ok(bare.submitted === 0,
      "★ 不带 token 时不自动提交（复现用户报的「请输入token」界面）");
  ok(bare.passkeyTried === 1,
      "不带 token 时退化成 passkey 尝试（与实测一致）");

  console.log();
  if (failures === 0) {
    console.log("网页自动登录验证通过（" + url.replace(/token=[^&]*/, "token=<口令>") + "）");
  } else {
    console.log("网页自动登录验证失败：" + failures + " 项");
  }
  process.exit(failures === 0 ? 0 : 1);
})();
