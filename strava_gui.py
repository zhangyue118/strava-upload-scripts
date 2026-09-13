#!/usr/bin/env python3
"""
Strava 上传器 — 图形界面
=========================

把 strava_cookie_upload.py 包一层 Tkinter 界面，省得敲命令行。

v1 范围（刻意做小）：
  1. 粘贴 cookie（带验证，就地去 Strava 验一次）
  2. 选择本地文件（可多选）
  3. 上传
  4. 单文件时可以改标题和说明；多文件走 Strava 自动名称

刻意不做（见 README 的未决清单）：
  · 重复上传检测
  · 逐行编辑标题（多文件时）
  · 上传历史/状态库

上传协议在 strava_web.py。Tkinter 是 Python 自带的，不需要 pip 装任何东西。

运行：
  python strava_gui.py          # 会弹一个控制台窗口
  双击 strava_gui.pyw           # 不弹控制台（推荐给最终用户）

平台：只在 Windows 上测过。Linux 上需要额外装系统包（python3-tk），
      macOS 用 Homebrew 装的 Python 也不含 Tk（brew install python-tk）。
"""

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests

import strava_web as sw


def classify_exception(e: Exception) -> tuple[str, str]:
    """
    把异常翻译成 (级别, 给用户看的话)。

    这个区分是这个工具最要紧的一处：cookie 失效和 Strava 改协议，
    用户能做的事完全不同 —— 前者重粘一次，后者只能等你发新版。
    分不出来的话，所有反馈都会长成"上传失败"。
    """
    if isinstance(e, sw.AuthError):
        return "auth", f"cookie 失效或未登录：{e}"

    if isinstance(e, sw.CsrfError):
        return "csrf", f"页面结构异常：{e}"

    if isinstance(e, requests.Timeout):
        return "network", ("连接 Strava 超时（30 秒无响应）。"
                           "可能是网络不通，也可能是被临时限速——等几分钟再试。")

    if isinstance(e, requests.RequestException):
        return "network", f"连不上 Strava：{e.__class__.__name__}"

    return "other", f"{type(e).__name__}: {e}"

COOKIE_PATH = Path.home() / ".strava_cookie"

FILE_TYPES = [
    ("运动文件", "*.fit *.fit.gz *.gpx *.gpx.gz *.tcx *.tcx.gz"),
    ("FIT", "*.fit *.fit.gz"),
    ("GPX", "*.gpx *.gpx.gz"),
    ("TCX", "*.tcx *.tcx.gz"),
    ("全部文件", "*.*"),
]


def load_saved_cookie() -> str:
    if COOKIE_PATH.exists():
        return COOKIE_PATH.read_text(encoding="utf-8").strip()
    return ""


