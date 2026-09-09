#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼机器人控制台（Windows EXE 源码）。

做三件事：
1. 在本地填写服务器与仓库配置；
2. 通过 SSH 让目标服务器从仓库下载源码、自动部署并启动机器人；
3. 部署完成后从服务器读回可调配置项，改完写回并让服务生效。

安全边界：
- 密码、私钥口令只留在内存，不写文件、不进日志。
- 只操作部署目录里的专用标记文件，不碰服务器上其他部署。
- 日志全部经过脱敏，远程输出不会原样落到界面或磁盘。
- 本程序不会连接或修改任何已有机器人部署，除非用户显式填入它的地址。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

import config_schema
import knobs as knobs_mod
from config_schema import DEPLOY_FIELDS, GROUP_ORDER
from deployer import (
    DeployConfig,
    DeployError,
    SSHDeployer,
    format_containers,
    format_environment,
)
from knobs import Knob, KnobError

APP_NAME = "大肥鱼机器人控制台"
PROFILE_NAME = "controller-profile.json"
PROFILE_KEYS = set(config_schema.public_defaults())

TUTORIAL = """欢迎使用大肥鱼机器人控制台。跟着下面六步走，不需要懂 Docker 命令。

【第 1 步】准备一台服务器
  · 一台 Linux 服务器（推荐 Debian 12 或 Ubuntu 22.04），内存 2 GB 以上。
  · 你能用 SSH 登录它（有地址、端口、用户名，以及密码或私钥）。
  · 服务器安全组放行：SSH 端口、NapCat 端口（默认 3001）、AstrBot 端口（默认 6185）。

【第 2 步】填写“连接与部署”
  · 服务器地址：公网 IP 或域名，不要带 http://。
  · 登录方式：密码或 SSH 私钥。密码只留在内存，不会被保存。
  · 首次连接这台服务器时，勾选“首次连接接受服务器指纹”。
  · 源码仓库地址：形如 https://<你的代码托管域名>/<账号>/<仓库>.git
    （私有仓库请在服务器上配置部署密钥，再填 git@<主机>:<账号>/<仓库>.git）

【第 3 步】点“测试连接”
  · 控制台会只读检查系统、Git、Docker、磁盘和权限，不修改服务器。

【第 4 步】点“开始部署”
  控制台会自动完成：下载源码 → 准备目录与配置 → 生成容器编排 → 拉取镜像 → 启动服务。
  部署目录默认 ~/dafeiyu-bot，只会管理带专用标记的目录，不会覆盖你已有的项目。

【第 5 步】到“配置与状态”页
  · 配置项由仓库里的 deploy/console-config.json 决定，部署后自动出现。
  · 改完点“应用配置”，需要重启的项会自动重建容器。
  · 顶部状态区显示两个容器是否在运行、端口映射是否正常。

【第 6 步】登录 QQ
  · 浏览器打开 http://<服务器地址>:<NapCat端口> 扫码登录机器人 QQ 号。
  · 再打开 http://<服务器地址>:<AstrBot端口> 完成模型与人格配置。

【常见问题】
  · “首次连接该服务器”：勾选接受指纹，或先用系统 ssh 确认指纹。
  · “当前账号无权访问 Docker”：用 root，或把用户加入 docker 组。
  · “部署目录已存在且不是本控制台创建的”：换一个部署目录，控制台不会删别人的目录。
  · “镜像拉取失败”：检查服务器能否访问容器镜像仓库，必要时给服务器配代理。
  · 端口被占用：把“服务端口”改成其他端口再部署。

【安全提醒】
  · 不要把真实服务器地址、密码、Token、私钥写进截图、日志或代码仓库。
  · 控制台不保存密码与私钥口令；私钥始终只在你自己的电脑上。
"""


def profile_path() -> Path:
    """配置放在程序旁边：EXE 放哪个文件夹都能用。"""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / PROFILE_NAME


