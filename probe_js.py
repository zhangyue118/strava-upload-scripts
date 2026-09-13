#!/usr/bin/env python3
"""
诊断：在指定页面里按关键词打印上下文（通用小工具）
==================================================

    python probe_js.py --url https://www.strava.com/athlete/training -n "/activities/"
"""

import argparse
import re
import sys

import requests

from get_strava_cookie import COOKIE_NAME
from strava_cookie_upload import USER_AGENT, load_cookie

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--needle", "-n", action="append", required=True)
    ap.add_argument("--context", "-c", type=int, default=120)
    ap.add_argument("--max", "-m", type=int, default=6)
    ap.add_argument("--regex", "-r", action="store_true", help="把 needle 当正则")
    args = ap.parse_args()

    s = requests.Session()
    cookie = load_cookie()
    if cookie:
        s.cookies.set(COOKIE_NAME, cookie)
    s.headers["User-Agent"] = USER_AGENT

    r = s.get(args.url, timeout=60)
    text = r.text
    print(f"{args.url}\n  HTTP {r.status_code}, {len(text)} 字符\n")

    for needle in args.needle:
        print(f"── {needle!r}")
        n = 0
        for m in re.finditer(needle if args.regex else re.escape(needle), text):
            s0 = max(0, m.start() - args.context)
            e0 = min(len(text), m.end() + args.context)
            print(f"   [{m.start()}] ...{text[s0:e0].replace(chr(10), ' ')}...")
            n += 1
            if n >= args.max:
                break
        if not n:
            print("   (无命中)")
        print()


if __name__ == "__main__":
    main()