def save_cookie(cookie: str):
    COOKIE_PATH.write_text(cookie, encoding="utf-8")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.files: list[Path] = []
        self.queue: queue.Queue = queue.Queue()
        self.uploading = False
        self.rows: dict[str, str] = {}          # 文件路径 -> Treeview item id

        root.title("Strava 上传器")

        self._build_ui()
        self._fit_window()
        self._refresh_title_fields()

        saved = load_saved_cookie()
        if saved:
            self.cookie_var.set(saved)
            self._log(f"已载入保存的 cookie（{len(saved)} 字符）")

        self.root.after(100, self._drain_queue)

    def _fit_window(self):
        """
        按内容的实际需要定窗口大小。

        不能写死 geometry("820x680")：开了 DPI 感知之后，控件按物理像素放大，
        150% 缩放的屏幕上内容需要 900+ px。而 Tk 的 pack 在空间不足时，会把
        **最后 pack 的那个帧**压到近乎零高度 —— 结果就是「第 4 帧只剩一条边框，
        上传按钮看不见」。

        下限设成内容高度，这样用户缩小窗口时也不会再把按钮挤没。
        """
        self.root.update_idletasks()

        w = max(860, self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()

        # 屏幕装不下就封顶，至少别把窗口顶到屏幕外面去
        max_h = self.root.winfo_screenheight() - 140
        if h > max_h:
            h = max_h

        self.root.geometry(f"{w}x{h}")
        self.root.minsize(700, h)

    # ── 界面搭建 ──────────────────────────────────────────────────────

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # 1. Cookie
        f1 = ttk.LabelFrame(self.root, text="1. Strava Cookie")
        f1.pack(fill="x", **pad)

        self.cookie_var = tk.StringVar()
        ttk.Entry(f1, textvariable=self.cookie_var).pack(
            fill="x", padx=10, pady=(8, 4))

        row = ttk.Frame(f1)
        row.pack(fill="x", padx=10, pady=(0, 8))
        self.verify_btn = ttk.Button(row, text="验证并保存", command=self._verify_cookie)
        self.verify_btn.pack(side="left")
        self.cookie_status = ttk.Label(row, text="未验证", foreground="#888")
        self.cookie_status.pack(side="left", padx=10)

        ttk.Label(
            f1,
            text="获取方式：浏览器登录 strava.com → F12 → Network → 刷新 → "
                 "右键任意请求 → Copy as cURL → 粘贴到这里",
            foreground="#888", wraplength=760, justify="left",
        ).pack(fill="x", padx=10, pady=(0, 8))

        # 2. 文件
        f2 = ttk.LabelFrame(self.root, text="2. 要上传的文件")
        f2.pack(fill="both", expand=True, **pad)

        row2 = ttk.Frame(f2)
        row2.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Button(row2, text="选择文件…", command=self._pick_files).pack(side="left")
        ttk.Button(row2, text="清空", command=self._clear_files).pack(side="left", padx=6)

        cols = ("name", "size", "status")
        self.tree = ttk.Treeview(f2, columns=cols, show="headings", height=6)
        self.tree.heading("name", text="文件名")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=430, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("status", width=230, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # 3. 活动信息（仅单文件）
        self.f3 = ttk.LabelFrame(self.root, text="3. 标题与说明（仅单个文件时可用）")
        self.f3.pack(fill="x", **pad)

        ttk.Label(self.f3, text="标题").grid(row=0, column=0, sticky="w", padx=(10, 6), pady=5)
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(self.f3, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, sticky="ew", pady=5)

        ttk.Label(self.f3, text="说明").grid(row=1, column=0, sticky="w", padx=(10, 6), pady=(0, 5))
        self.desc_var = tk.StringVar()
        self.desc_entry = ttk.Entry(self.f3, textvariable=self.desc_var)
        self.desc_entry.grid(row=1, column=1, sticky="ew", pady=(0, 5))

        self.multi_hint = ttk.Label(
            self.f3, text="", foreground="#888", wraplength=740, justify="left")
        self.multi_hint.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 6))

        self.f3.columnconfigure(1, weight=1)

        ttk.Label(
            self.f3, text="留空 = 使用 Strava 从文件里读出的名称", foreground="#888"
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))

        # 4. 上传
        f4 = ttk.LabelFrame(self.root, text="4. 上传")
        f4.pack(fill="both", expand=True, **pad)

        row4 = ttk.Frame(f4)
        row4.pack(fill="x", padx=10, pady=(8, 4))
        self.upload_btn = ttk.Button(row4, text="开始上传", command=self._start_upload)
        self.upload_btn.pack(side="left")
        self.progress = ttk.Progressbar(row4, mode="determinate", length=380)
        self.progress.pack(side="left", padx=12, fill="x", expand=True)

        # 日志带滚动条 —— 只靠滚轮的话，之前那几行错误用户看不到
        logwrap = ttk.Frame(f4)
        logwrap.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        sb = ttk.Scrollbar(logwrap, orient="vertical")
        self.log = tk.Text(logwrap, height=7, wrap="word", state="disabled",
                           background="#f7f7f7", relief="flat",
                           yscrollcommand=sb.set)
        sb.configure(command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_config("err", foreground="#c0392b")
        self.log.tag_config("ok", foreground="#1e8449")
        self.log.tag_config("warn", foreground="#b9770e")
        self.log.tag_config("dim", foreground="#888")

    # ── 界面辅助 ──────────────────────────────────────────────────────

    def _log(self, text: str, tag: str = None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_controls(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.upload_btn.configure(state=state)
        self.verify_btn.configure(state=state)

    def _refresh_title_fields(self):
        """只有恰好一个文件时才允许改标题/说明"""
        single = len(self.files) == 1
        state = "normal" if single else "disabled"
        self.name_entry.configure(state=state)
        self.desc_entry.configure(state=state)

        if len(self.files) == 1:
            self.multi_hint.configure(text="")
        elif len(self.files) == 0:
            self.multi_hint.configure(text="尚未选择文件。")
        else:
            self.multi_hint.configure(
                text=f"已选 {len(self.files)} 个文件 —— 多文件上传时不支持改标题，"
                     "这些活动将使用 Strava 自动生成的名称。"
                     "要改某个的标题，请只选中它一个。"
            )
            self.name_var.set("")
            self.desc_var.set("")

    # ── 文件操作 ──────────────────────────────────────────────────────

    def _pick_files(self):
        paths = filedialog.askopenfilenames(title="选择要上传的文件", filetypes=FILE_TYPES)
        if not paths:
            return

        skipped = []
        for p in paths:
            path = Path(p)
            try:
                sw.detect_data_type(path)
            except ValueError as e:
                skipped.append(f"{path.name}: {e}")
                continue
            if path in self.files:
                continue
            self.files.append(path)

        self._rebuild_tree()
        self._refresh_title_fields()

        for s in skipped:
            self._log(f"跳过 {s}", "warn")
        if self.files:
            self._log(f"已选择 {len(self.files)} 个文件")

    def _rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for f in self.files:
            try:
                size = human_size(f.stat().st_size)
            except OSError:
                size = "?"
            item = self.tree.insert("", "end", values=(f.name, size, "待上传"))
            self.rows[str(f)] = item

    def _clear_files(self):
        if self.uploading:
            return
        self.files.clear()
        self._rebuild_tree()
        self._refresh_title_fields()
        self.progress["value"] = 0

    # ── Cookie 验证 ───────────────────────────────────────────────────

    def _verify_cookie(self):
        cookie = self.cookie_var.get().strip()
        if not cookie:
            messagebox.showwarning("提示", "请先粘贴 cookie")
            return

        self.verify_btn.configure(state="disabled")
        self.cookie_status.configure(text="验证中…", foreground="#888")
        threading.Thread(target=self._verify_worker, args=(cookie,), daemon=True).start()

    def _verify_worker(self, cookie: str):
        try:
            session = sw.new_session(cookie)
            sw.fetch_csrf(session)
        except Exception as e:
            level, msg = classify_exception(e)
            self.queue.put(("cookie", (False, msg, level)))
            return

        try:
            save_cookie(cookie)
        except OSError as e:
            self.queue.put(("cookie", (True, f"验证通过，但保存失败：{e}", "warn")))
            return
        self.queue.put(("cookie", (True, f"验证通过，已保存到 {COOKIE_PATH}", "ok")))

    # ── 上传 ──────────────────────────────────────────────────────────

    def _start_upload(self):
        if self.uploading:
            return
        if not self.files:
            messagebox.showwarning("提示", "请先选择要上传的文件")
            return
        cookie = self.cookie_var.get().strip()
        if not cookie:
            messagebox.showwarning("提示", "请先粘贴 cookie 并验证")
            return

        # 多文件时不带标题 —— 见用户要求的 v1 范围
        single = len(self.files) == 1
        name = (self.name_var.get().strip() or None) if single else None
        desc = (self.desc_var.get().strip() or None) if single else None

        self.uploading = True
        self._set_controls(False)
        self.progress["value"] = 0
        self.progress["maximum"] = len(self.files)
        for f in self.files:
            self.tree.set(self.rows[str(f)], "status", "等待中")

        threading.Thread(target=self._upload_worker,
                         args=(cookie, name, desc), daemon=True).start()

    def _upload_worker(self, cookie: str, name, desc):
        def log(t, tag=None):
            self.queue.put(("log", (t, tag)))

        def status(path, text):
            self.queue.put(("status", (path, text)))

        # 建会话 —— 这一步顺便验证 cookie
        try:
            session = sw.new_session(cookie)
            csrf = sw.fetch_csrf(session)
        except Exception as e:
            level, msg = classify_exception(e)
            log(f"✗ {msg}", "err")
            if level == "auth":
                log("  → cookie 失效了。重新复制一次，粘贴后点「验证并保存」。", "warn")
            elif level == "csrf":
                log("  → 这不是 cookie 的问题 —— 拿到的响应说明页面结构变了，"
                    "通常意味着 Strava 改动了上传协议。", "warn")
                log("  → 请把上面这几行日志附到项目 issue 里。", "warn")
            elif level == "network":
                log("  → 网络层面就没连上，先别怀疑 cookie。稍后再试。", "warn")
            self.queue.put(("done", None))
            return

        ok = 0
        fail = 0

        for i, path in enumerate(self.files, 1):
            status(path, "上传中…")
            log(f"[{i}/{len(self.files)}] {path.name}")

            result, csrf = sw.upload_with_retry(session, path, csrf)

            if result.get("error") == "rate_limited":
                status(path, "被限速，等待 60 秒")
                log("  ⚠ 触发限速，等 60 秒后重试…", "warn")
                time.sleep(60)
                result, csrf = sw.upload_with_retry(session, path, csrf)

            if result.get("error"):
                fail += 1
                self._report_failure(result, log, status, path)
                self.queue.put(("progress", i))
                continue

            upload_id = result.get("id") or result.get("id_str")
            status(path, f"已收到 (upload_id={upload_id})，等待处理…")
            prog = sw.poll_upload(session, upload_id)

            if prog.get("error"):
                fail += 1
                kind = prog.get("error")
                if kind == "timeout":
                    log("  ✗ 超时：没等到处理结果。", "err")
                    log(f"    但文件**可能已经传上去了**（upload_id={upload_id}）。", "warn")
                    log("    → 先去 Strava 上确认有没有这条活动，再决定要不要重传。", "warn")
                    status(path, "超时，需人工确认")
                else:
                    log(f"  ✗ 处理失败：{prog.get('detail')}", "err")
                    status(path, "处理失败")
                self.queue.put(("progress", i))
                continue

            activity = prog.get("activity") or {}
            activity_id = activity.get("id") or activity.get("id_str")
            url = f"https://www.strava.com/activities/{activity_id}"

            # 标题/说明：仅单文件时
            if name is not None or desc is not None:
                meta = sw.set_activity_metadata(session, activity, csrf,
                                                name=name, description=desc)
                if meta.get("error") == "csrf_failed":
                    csrf = sw.fetch_csrf(session)
                    meta = sw.set_activity_metadata(session, activity, csrf,
                                                    name=name, description=desc)
                if meta.get("error"):
                    log(f"  ⚠ 活动已创建，但标题/说明没设置成功：{meta.get('detail')}", "warn")
                else:
                    log("  ✓ 标题/说明已设置", "ok")

            ok += 1
            status(path, f"完成 → {activity_id}")
            log(f"  ✓ {url}", "ok")
            self.queue.put(("progress", i))

        self.queue.put(("done", (ok, fail)))

    def _report_failure(self, result: dict, log, status, path):
        kind = result.get("error")
        detail = result.get("detail")

        if kind == "auth_failed":
            log("  ✗ cookie 失效（认证被拒）", "err")
            log("    → 重新复制 cookie，粘贴后点「验证并保存」。", "warn")
            status(path, "cookie 失效")
        elif kind == "csrf_failed":
            log("  ✗ CSRF token 被拒 —— 这不像 cookie 问题", "err")
            log("    → 疑似 Strava 改了上传协议，请把日志附到 issue 里。", "warn")
            status(path, "疑似协议变更")
        elif kind == "http_error":
            log(f"  ✗ HTTP {result.get('status_code')}：{detail}", "err")
            status(path, f"HTTP {result.get('status_code')}")
        else:
            log(f"  ✗ 上传失败：{kind} {detail or ''}", "err")
            status(path, f"失败 ({kind})")

    # ── 线程 → 界面 的消息泵 ─────────────────────────────────────────

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()

                if kind == "log":
                    text, tag = payload
                    self._log(text, tag)
                elif kind == "status":
                    path, text = payload
                    item = self.rows.get(str(path))
                    if item:
                        self.tree.set(item, "status", text)
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "cookie":
                    ok, msg, level = payload
                    tag = {"ok": "ok", "warn": "warn", "auth": "err", "csrf": "err",
                           "network": "warn", "other": "err"}.get(level, None)

                    if ok:
                        label, colour = "✓ 已验证", "#1e8449"
                    elif level == "network":
                        # 没连上，不代表 cookie 有问题 —— 别让用户白重粘一遍
                        label, colour = "? 没能验证（网络）", "#b9770e"
                    else:
                        label, colour = "✗ 验证失败", "#c0392b"
                    self.cookie_status.configure(text=label, foreground=colour)

                    self._log(msg, tag)
                    if not ok:
                        self._log("  （cookie 未保存）", "dim")
                    if level == "csrf":
                        self._log("  → 这不是 cookie 的问题，是 Strava 那边变了。"
                                  "请把日志附到 issue 里。", "warn")
                    elif level == "network":
                        self._log("  → 连都没连上，先别急着重新复制 cookie。", "warn")

                    self.verify_btn.configure(state="normal")
                elif kind == "done":
                    self.uploading = False
                    self._set_controls(True)
                    if payload:
                        ok, fail = payload
                        self._log("")
                        self._log(f"完成：成功 {ok}，失败 {fail}", "ok" if not fail else "warn")
                    self._refresh_title_fields()
        except queue.Empty:
            pass

        self.root.after(100, self._drain_queue)


def main():
    # Windows 高分屏：必须在创建任何窗口之前设置，否则字会糊。
    # 非 Windows 上 ctypes 这段会直接抛异常，忽略即可。
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
