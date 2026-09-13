#!/usr/bin/env python3
"""
Strava 自动同步守护进程 — 监听文件夹，自动上传新活动
=======================================================

适用场景：
  码表 / 运动手表通过 USB 或蓝牙同步 FIT 文件到本地文件夹后，
  本脚本自动检测新文件并上传到 Strava，无需手动操作。

原理：
  与 strava_cookie_upload.py 相同，使用 _strava4_session cookie 模拟网页端
  上传（POST /upload/files + CSRF token），绕过 Strava API 订阅限制。
  协议实现集中在 strava_web.py，两个脚本共用。

⚠️ 与旧版（走 /api/v3/uploads 的那版）的区别：
  · /api/v3/* 现在只认 OAuth Bearer token，带 session cookie 一律 401
  · 轮询改走 /upload/progress.json?ids[]=<upload_id>，响应里直接带活动 id
  · 成功后照旧记录 activity_id 和 strava_url，去重逻辑不变

用法：
  # 监听 Garmin 默认同步目录
  python strava_watch_upload.py --cookie "xxx" --watch ~/Garmin/Activities

  # 一次性扫描目录并上传所有新文件
  python strava_watch_upload.py --cookie "xxx" --watch ~/Activities --once

  # 持续监听，每 30 秒扫描一次
  python strava_watch_upload.py --cookie "xxx" --watch ~/Activities --interval 30

  # 移动已上传文件到子目录（避免重复上传）
  python strava_watch_upload.py --cookie "xxx" --watch ~/Activities --move-uploaded

  # 使用环境变量
  export STRAVA_SESSION_COOKIE="xxx"
  python strava_watch_upload.py --watch ~/Activities

首次使用：
  1. 先用 strava_cookie_upload.py --save-cookie "xxx" 保存 cookie
  2. 本脚本自动读取 ~/.strava_cookie

依赖：pip install requests
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

import strava_web as sw

# Windows 控制台默认 GBK，重设为 UTF-8，否则 ⚠ / ❌ 等字符会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ── 常量 ──────────────────────────────────────────────────────────────

USER_AGENT = sw.USER_AGENT

SUPPORTED_EXTENSIONS = {".fit", ".gpx", ".tcx", ".fit.gz", ".gpx.gz", ".tcx.gz"}

# 默认状态数据库路径
DEFAULT_DB = Path.home() / ".strava_uploads.db"


# ── 数据库（记录已上传文件，避免重复） ─────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            file_hash TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            upload_id INTEGER,
            activity_id INTEGER,
            strava_url TEXT,
            uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def file_already_uploaded(conn: sqlite3.Connection, file_path: str) -> bool:
    """
    检查文件是否已经上传过。

    新协议下无法验证活动是否真的创建成功（没有状态端点可查），所以把
    'accepted'（Strava 已收下文件）也算作已上传 —— 否则每轮扫描都会重复上传。
    'success' 是旧版协议留下的状态，保留兼容。
    """
    row = conn.execute(
        "SELECT id FROM uploaded_files WHERE file_path = ? AND status IN ('success', 'accepted')",
        (str(file_path),),
    ).fetchone()
    return row is not None


def record_upload(
    conn: sqlite3.Connection,
    file_path: str,
    file_size: int,
    upload_id: int = None,
    activity_id: int = None,
    status: str = "pending",
    strava_url: str = None,
):
    """记录上传"""
    file_hash = hash_file(file_path)
    conn.execute(
        """INSERT OR REPLACE INTO uploaded_files
           (file_path, file_hash, file_size, upload_id, activity_id, strava_url, uploaded_at, status)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
        (str(file_path), file_hash, file_size, upload_id, activity_id, strava_url, status),
    )
    conn.commit()


def log_event(conn: sqlite3.Connection, file_path: str, event: str, detail: str = None):
    """记录日志"""
    conn.execute(
        "INSERT INTO upload_log (file_path, event, detail, created_at) VALUES (?, ?, ?, datetime('now'))",
        (str(file_path), event, detail),
    )
    conn.commit()


# ── 文件处理 ──────────────────────────────────────────────────────────

def hash_file(file_path: str) -> str:
    """计算文件前 64KB 的 SHA-256（快速且够用）"""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        sha.update(f.read(65536))
    return sha.hexdigest()


def is_activity_file(file_path: str) -> bool:
    """判断是否为支持的活动文件（排除缓存、临时文件等）"""
    path = Path(file_path)
    # 排除隐藏文件、临时文件
    if path.name.startswith(".") or path.name.startswith("~"):
        return False
    if path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return True
    # 处理 .gz 双后缀
    return any(file_path.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)


