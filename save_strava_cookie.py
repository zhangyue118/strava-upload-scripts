#!/usr/bin/env python3
"""
从剪贴板保存 Strava session cookie
==================================

配合 get_strava_cookie.py 的手动兜底方案用：
在 Edge 里复制 cookie（裸值，或整行 Cookie 请求头都行），然后跑：

    python save_strava_cookie.py

它会读剪贴板 → 自动挑出 _strava4_session → 在线验证 → 写入 ~/.strava_cookie。
好处是 cookie 值不用经过剪贴板以外的任何地方（不会进聊天记录）。

    python save_strava_cookie.py --show     # 顺便打印完整值
    python save_strava_cookie.py --no-verify
    python save_strava_cookie.py "直接传值"  # 不用剪贴板

依赖：pip install requests
"""

import argparse
import re
import sys
from pathlib import Path

from get_strava_cookie import COOKIE_NAME, mask, verify


def read_clipboard() -> str:
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    finally:
        root.destroy()


def extract(text: str) -> str | None:
    """从剪贴板内容里挑出 _strava4_session 的值"""
    text = text.strip().strip('"\'')

    if not text:
        return None

    # 情况 1：整行 Cookie 请求头，或 "_strava4_session=xxx;"
    m = re.search(rf"{re.escape(COOKIE_NAME)}\s*=\s*([^;\s\"']+)", text, re.IGNORECASE)
    if m:
        return m.group(1)

    # 情况 2：裸值（没有等号、分号、空白，且长度像 cookie）
    if not re.search(r"[=;\s]", text) and len(text) >= 20:
        return text

    return None


def main():
    parser = argparse.ArgumentParser(description="从剪贴板保存 Strava cookie")
    parser.add_argument("value", nargs="?", help="直接传 cookie 值（不用剪贴板）")
    parser.add_argument("--out", help="保存路径（默认 ~/.strava_cookie）")
    parser.add_argument("--show", action="store_true", help="打印完整值")
    parser.add_argument("--no-verify", action="store_true", help="跳过在线验证")
    args = parser.parse_args()

    if args.value:
        raw = args.value
        source = "命令行参数"
    else:
        raw = read_clipboard()
        source = "剪贴板"

    cookie = extract(raw)
    if not cookie:
        print(f"❌ 没能从{source}里识别出 {COOKIE_NAME}")
        print(f"   {source}内容（前 120 字）: {raw[:120]!r}")
        print()
        print("   应该复制的是下面两者之一：")
        print(f"     · 裸值:      e32a1b2c...（约 350 字符）")
        print(f"     · 整行:      {COOKIE_NAME}=e32a1b2c...; 其它cookie=xxx")
        sys.exit(1)

    print(f"从{source}识别到 {COOKIE_NAME}: {mask(cookie)}")

    if not args.no_verify:
        print("验证中...")
        ok, msg = verify(cookie)
        print(f"  {'✓' if ok else '✗'} {msg}")
        if not ok:
            print("\n❌ 验证失败，没有写入。")
            print("   → 确认浏览器里 strava.com 是登录状态，重新复制一次完整值")
            sys.exit(1)
    else:
        print("（已跳过验证）")

    if args.show:
        print(f"\n完整 cookie:\n{cookie}\n")

    out = Path(args.out) if args.out else Path.home() / ".strava_cookie"
    out.write_text(cookie, encoding="utf-8")
    print(f"\n✓ 已写入 {out}")
    print("  现在可以直接跑: python strava_cookie_upload.py ride.fit")


if __name__ == "__main__":
    main()
