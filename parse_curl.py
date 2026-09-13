#!/usr/bin/env python3
"""
诊断：解析「Copy as cURL」捕获的请求，还原出端点 / 头 / 表单字段
================================================================

用途：在浏览器里真实操作一次（比如上传），把请求以 cURL 格式复制到剪贴板，
然后跑本脚本，它会把请求拆解成结构化信息，写到 captured_upload.json，
并打印一份脱敏摘要（cookie 值只显示首尾）。

    python parse_curl.py            # 从剪贴板读
    python parse_curl.py -f x.txt   # 从文件读

只做解析，不发任何请求。
"""

import argparse
import json
import re
import shlex
import sys

from get_strava_cookie import COOKIE_NAME

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

OUT = "captured_upload.json"


def read_clipboard() -> str:
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    finally:
        root.destroy()


def mask_cookie_header(value: str) -> str:
    """把 Cookie 请求头里的值脱敏，保留结构"""
    def repl(m):
        v = m.group(2)
        shown = v if len(v) <= 10 else f"{v[:4]}...{v[-4]}({len(v)}字符)"
        return f"{m.group(1)}={shown}"
    return re.sub(r"([A-Za-z0-9_.\-]+)=([^;]+)", repl, value)


def looks_like_bare_cookie(text: str) -> bool:
    """整段都是裸 cookie 值（不是 cURL 命令）"""
    t = text.strip().strip("\"'")
    return bool(t) and not re.search(r"[\s=;]", t) and len(t) >= 20


def parse_curl(text: str) -> dict:
    # cURL 从 Windows 复制过来可能是 cmd 风格（^ 续行）或 PowerShell 反引号
    text = text.replace("^\n", " ").replace("`\n", " ").replace("\\\n", " ")

    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()

    req = {"method": None, "url": None, "headers": {}, "form": {}, "data": None,
           "cookies": None, "raw_len": len(text)}

    i = 0
    while i < len(parts):
        p = parts[i]

        if p in ("-X", "--request") and i + 1 < len(parts):
            req["method"] = parts[i + 1]; i += 2; continue
        if p in ("-H", "--header") and i + 1 < len(parts):
            h = parts[i + 1]
            if ":" in h:
                k, v = h.split(":", 1)
                k, v = k.strip(), v.strip()
                req["headers"][k] = mask_cookie_header(v) if k.lower() == "cookie" else v
            i += 2; continue
        if p in ("-F", "--form", "--form-string") and i + 1 < len(parts):
            f = parts[i + 1]
            if "=" in f:
                k, v = f.split("=", 1)
                # 文件字段：file=@/path/to/x.fit;type=...
                m = re.match(r"@([^;]+)(;.*)?$", v)
                if m:
                    req["form"][k] = {"type": "file", "path": m.group(1),
                                      "extra": (m.group(2) or "").lstrip(";")}
                else:
                    req["form"][k] = {"type": "literal", "value": v[:200]}
            i += 2; continue
        if p in ("-b", "--cookie") and i + 1 < len(parts):
            req["cookies"] = mask_cookie_header(parts[i + 1]); i += 2; continue
        if p in ("--data", "--data-raw", "--data-binary", "--data-urlencode") and i + 1 < len(parts):
            # body 只截断，不脱敏（脱敏会把 multipart 正文改成乱码）
            req["data"] = parts[i + 1][:600]; i += 2; continue
        if p in ("--url",) and i + 1 < len(parts):
            req["url"] = parts[i + 1]; i += 2; continue

        if p.startswith(("http://", "https://")) and not req["url"]:
            req["url"] = p
        i += 1

    if not req["method"]:
        req["method"] = "POST" if (req["form"] or req["data"]) else "GET"
    return req


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", help="从文件读，不用剪贴板")
    ap.add_argument("-o", "--out", default=OUT)
    args = ap.parse_args()

    text = open(args.file, encoding="utf-8").read() if args.file else read_clipboard()

    if looks_like_bare_cookie(text):
        print(f"剪贴板里是裸 cookie 值（{len(text.strip())} 字符），不是 cURL 命令。")
        print(f"   → 要存 cookie 请用: python save_strava_cookie.py")
        print(f"   → 这里需要的是在 Network 面板右键那个**上传请求** → Copy as cURL")
        sys.exit(1)

    if "curl" not in text[:200].lower():
        print("⚠ 剪贴板内容看起来不是 cURL 命令，仍尝试解析。")
        print(f"   前 120 字: {text[:120]!r}\n")

    req = parse_curl(text)

    print("═══ 解析结果 ═══")
    print(f"  method : {req['method']}")
    print(f"  url    : {req['url']}")
    if req["cookies"]:
        print(f"  cookies: {req['cookies'][:160]}")
    print(f"  headers ({len(req['headers'])}):")
    for k, v in req["headers"].items():
        print(f"     {k}: {str(v)[:120]}")
    if req["form"]:
        print(f"  form ({len(req['form'])}):")
        for k, v in req["form"].items():
            print(f"     {k} = {v}")
    if req["data"]:
        print(f"  data   : {req['data'][:300]}")
    if not req["form"] and not req["data"]:
        print("  (无 body)")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2, ensure_ascii=False)
    print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    main()