def scan_files(watch_dir: str) -> list[str]:
    """扫描目录中的活动文件"""
    files = []
    watch = Path(watch_dir).expanduser().resolve()
    if not watch.exists():
        print(f"⚠ 目录不存在: {watch}")
        return files
    for f in watch.rglob("*"):
        if f.is_file() and is_activity_file(str(f)):
            files.append(str(f))
    return sorted(files)


def move_file(file_path: str, target_dir: str):
    """移动已上传文件到指定目录"""
    src = Path(file_path)
    dst_dir = Path(target_dir).expanduser().resolve()
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    # 如果目标已存在，加时间戳
    if dst.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = dst_dir / f"{src.stem}_{ts}{src.suffix}"
    src.rename(dst)
    return str(dst)


# ── 上传逻辑 ──────────────────────────────────────────────────────────

def upload_file(session, file_path: str, csrf: str,
                timeout: int = 120, max_wait: float = 120) -> tuple[dict, str]:
    """
    上传单个文件并等到 Strava 处理完。协议在 strava_web.py，这里只是薄封装。

    返回 (结果, 可能已刷新的 csrf)。成功时结果里含 activity_id / activity。
    """
    print(f"  ↑ {Path(file_path).name} ", end="", flush=True)
    result, csrf = sw.upload_with_retry(session, file_path, csrf, timeout=timeout)
    if result.get("error"):
        print("✗")            # 换行收尾，错误细节由调用方打印
        return result, csrf

    upload_id = result.get("id") or result.get("id_str")
    print(f"upload_id={upload_id} → 等待处理 ", end="", flush=True)

    prog = sw.poll_upload(session, upload_id, max_wait=max_wait)
    if prog.get("error"):
        print(f"✗ {prog.get('detail')}")
        return {"error": prog.get("error"), "detail": prog.get("detail"),
                "upload_id": upload_id}, csrf

    activity = prog.get("activity") or {}
    activity_id = activity.get("id") or activity.get("id_str")
    print(f"✓ activity_id={activity_id}")

    result = dict(result)
    result["activity"] = activity
    result["activity_id"] = activity_id
    return result, csrf


# ── 一次性扫描上传 ───────────────────────────────────────────────────

def process_scan(
    watch_dir: str,
    cookie: str,
    conn: sqlite3.Connection,
    move_uploaded: str = None,
) -> tuple[int, int]:
    """
    扫描目录，上传所有新文件。

    返回: (成功数, 失败数)
    """
    files = scan_files(watch_dir)
    if not files:
        print("  没有找到活动文件")
        return 0, 0

    # 过滤已上传的
    new_files = [f for f in files if not file_already_uploaded(conn, f)]
    skipped = len(files) - len(new_files)
    if skipped:
        print(f"  跳过 {skipped} 个已上传文件")

    if not new_files:
        print("  所有文件均已上传过")
        return 0, 0

    # 确认有新文件后才建会话 / 取 CSRF token（这一步顺带验证 cookie 是否有效）
    try:
        session = sw.new_session(cookie)
        csrf = sw.fetch_csrf(session)
    except sw.AuthError as e:
        print(f"  ✗ {e}")
        return 0, 0
    except (sw.CsrfError, requests.RequestException) as e:
        print(f"  ✗ 取 CSRF token 失败: {e}")
        return 0, 0

    print(f"  找到 {len(new_files)} 个新文件")
    success = 0
    failed = 0

    for i, file_path in enumerate(new_files, 1):
        file_size = Path(file_path).stat().st_size
        print(f"  [{i}/{len(new_files)}] {Path(file_path).name} ({file_size} bytes)")

        result, csrf = upload_file(session, file_path, csrf)
        err = result.get("error")

        if err == "rate_limited":
            print("    ⚠ 限速，等待 60 秒...")
            time.sleep(60)
            result, csrf = upload_file(session, file_path, csrf)
            err = result.get("error")

        if err:
            detail = str(result.get("detail") or "")[:150]
            print(f"    ✗ 上传失败: {err} {detail}")
            failed += 1
            record_upload(conn, file_path, file_size, status="failed")
            log_event(conn, file_path, err,
                      json.dumps(result, ensure_ascii=False, default=str)[:500])
            if err == "auth_failed":
                print("    ✗ Cookie 已过期! 上传终止")
                break
            continue

        activity_id = result.get("activity_id")
        strava_url = (f"https://www.strava.com/activities/{activity_id}"
                      if activity_id else None)
        success += 1
        record_upload(conn, file_path, file_size, upload_id=result.get("id"),
                      activity_id=activity_id, strava_url=strava_url,
                      status="success")
        log_event(conn, file_path, "upload_success", strava_url or "")
        if strava_url:
            print(f"    → {strava_url}")

        # 移动已上传文件
        if move_uploaded:
            new_path = move_file(file_path, move_uploaded)
            print(f"    → 已移动到: {new_path}")

        # 文件间延迟
        if i < len(new_files):
            time.sleep(2)

    return success, failed