def load_profile(path: Optional[Path] = None) -> Dict[str, Any]:
    """只读取允许持久化的字段；文件损坏时返回空配置而不是崩溃。"""
    path = path or profile_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        raise DeployError("本地配置无法读取，请删除 %s 后重新填写。" % path.name) from None
    if not isinstance(raw, dict):
        raise DeployError("本地配置格式不正确，请重新填写。")
    return {key: value for key, value in raw.items() if key in PROFILE_KEYS}


def save_profile(values: Dict[str, Any], path: Optional[Path] = None) -> None:
    """只写非敏感字段；密码、私钥路径、口令一律不落盘。"""
    path = path or profile_path()
    payload = {key: value for key, value in values.items() if key in PROFILE_KEYS}
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError as exc:
        raise DeployError("无法保存本地配置。") from exc


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1020x760")
        self.minsize(940, 660)

        self.values: Dict[str, Any] = dict(config_schema.public_defaults())
        try:
            self.values.update(load_profile())
        except DeployError as exc:
            self.after(50, lambda: messagebox.showwarning(APP_NAME, str(exc)))

        self.vars: Dict[str, Any] = {}
        self.field_rows: Dict[str, Any] = {}
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.busy = 0
        self.knobs: List[Knob] = []
        self.knob_by_key: Dict[str, Knob] = {}
        self.knob_vars: Dict[str, Any] = {}
        self.env_text = ""
        self._build()
        self._refresh_visibility()
        self.after(120, self._drain_log)

    # ------------------------------------------------------------ 界面

    def _build(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.deploy_tab = ttk.Frame(self.notebook)
        self.config_tab = ttk.Frame(self.notebook)
        self.tutorial_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.deploy_tab, text="① 连接与部署")
        self.notebook.add(self.config_tab, text="② 配置与状态")
        self.notebook.add(self.tutorial_tab, text="③ 新手教程")

        self._build_deploy_tab()
        self._build_config_tab()
        self._build_tutorial_tab()

    def _scroll_area(self, parent) -> ttk.Frame:
        canvas = tk.Canvas(parent, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return inner

    def _build_deploy_tab(self) -> None:
        top = ttk.Frame(self.deploy_tab)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(
            top,
            text="填写服务器和仓库信息，先“测试连接”，再“开始部署”。",
            foreground="#444",
        ).pack(side="left")
        self.btn_test = ttk.Button(top, text="测试连接", command=self.test_connection)
        self.btn_test.pack(side="right")
        self.btn_deploy = ttk.Button(top, text="开始部署", command=self.start_deploy)
        self.btn_deploy.pack(side="right", padx=6)

        body = ttk.Frame(self.deploy_tab)
        body.pack(fill="both", expand=True, padx=10)
        area = self._scroll_area(body)

        for group in GROUP_ORDER:
            fields = [field for field in DEPLOY_FIELDS if field.group == group]
            if not fields:
                continue
            frame = ttk.LabelFrame(area, text=group)
            frame.pack(fill="x", padx=2, pady=6)
            frame.columnconfigure(1, weight=1)
            for row, field in enumerate(fields):
                self._add_field(frame, row, field)

        self.status_var = tk.StringVar(value="就绪。")
        ttk.Label(self.deploy_tab, textvariable=self.status_var,
                  foreground="#333").pack(anchor="w", padx=14, pady=(4, 0))

        log_frame = ttk.LabelFrame(self.deploy_tab, text="部署日志（已脱敏）")
        log_frame.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        log_bar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_bar.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        log_bar.pack(side="right", fill="y", pady=6)

    def _add_field(self, parent: ttk.Frame, row: int, field) -> None:
        label = ttk.Label(parent, text=field.label)
        label.grid(row=row, column=0, sticky="w", padx=8, pady=5)

        current = self.values.get(field.key, field.default)
        if field.kind == "bool":
            var: Any = tk.BooleanVar(value=bool(current))
            widget: Any = ttk.Checkbutton(parent, variable=var)
        elif field.kind == "choice":
            var = tk.StringVar(value=str(current or field.default))
            widget = ttk.Combobox(parent, textvariable=var, values=list(field.choices),
                                  state="readonly", width=30)
        elif field.kind == "file":
            var = tk.StringVar(value=str(current or ""))
            holder = ttk.Frame(parent)
            ttk.Entry(holder, textvariable=var, width=30).pack(
                side="left", fill="x", expand=True)
            ttk.Button(holder, text="选择…",
                       command=lambda v=var: self._pick_file(v)).pack(
                side="left", padx=(6, 0))
            widget = holder
        else:
            var = tk.StringVar(value=str(current if current is not None else ""))
            widget = ttk.Entry(parent, textvariable=var, width=32,
                               show="*" if field.secret else "")
        widget.grid(row=row, column=1, sticky="ew", padx=8, pady=5)

        hint = ttk.Label(parent, text=field.hint, foreground="#666",
                         wraplength=360, justify="left")
        hint.grid(row=row, column=2, sticky="w", padx=8, pady=5)

        self.vars[field.key] = var
        self.field_rows[field.key] = (label, widget, hint, field)
        if field.key == "auth_type":
            var.trace_add("write", lambda *_a: self._refresh_visibility())

    def _pick_file(self, var: tk.StringVar) -> None:
        chosen = filedialog.askopenfilename(
            title="选择 SSH 私钥文件",
            filetypes=[("所有文件", "*.*")],
        )
        if chosen:
            var.set(chosen)

    def _refresh_visibility(self) -> None:
        for _key, (label, widget, hint, field) in self.field_rows.items():
            visible = True
            if field.visible_if:
                cond_key, cond_value = field.visible_if
                cond_var = self.vars.get(cond_key)
                visible = bool(cond_var) and str(cond_var.get()) == str(cond_value)
            for item in (label, widget, hint):
                if visible:
                    item.grid()
                else:
                    item.grid_remove()

    def _build_config_tab(self) -> None:
        top = ttk.Frame(self.config_tab)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="配置项由服务器上的仓库清单决定。",
                  foreground="#444").pack(side="left")
        self.btn_apply = ttk.Button(top, text="应用配置", command=self.apply_config)
        self.btn_apply.pack(side="right")
        self.btn_reload = ttk.Button(top, text="刷新配置", command=self.load_config)
        self.btn_reload.pack(side="right", padx=6)
        self.btn_status = ttk.Button(top, text="刷新状态", command=self.refresh_status)
        self.btn_status.pack(side="right")

        self.container_var = tk.StringVar(value="还没有读取运行状态。")
        ttk.Label(self.config_tab, textvariable=self.container_var,
                  foreground="#333", justify="left").pack(
            anchor="w", padx=14, pady=(2, 6))

        self.knob_area = self._scroll_area(self.config_tab)

    def _build_tutorial_tab(self) -> None:
        frame = ttk.Frame(self.tutorial_tab)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        text = tk.Text(frame, wrap="word")
        bar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.insert("1.0", TUTORIAL)
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    # ------------------------------------------------------------ 异步

    def _run_async(self, task: Callable[[], Any],
                   on_ok: Callable[[Any], None],
                   on_err: Callable[[str], None]) -> None:
        def worker() -> None:
            try:
                result = task()
            except (DeployError, KnobError) as exc:
                self.after(0, lambda e=exc: on_err(str(e)))
            except Exception:
                # windowed EXE 没有控制台，任何漏网异常都必须变成可见提示。
                self.after(0, lambda: on_err("发生未预期的错误，请稍后重试。"))
            else:
                self.after(0, lambda r=result: on_ok(r))
        threading.Thread(target=worker, daemon=True).start()

    def _queue_progress(self, text: str) -> None:
        self.log_queue.put(text)

    def _drain_log(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
        self.after(120, self._drain_log)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _busy(self, on: bool, text: Optional[str] = None) -> None:
        self.busy = max(0, self.busy + (1 if on else -1))
        state = "disabled" if self.busy else "normal"
        for button in (self.btn_test, self.btn_deploy, self.btn_status,
                       self.btn_reload, self.btn_apply):
            button.configure(state=state)
        if text is not None:
            self.status_var.set(text)
        self.update_idletasks()

    def _current_config(self) -> DeployConfig:
        values = {key: var.get() for key, var in self.vars.items()}
        return DeployConfig.from_values(values)

    def _run_remote(self, label: str, work: Callable[[SSHDeployer], Any],
                    on_done: Callable[[Any], None]) -> None:
        try:
            cfg = self._current_config()
        except DeployError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._busy(True, label)
        self._append_log(label)

        def task():
            session = SSHDeployer(cfg, progress=self._queue_progress)
            try:
                session.open()
                return work(session)
            finally:
                session.close()

        def on_ok(result):
            self._busy(False, "就绪。")
            on_done(result)

        def on_err(text):
            self._busy(False, "出错，请看下方日志。")
            self._append_log("错误：" + text)
            messagebox.showerror(APP_NAME, text)

        self._run_async(task, on_ok, on_err)

    # ------------------------------------------------------------ 操作

    def test_connection(self) -> None:
        def work(session: SSHDeployer):
            return session.check_environment()

        def done(info):
            text = format_environment(info)
            for line in text.splitlines():
                self._append_log(line)
            self._save_profile()
            messagebox.showinfo(APP_NAME, "连接成功。\n\n" + text)

        self._run_remote("正在测试连接…", work, done)

    def start_deploy(self) -> None:
        try:
            cfg = self._current_config()
        except DeployError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        if not messagebox.askyesno(
            APP_NAME,
            "将在服务器 %s 的 %s 目录下载源码并启动容器。\n\n继续吗？"
            % (cfg.host, cfg.deploy_dir),
        ):
            return
        self._busy(True, "正在部署…")
        self._append_log("开始部署到 %s" % cfg.host)

        def task():
            session = SSHDeployer(cfg, progress=self._queue_progress)
            try:
                session.open()
                session.progress("检查服务器环境")
                env = session.check_environment()
                for line in format_environment(env).splitlines():
                    self._queue_progress(line)
                session.progress("下载源码并启动服务")
                session.deploy()
                return session.read_knob_schema(), session.read_env(), session.status()
            finally:
                session.close()

        def on_ok(result):
            schema, env_text, containers = result
            self._busy(False, "部署完成。")
            self.knobs = knobs_mod.parse_knobs(schema)
            self.env_text = env_text
            self._render_knobs()
            self.container_var.set(format_containers(containers))
            self._save_profile()
            self._append_log("部署完成，共读取到 %d 个配置项。" % len(self.knobs))
            self.notebook.select(self.config_tab)
            messagebox.showinfo(
                APP_NAME,
                "部署完成。\n\n下一步：到“配置与状态”页查看配置，"
                "再按教程用浏览器打开 NapCat 页面扫码登录 QQ。",
            )

        def on_err(text):
            self._busy(False, "部署失败，请看下方日志。")
            self._append_log("错误：" + text)
            messagebox.showerror(APP_NAME, text)

        self._run_async(task, on_ok, on_err)

    def refresh_status(self) -> None:
        def work(session: SSHDeployer):
            return session.status()

        def done(containers):
            self.container_var.set(format_containers(containers))
            self._append_log("状态已刷新。")

        self._run_remote("正在读取运行状态…", work, done)

    def load_config(self) -> None:
        def work(session: SSHDeployer):
            return session.read_knob_schema(), session.read_env(), session.status()

        def done(result):
            schema, env_text, containers = result
            self.knobs = knobs_mod.parse_knobs(schema)
            self.env_text = env_text
            self._render_knobs()
            self.container_var.set(format_containers(containers))
            self._append_log("已读取 %d 个配置项。" % len(self.knobs))
            if not self.knobs:
                messagebox.showinfo(
                    APP_NAME,
                    "服务器上没有配置清单（deploy/console-config.json）。\n"
                    "请确认仓库里包含该文件，或先完成一次部署。",
                )

        self._run_remote("正在读取服务器配置…", work, done)

    def _render_knobs(self) -> None:
        for child in self.knob_area.winfo_children():
            child.destroy()
        self.knob_vars.clear()
        self.knob_by_key = {knob.key: knob for knob in self.knobs}
        if not self.knobs:
            ttk.Label(self.knob_area,
                      text="还没有配置项。请先完成部署，或点“刷新配置”。",
                      foreground="#666").pack(anchor="w", padx=10, pady=10)
            return
        values = knobs_mod.env_values(self.env_text)
        for group in knobs_mod.group_order(self.knobs):
            frame = ttk.LabelFrame(self.knob_area, text=group)
            frame.pack(fill="x", padx=4, pady=6)
            frame.columnconfigure(1, weight=1)
            row = 0
            for knob in [item for item in self.knobs if item.group == group]:
                raw = values.get(knob.key)
                ttk.Label(frame, text=knob.label).grid(
                    row=row, column=0, sticky="w", padx=8, pady=4)
                if knob.secret:
                    var: Any = tk.StringVar(value="")
                    widget: Any = ttk.Entry(frame, textvariable=var, width=30, show="*")
                    note = "%s；留空表示不修改" % knobs_mod.secret_state(knob, raw)
                elif knob.kind == "bool":
                    var = tk.BooleanVar(value=knobs_mod.is_enabled(knob, raw))
                    widget = ttk.Checkbutton(frame, variable=var)
                    note = knob.hint
                elif knob.kind == "enum":
                    var = tk.StringVar(value=knobs_mod.display_value(knob, raw))
                    widget = ttk.Combobox(frame, textvariable=var,
                                          values=list(knob.options), state="readonly",
                                          width=28)
                    note = knob.hint
                else:
                    var = tk.StringVar(value=knobs_mod.display_value(knob, raw))
                    widget = ttk.Entry(frame, textvariable=var, width=30)
                    note = knob.hint
                widget.grid(row=row, column=1, sticky="ew", padx=8, pady=4)
                suffix = "（改完需要重启服务）" if knob.restart else "（改完立即生效）"
                ttk.Label(frame, text=note + suffix, foreground="#666",
                          wraplength=360, justify="left").grid(
                    row=row, column=2, sticky="w", padx=8, pady=4)
                self.knob_vars[knob.key] = var
                row += 1

    def apply_config(self) -> None:
        if not self.knobs:
            messagebox.showinfo(APP_NAME, "还没有配置项，请先完成部署或刷新配置。")
            return
        updates: Dict[str, str] = {}
        try:
            for knob in self.knobs:
                var = self.knob_vars.get(knob.key)
                if var is None:
                    continue
                value = var.get()
                if knob.secret:
                    if str(value).strip() == "":
                        continue
                    updates[knob.key] = knobs_mod.coerce_value(knob, value)
                else:
                    updates[knob.key] = knobs_mod.coerce_value(knob, value)
        except KnobError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        new_text, changed = knobs_mod.apply_env_text(self.env_text, updates)
        if not changed:
            messagebox.showinfo(APP_NAME, "配置没有变化。")
            return
        needs_restart = any(self.knob_by_key[key].restart for key in changed)
        services = ("astrbot",) if needs_restart else ()

        def work(session: SSHDeployer):
            session.write_env(new_text)
            if services:
                session.recreate(services)
            return session.read_env(), session.status()

        def done(result):
            env_text, containers = result
            self.env_text = env_text
            self._render_knobs()
            self.container_var.set(format_containers(containers))
            self._append_log("已保存配置：%s" % "、".join(sorted(changed)))
            messagebox.showinfo(APP_NAME, "配置已保存并生效。")

        self._run_remote("正在保存配置…", work, done)

    def _save_profile(self) -> None:
        try:
            cfg = self._current_config()
        except DeployError:
            return
        try:
            save_profile(cfg.safe_profile())
        except DeployError as exc:
            self._append_log("提示：%s" % exc)


if __name__ == "__main__":
    DesktopApp().mainloop()
