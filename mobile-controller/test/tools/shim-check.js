#!/usr/bin/env node
/*
 * 用真 JS 引擎验证注入脚本（由 run-shim-test.sh 调用）。
 *
 * 为什么不能只靠 Java 单测：单测只能断言「脚本字符串长什么样」，
 * 证明不了它**真的能工作**。这段脚本的职责是改写页面 JS 发出的 API 调用，
 * 它坏了的表现是「页面能打开、但一登录就失败」（POST body 丢失）——
 * 看起来一切正常，是最难查的一类问题。
 *
 * 这里造一个最小的假浏览器环境（XMLHttpRequest / fetch / Request /
 * EventSource），实际执行脚本，然后断言改写行为。
 */
'use strict';
const fs = require('fs');

const shimPath = process.argv[2];
const shim = fs.readFileSync(shimPath, 'utf8');

let pass = 0;
const fails = [];
function check(name, cond) {
    if (cond) {
        pass++;
        console.log('  \u001b[32m✓\u001b[0m ' + name);
    } else {
        fails.push(name);
        console.log('  \u001b[31m✗\u001b[0m ' + name);
    }
}
function eq(name, want, got) {
    check(name + (want === got ? '' : '（想要 ' + want + '，得到 ' + got + '）'), want === got);
}

/*
 * 浏览器会把传给 open()/fetch() 的地址**按页面 origin 解析**成绝对地址。
 * 脚本有时返回相对路径（比如已经是代理地址时原样返回），
 * 所以断言要比对「解析后」的结果，而不是原始字符串 ——
 * 否则会误判一个其实完全正确的实现。
 * 页面 origin 就是代理 origin（我们加载 http://127.0.0.1:41000/proxy/qq1/webui/）。
 */
const PAGE_ORIGIN = 'http://127.0.0.1:41000';
function resolve(u) {
    if (typeof u !== 'string' || u.length === 0) { return u; }
    if (u.indexOf('://') >= 0) { return u; }
    if (u.charAt(0) === '/') { return PAGE_ORIGIN + u; }
    return PAGE_ORIGIN + '/proxy/qq1/webui/' + u;
}

// ---------------- 假浏览器环境 ----------------
const xhrCalls = [];
function XHR() {}
XHR.prototype.open = function (m, u) { xhrCalls.push({ m: m, u: u }); };
global.XMLHttpRequest = XHR;

global.window = global;

const fetchCalls = [];
global.fetch = function (a, b) { fetchCalls.push({ a: a, b: b }); return Promise.resolve(); };
global.Request = function (url, init) { this.url = url; this.init = init; };

const esCalls = [];
function ES(u, c) { esCalls.push(u); this.url = u; }
global.EventSource = ES;

// ---------------- 执行脚本 ----------------
eval(shim);

console.log('用真 JS 引擎验证注入脚本');
console.log('');

// ① axios 风格的登录 POST（NapCat 的登录就是这么发的）
const loginBody = JSON.stringify({ hash: 'abc123', totpCode: '654321' });
const x1 = new XMLHttpRequest();
x1.open('POST', '/api/auth/login');
eq('★ 登录 POST 的地址被改写',
    'http://127.0.0.1:41000/proxy/qq1/api/auth/login', resolve(xhrCalls[0].u));

// 关键：body 是页面自己发的，脚本只改 URL，不该碰它。
// （HTTP 层拦截做不到这一点 —— WebView 拿不到 POST body。）
check('★ 登录 body 没被脚本碰到（仍是页面自己那份）',
    loginBody === JSON.stringify({ hash: 'abc123', totpCode: '654321' }));

// ② fetch 带 body
global.fetch('/api/base/GetSysStatusRealTime', { method: 'POST', body: 'payload' });
eq('fetch 的地址被改写',
    'http://127.0.0.1:41000/proxy/qq1/api/base/GetSysStatusRealTime',
    resolve(fetchCalls[0].a));
eq('★ fetch 的 body 原样保留', 'payload', fetchCalls[0].b.body);
eq('★ fetch 的 method 原样保留', 'POST', fetchCalls[0].b.method);

// ③ 绝对同源地址（WebView 里页面若用绝对地址）
const x2 = new XMLHttpRequest();
x2.open('GET', 'http://127.0.0.1:41000/webui/assets/a.js');
eq('绝对同源地址被改写',
    'http://127.0.0.1:41000/proxy/qq1/webui/assets/a.js', resolve(xhrCalls[1].u));

// ④ 外部地址**不能**被改写（否则会把外链也代理了）
const x3 = new XMLHttpRequest();
x3.open('GET', 'https://cdn.example.com/lib.js');
eq('★ 外部地址不被改写', 'https://cdn.example.com/lib.js', resolve(xhrCalls[2].u));

// ⑤ 已经是代理地址 → 不能重复套前缀
const x4 = new XMLHttpRequest();
x4.open('GET', 'http://127.0.0.1:41000/proxy/qq1/api/x');
eq('★ 已是代理地址不重复套（解析后仍是同一个地址）',
    'http://127.0.0.1:41000/proxy/qq1/api/x', resolve(xhrCalls[3].u));

// ⑥ EventSource（NapCat 用它拉实时日志）
new EventSource('/api/Log/GetLogRealTime');
eq('★ EventSource 被改写',
    'http://127.0.0.1:41000/proxy/qq1/api/Log/GetLogRealTime', resolve(esCalls[0]));

// ⑦ 边界：不改非字符串参数、不改相对路径、不抛异常
const x5 = new XMLHttpRequest();
x5.open('GET', 'foo/bar.js');
eq('相对路径（非 / 开头）不动',
    'http://127.0.0.1:41000/proxy/qq1/webui/foo/bar.js', resolve(xhrCalls[4].u));

let threw = false;
try {
    global.fetch(undefined, undefined);
    const x6 = new XMLHttpRequest();
    x6.open('GET', '');
} catch (e) {
    threw = true;
}
check('★ 传 undefined/空串时不抛异常（页面不会因此白屏）', !threw);

console.log('');
if (fails.length) {
    console.log('未通过的项：');
    fails.forEach(function (f) { console.log('  · ' + f); });
    console.log('通过 ' + pass + '，失败 ' + fails.length);
    process.exit(1);
}
console.log('通过 ' + pass + '，失败 0');
console.log('注入脚本在真 JS 引擎里工作正常 ✓');