# ── 持续监听模式 ─────────────────────────────────────────────────────

class Watchdog:
    """轻量级文件夹监听器，定时扫描新文件"""

    def __init__(
        self,
        watch_dir: str,
        cookie: str,
        db_path: str = None,
        interval: int = 30,
        move_uploaded: str = None,
    ):
        self.watch_dir = Path(watch_dir).expanduser().resolve()
        self.cookie = cookie
        self.db_path = db_path or str(DEFAULT_DB)
        self.interval = interval
        self.move_uploaded = move_uploaded
        self.conn = init_db(self.db_path)
        self._running = False
        self._thread = None

    def start(self):
        """启动监听"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"\n🔍 开始监听: {self.watch_dir}")
        print(f"   间隔: {self.interval} 秒")
        print(f"   按 Ctrl+C 停止\n")

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """停止监听"""
        self._running = False
        print("\n👋 监听已停止")
        self.conn.close()

    def _run(self):
        """后台线程：定时扫描"""
        last_scan = 0

        while self._running:
            now = time.time()
            if now - last_scan >= self.interval:
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{timestamp}] 扫描中...")

                try:
                    success, failed = process_scan(
                        str(self.watch_dir),
                        self.cookie,
                        self.conn,
                        move_uploaded=self.move_uploaded,
                    )
                    if success or failed:
                        print(f"[{timestamp}] 成功: {success}, 失败: {failed}")
                    else:
                        print(f"[{timestamp}] 无新文件")
                except Exception as e:
                    print(f"[{timestamp}] 错误: {e}")

                last_scan = now

            time.sleep(1)  # 每秒检查一次退出标志


# ── 工具函数 ──────────────────────────────────────────────────────────

def load_cookie(cookie_arg: str = None) -> str:
    if cookie_arg:
        return cookie_arg
    env_val = os.environ.get("STRAVA_SESSION_COOKIE", "")
    if env_val:
        return env_val
    config_paths = [
        Path.home() / ".strava_cookie",
        Path.home() / ".config" / "strava" / "cookie",
    ]
    for p in config_paths:
        if p.exists():
            return p.read_text().strip()
    return ""


# ── 主入口 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Strava 自动同步 — 监听文件夹自动上传活动文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 一次性扫描上传
  python strava_watch_upload.py -c "xxx" -w ~/Garmin/Activities --once

  # 持续监听
  python strava_watch_upload.py -c "xxx" -w ~/Garmin/Activities

  # 上传后移动文件（避免重复）
  python strava_watch_upload.py -c "xxx" -w ~/Activities --move-uploaded ~/Activities/uploaded

  # 使用保存在 ~/.strava_cookie 的 cookie
  python strava_watch_upload.py -w ~/Garmin/Activities
        """,
    )

    parser.add_argument("--watch", "-w", required=True, help="要监听的文件夹路径")
    parser.add_argument("--cookie", "-c", help="_strava4_session cookie 值")
    parser.add_argument("--once", action="store_true", help="只扫描一次后退出（不持续监听）")
    parser.add_argument("--interval", "-i", type=int, default=30, help="扫描间隔（秒，默认 30）")
    parser.add_argument("--move-uploaded", "-m", help="上传成功后移动到目标目录")
    parser.add_argument("--db", help=f"状态数据库路径（默认 ~/.strava_uploads.db）")

    args = parser.parse_args()

    # 加载 cookie
    cookie = load_cookie(args.cookie)
    if not cookie:
        print("❌ 未提供 _strava4_session cookie")
        print()
        print("获取方式（推荐）:")
        print("  1. 浏览器打开 https://www.strava.com 并登录")
        print("  2. F12 → Network → 刷新页面 → 右键任意 strava.com 请求")
        print("  3. Copy → Copy as cURL (bash)")
        print("  4. python save_strava_cookie.py")
        sys.exit(1)

    print(f"Cookie: {cookie[:12]}...{cookie[-4:]}")
    print(f"监听目录: {args.watch}")
    print()

    db_path = args.db or str(DEFAULT_DB)
    conn = init_db(db_path)

    if args.once:
        # 一次性模式
        success, failed = process_scan(
            args.watch, cookie, conn,
            move_uploaded=args.move_uploaded,
        )
        conn.close()
        print(f"\n完成: 成功 {success}, 失败 {failed}")
    else:
        # 持续监听模式
        watchdog = Watchdog(
            args.watch,
            cookie,
            db_path=db_path,
            interval=args.interval,
            move_uploaded=args.move_uploaded,
        )
        watchdog.start()


if __name__ == "__main__":
    main()
