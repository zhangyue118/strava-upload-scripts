#!/usr/bin/env python3
"""
Strava Cookie Uploader — 绕过 API 订阅限制上传活动文件
=========================================================

原理：用浏览器 session cookie 认证，模拟网页端上传，绕过 Strava API
的开发者订阅要求。上传协议实现在 strava_web.py（唯一一处，两个上传脚本共用）。

用法：

  # 单文件上传
  python strava_cookie_upload.py ride.fit

  # 批量上传
  python strava_cookie_upload.py *.fit

  # 指定 cookie
  python strava_cookie_upload.py --cookie "<cookie值>" ride.fit

  # 保存 cookie 到 ~/.strava_cookie
  python strava_cookie_upload.py --save-cookie "<cookie值>"

获取 cookie 用：
  python save_strava_cookie.py

协议（2026-09 抓 HAR 确认，实现在 strava_web.py）：

  GET  /upload/select                           → 取 CSRF token
  POST /upload/files                            → 上传，返回 upload_id
  GET  /upload/progress.json?ids[]=<id>         → 轮询，拿到活动 id
  POST /athlete/training_activities/bulk_update → 设置名称/描述

⚠️ 与旧版（走 /api/v3/uploads 的那版）的区别：
  · /api/v3/* 现在只认 OAuth Bearer token，带 session cookie 一律 401。
    所以旧脚本会"认证失败"，那不是 cookie 的问题。
  · --name / --desc 是在上传成功后单独调 bulk_update 端点实现的
    （上传接口本身不接受这两个参数）。

依赖：pip install requests
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

import strava_web as sw

# Windows 控制台默认 GBK，重设为 UTF-8，否则 ⚠ / ❌ 等字符会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ── 配置 ──────────────────────────────────────────────────────────────
# Cookie 优先级: 命令行参数 > 环境变量 > 配置文件
# 下面这两个名字保留给其他脚本 import（诊断脚本会用到）
USER_AGENT = sw.USER_AGENT
detect_data_type = sw.detect_data_type
SUPPORTED_TYPES = sw.SUPPORTED_TYPES

# cookie 值是 32 位随机串（实测），形如 4s4vnjbp3eoq...ovna
# 注意：不要指望它很长 —— 老文档里说的 "~350 字符 / e32a 开头" 是错的
COOKIE = os.environ.get("STRAVA_SESSION_COOKIE", "")


# ── 工具函数 ──────────────────────────────────────────────────────────

def load_cookie(cookie_arg: str | None = None) -> str:
    """从多个来源加载 cookie"""
    if cookie_arg:
        return cookie_arg
    if COOKIE:
        return COOKIE
    # 尝试从配置文件读取
    config_paths = [
        Path.home() / ".strava_cookie",
        Path.home() / ".config" / "strava" / "cookie",
        Path.cwd() / ".strava_cookie",
    ]
    for p in config_paths:
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return ""


def save_cookie(cookie: str):
    """将 cookie 保存到 ~/.strava_cookie"""
    p = Path.home() / ".strava_cookie"
    p.write_text(cookie, encoding="utf-8")
    print(f"✓ Cookie 已保存到 {p}")
    print("  注意：Windows 上 chmod 只改只读标志，不构成真正的权限保护")


# ── 核心函数 ──────────────────────────────────────────────────────────

def upload_one(session: requests.Session, file_path: str, csrf: str,
               name: str = None, desc: str = None, timeout: int = 120,
               poll: bool = True) -> tuple[dict, str]:
    """
    上传单个文件 → 轮询处理结果 → 可选设置名称/描述。

    返回 (结果, 可能已刷新的 csrf)。结果里成功时含 activity_id / activity。
    """
    path = Path(file_path)
    print(f"  ↑ 上传中: {path.name} ({path.stat().st_size} 字节) ... ", end="", flush=True)

    result, csrf = sw.upload_with_retry(session, path, csrf, timeout=timeout)
    if result.get("error"):
        print("✗")
        return result, csrf

    upload_id = result.get("id") or result.get("id_str")
    print(f"✓ upload_id={upload_id}")

    if not poll:
        return result, csrf

    print("  ⏳ 等待 Strava 处理", end="", flush=True)
    prog = sw.poll_upload(session, upload_id)

    if prog.get("error"):
        print(f"\n  ✗ 处理失败 ({prog.get('error')}): {prog.get('detail')}")
        return {"error": prog.get("error"), "detail": prog.get("detail"),
                "upload_id": upload_id}, csrf

    activity = prog.get("activity") or {}
    activity_id = activity.get("id") or activity.get("id_str")
    print(f" ✓ activity_id={activity_id}")

    # 设置名称/描述 —— 对应网页上的「保存并查看」
    if name is not None or desc is not None:
        print("  ✎ 设置名称/描述 ", end="", flush=True)
        meta = sw.set_activity_metadata(session, activity, csrf,
                                        name=name, description=desc)
        if meta.get("error") == "csrf_failed":       # token 过期，重取再试一次
            csrf = sw.fetch_csrf(session)
            meta = sw.set_activity_metadata(session, activity, csrf,
                                            name=name, description=desc)
        if meta.get("error"):
            print(f"✗ 活动已创建，但改名称/描述失败: {meta.get('detail')}")
        else:
            print("✓")

    result = dict(result)
    result["activity"] = activity
    result["activity_id"] = activity_id
    return result, csrf


def describe_error(result: dict) -> str:
    err = result.get("error")
    if err == "auth_failed":
        return "认证失败 — cookie 可能已过期"
    if err == "rate_limited":
        return "触发限速 (429)"
    if err == "csrf_failed":
        return "CSRF token 被拒，重取后仍然失败"
    if err == "upload_failed":
        return f"Strava 拒绝了这个文件: {result.get('detail')}"
    if err == "unsupported_type":
        return f"不支持的文件类型 — {result.get('detail')}"
    if err == "missing_file":
        return str(result.get("detail"))
    if err == "timeout":
        return (f"处理超时 — 文件已上传 (upload_id={result.get('upload_id')})，"
                "可以稍后去 Strava 上看看")
    if err == "bad_activity":
        return f"拿到活动信息但缺 id: {result.get('detail')}"
    if err == "metadata_failed":
        return f"设置名称/描述失败: {result.get('detail')}"
    detail = result.get("detail")
    return f"HTTP {result.get('status_code')}" + (f" — {str(detail)[:200]}" if detail else "")


# ── 批量上传 ──────────────────────────────────────────────────────────

def batch_upload(session: requests.Session, file_paths: list[str], csrf: str,
                 name: str = None, desc: str = None,
                 delay: float = 2.0, timeout: int = 120) -> list[dict]:
    """批量上传文件，内置限速延迟。name/desc 会应用到每个活动。"""
    results = []
    total = len(file_paths)
    success = 0
    failed = 0

    print(f"\n{'='*60}")
    print(f"批量上传 {total} 个文件")
    if name is not None:
        print(f"活动名称: {name}")
    if desc is not None:
        print(f"活动描述: {desc}")
    print(f"{'='*60}")

    for i, file_path in enumerate(file_paths, 1):
        print(f"\n[{i}/{total}] {file_path}")

        result, csrf = upload_one(session, file_path, csrf,
                                  name=name, desc=desc, timeout=timeout)

        if result.get("error"):
            if result["error"] == "rate_limited":
                print("  → 触发限速，等待 60 秒后重试...")
                time.sleep(60)
                result, csrf = upload_one(session, file_path, csrf,
                                          name=name, desc=desc, timeout=timeout)

            if result.get("error"):
                msg = describe_error(result)
                print(f"  ✗ {msg}")
                failed += 1
                results.append({"file": file_path, "success": False, "error": result})
                if result["error"] == "auth_failed":
                    print("  → Cookie 已过期，终止批量上传")
                    break
                continue

        success += 1
        activity_id = result.get("activity_id")
        results.append({"file": file_path, "success": True, "result": result})
        if activity_id:
            print(f"  → https://www.strava.com/activities/{activity_id}")

        # 文件间延迟
        if i < total:
            time.sleep(delay)

    print(f"\n{'='*60}")
    print(f"完成: 成功 {success}, 失败 {failed}, 总计 {total}")
    print(f"{'='*60}")

    return results


# ── 主入口 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Strava Cookie 上传器 — 绕过 API 订阅限制上传活动文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python strava_cookie_upload.py ride.fit
  python strava_cookie_upload.py --name "晨骑" --desc "早上的骑行" ride.fit
  python strava_cookie_upload.py *.fit
  python strava_cookie_upload.py --cookie "<cookie值>" ride.fit
  python strava_cookie_upload.py --save-cookie "<cookie值>"

获取 cookie:
  浏览器打开 strava.com 并登录 → F12 → Network → 刷新 →
  右键任意请求 → Copy as cURL → 然后运行: python save_strava_cookie.py
        """,
    )

    parser.add_argument("files", nargs="*", help="要上传的文件路径 (FIT/GPX/TCX)")
    parser.add_argument("--cookie", "-c", help="_strava4_session cookie 值")
    parser.add_argument("--name", "-n", help="活动名称（上传成功后设置）")
    parser.add_argument("--desc", "-d", help="活动描述（上传成功后设置）")
    parser.add_argument("--save-cookie", help="保存 cookie 到 ~/.strava_cookie 并退出")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="批量上传的文件间延迟秒数（默认 2）")
    parser.add_argument("--timeout", type=int, default=120, help="上传超时秒数（默认 120）")

    args = parser.parse_args()

    # 保存 cookie 模式
    if args.save_cookie:
        save_cookie(args.save_cookie)
        return

    # 未提供文件
    if not args.files:
        parser.error("请提供要上传的文件，或使用 --save-cookie 保存 cookie")

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

    # 先校验文件，避免白跑一趟网络
    bad = []
    for f in args.files:
        try:
            sw.detect_data_type(f)
        except ValueError as e:
            bad.append(str(e))
            continue
        if not Path(f).is_file():
            bad.append(f"文件不存在: {f}")
    if bad:
        for b in bad:
            print(f"❌ {b}")
        sys.exit(1)

    # 建会话并取 CSRF token —— 这一步顺带验证 cookie 是否还有效
    session = sw.new_session(cookie)
    try:
        csrf = sw.fetch_csrf(session)
    except sw.AuthError as e:
        print(f"❌ {e}")
        print("   重新跑一次: python save_strava_cookie.py")
        sys.exit(1)
    except (sw.CsrfError, requests.RequestException) as e:
        print(f"❌ 取 CSRF token 失败: {e}")
        sys.exit(1)

    print(f"Cookie: {cookie[:12]}...{cookie[-4:]}")
    print(f"CSRF token: {csrf[:12]}...{csrf[-6:]}")

    # 单文件 vs 批量
    if len(args.files) == 1:
        file_path = args.files[0]
        print(f"\n上传: {file_path}")

        result, _ = upload_one(session, file_path, csrf,
                               name=args.name, desc=args.desc, timeout=args.timeout)

        if result.get("error"):
            print(f"\n❌ {describe_error(result)}")
            if result["error"] == "auth_failed":
                print("   → 重新跑一次: python save_strava_cookie.py")
                sys.exit(1)
            if result["error"] == "rate_limited":
                print("   → 被限速，等几分钟再试")
                sys.exit(2)
            sys.exit(3)

        activity_id = result.get("activity_id")
        print(f"\n✓ 完成! 活动 id: {activity_id}")
        if activity_id:
            print(f"  https://www.strava.com/activities/{activity_id}")
    else:
        results = batch_upload(session, args.files, csrf,
                               name=args.name, desc=args.desc,
                               delay=args.delay, timeout=args.timeout)
        result_file = Path.cwd() / "strava_upload_results.json"
        with result_file.open("w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"结果已保存到: {result_file}")

        if any(not r["success"] for r in results):
            sys.exit(3)


if __name__ == "__main__":
    main()
