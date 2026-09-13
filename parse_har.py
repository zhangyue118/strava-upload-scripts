#!/usr/bin/env python3
"""
解析 HAR 文件，还原上传流程的请求
=================================

针对大文件做了优化：不走 json.load（几百 MB 的 HAR 会把内存撑爆），
而是按条目分块扫描，只提取关心的字段。

用法：
  python parse_har.py strava_upload.har                  # 列出所有 strava.com 请求
  python parse_har.py strava_upload.har --find 关键词     # 定位含关键词的请求（含请求体）
  python parse_har.py strava_upload.har --all            # 不过滤第三方域名

只读本地文件，不发任何请求。cookie 值自动脱敏。
"""

import argparse
import base64
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SKIP_HOSTS = (
    "cloudfront.net", "google", "sentry", "doubleclick", "facebook",
    "cookiebot", "intercom", "googletagmanager", "gstatic",
)

ENTRY_RE = re.compile(rb'"startedDateTime"')
METHOD_RE = re.compile(rb'"method":\s*"(\w+)",\s*"url":\s*"([^"]*)"')
STATUS_RE = re.compile(rb'"status":\s*(\d+)')
MIME_RE = re.compile(rb'"mimeType":\s*"([^"]*)"')


def mask(value: str, keep: int = 6) -> str:
    value = str(value)
    if len(value) <= keep * 2:
        return value
    return f"{value[:keep]}...{value[-4:]}({len(value)}字符)"


def mask_cookie_header(value: str) -> str:
    return re.sub(
        r"([A-Za-z0-9_.\-]+)=([^;]+)",
        lambda m: f"{m.group(1)}={mask(m.group(2), 4)}",
        value,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har")
    ap.add_argument("--find", "-f", help="定位请求体/响应体里含该关键词的请求")
    ap.add_argument("--dump", "-D", help="完整打印 URL 含该子串的请求（头+体+响应）")
    ap.add_argument("--all", action="store_true", help="不过滤第三方域名")
    ap.add_argument("--body", "-b", type=int, default=0, help="同时打印请求体前 N 字节")
    args = ap.parse_args()

    with open(args.har, "rb") as fh:
        data = fh.read()
    print(f"读取 {len(data)} 字节")

    starts = [m.start() for m in ENTRY_RE.finditer(data)]
    print(f"共 {len(starts)} 条请求\n")

    needle = args.find.encode() if args.find else None
    hits = 0

    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(data)
        chunk = data[start:end]

        m = METHOD_RE.search(chunk)
        if not m:
            continue
        method = m.group(1).decode()
        url = m.group(2).decode("utf-8", "replace")

        if "strava.com" not in url:
            continue
        if not args.all and any(h in url for h in SKIP_HOSTS):
            continue

        if args.dump:
            if args.dump in url:
                print(f"[{i}] {method} {url}")
                sm0 = STATUS_RE.search(chunk)
                print(f"     HTTP {sm0.group(1).decode() if sm0 else '?'}")
                # 请求头（挑关心的）
                for hm in re.finditer(rb'"name":\s*"([^"]+)",\s*"value":\s*"([^"]*)"', chunk[:8000]):
                    hn = hm.group(1).decode("utf-8", "replace").lower()
                    hv = hm.group(2).decode("utf-8", "replace")
                    if hn in ("cookie",):
                        print(f"     {hn}: {mask_cookie_header(hv)}")
                    elif hn in ("x-csrf-token", "content-type", "x-requested-with",
                                "referer", "origin", "accept"):
                        print(f"     {hn}: {hv[:130]}")
                # 请求体
                bm = re.search(rb'"postData":\s*\{(.*?)\n\s*\}', chunk, re.S)
                if bm:
                    bt = bm.group(1).decode("utf-8", "replace")
                    bt = bt.replace("\\r\\n", "\n").replace("\\n", "\n").replace('\\"', '"')
                    print(f"     请求体:\n       {bt[:2000]}")
                # 响应体
                rm = re.search(rb'"content":\s*\{.*?"text":\s*"(.*?)"\s*\}', chunk, re.S)
                if rm:
                    rt = rm.group(1).decode("utf-8", "replace")
                    rt = rt.replace("\\r\\n", "\n").replace("\\n", "\n").replace('\\"', '"')
                    print(f"     响应体:\n       {rt[:1500]}")
                print()
            continue

        if needle and needle not in chunk:
            continue
        if not needle and not args.all and not any(
            k in url for k in ("upload", "activities", "/graphql", "/api/")
        ):
            # 默认只列可能的候选，避免刷屏
            continue

        hits += 1
        sm = STATUS_RE.search(chunk)
        status = sm.group(1).decode() if sm else "?"
        print(f"[{hits}] {method} {url}")
        print(f"     HTTP {status}")

        # 请求体
        body_m = re.search(rb'"postData":\s*\{(.{0,4000}?)\}\s*,\s*"', chunk, re.S)
        if body_m:
            body = body_m.group(1)
            text_m = re.search(rb'"text":\s*"(.*)"\s*\}?$', body, re.S)
            raw = text_m.group(1) if text_m else body
            # 转义还原
            try:
                text = raw.decode("utf-8", "replace")
                text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace('\\"', '"')
            except Exception:
                text = str(raw[:300])

            # cookie 脱敏
            text = re.sub(
                r"(?i)(cookie:\s*)([^\n]+)",
                lambda mm: mm.group(1) + mask_cookie_header(mm.group(2)),
                text,
            )
            if needle:
                print(f"     请求体 ({len(text)} 字符):")
                print("       " + text[:2500].replace("\n", "\n       "))
            elif args.body:
                print(f"     请求体: {text[:args.body]}")

        # 响应体
        resp_m = re.search(rb'"content":\s*\{[^}]*?"text":\s*"(.*?)"\s*\}', chunk, re.S)
        if resp_m:
            raw = resp_m.group(1)
            text = raw.decode("utf-8", "replace")
            if len(text) < 1200:
                print(f"     响应体: {text[:600].replace(chr(92) + 'n', ' ')}")
        print()

    if needle and not hits:
        print(f"没找到含 {args.find!r} 的 strava.com 请求")
        print("  → 确认保存前真的改了名称，且 HAR 是那次操作之后导出的")
    print(f"── 命中 {hits} 条 ──")


if __name__ == "__main__":
    main()
