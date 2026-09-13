#!/usr/bin/env python3
"""
诊断：session cookie 在 Strava 各个端点上到底能不能认证
=======================================================

只发 GET 请求，不会上传任何东西。

    python debug_strava_auth.py
"""

import sys
from pathlib import Path

import requests

from get_strava_cookie import COOKIE_NAME
from strava_cookie_upload import USER_AGENT, load_cookie

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROBES = [
    ("网站首页(对照)", "GET", "https://www.strava.com/dashboard"),
    ("API v3 athlete", "GET", "https://www.strava.com/api/v3/athlete"),
    ("旧上传页面", "GET", "https://www.strava.com/upload/select"),
    ("上传页面", "GET", "https://www.strava.com/upload"),
]

# 候选上传端点：空 body POST 只探路径是否存在，不会创建活动
#   404 = 路径不存在 | 401/403 = 存在但拒绝认证 | 400/422 = 存在且认证通过（缺参数）
#   405 = 路径存在但方法不对
CANDIDATE_UPLOAD_PATHS = [
    "/upload",
    "/upload/files",
    "/upload/create",
    "/upload/activity",
    "/upload/save",
    "/uploads",
    "/api/v3/uploads",
]


def main():
    cookie = load_cookie()
    if not cookie:
        print("❌ 没读到 cookie，先跑 save_strava_cookie.py")
        sys.exit(1)
    print(f"cookie: {cookie[:8]}...{cookie[-4:]} ({len(cookie)} 字符)\n")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    for label, method, url in PROBES:
        try:
            r = requests.request(
                method, url, cookies={COOKIE_NAME: cookie},
                headers=headers, timeout=20, allow_redirects=False,
            )
        except requests.RequestException as e:
            print(f"  {label:18} 网络错误: {e}")
            continue

        loc = r.headers.get("Location", "")
        print(f"  {label:18} HTTP {r.status_code}" + (f"  →  {loc}" if loc else ""))

        body = r.text[:300].replace("\n", " ")
        if r.status_code >= 400:
            print(f"      响应: {body}")
        elif "json" in r.headers.get("Content-Type", ""):
            print(f"      响应: {body}")

    print("\n── 候选上传端点探测（空 body POST，只看状态码）──")
    for path in CANDIDATE_UPLOAD_PATHS:
        url = "https://www.strava.com" + path
        try:
            r = requests.post(
                url, cookies={COOKIE_NAME: cookie},
                headers={**headers, "Origin": "https://www.strava.com",
                         "Referer": "https://www.strava.com/upload/select"},
                timeout=20, allow_redirects=False,
            )
        except requests.RequestException as e:
            print(f"  {path:22} 网络错误: {e}")
            continue

        if r.status_code == 404:
            verdict = "路径不存在"
        elif r.status_code in (401, 403):
            verdict = "存在，但拒绝认证"
        elif r.status_code == 405:
            verdict = "路径存在，方法不对"
        elif r.status_code in (400, 422):
            verdict = "★ 存在且认证通过（缺参数）"
        else:
            verdict = "★ 存在"
        print(f"  {path:22} HTTP {r.status_code:3}  {verdict}")

    print("\n参考：404=路径不存在 | 401/403=存在但拒绝认证 | 400/422=认证通过只是缺参数")

    # GraphQL：网页端大量走这个，如果它能认证，就能 introspect 出上传 mutation
    print("\n── GraphQL 探测 ──")
    gql_url = "https://www.strava.com/graphql"
    try:
        r = requests.post(
            gql_url,
            cookies={COOKIE_NAME: cookie},
            headers={**headers, "Content-Type": "application/json",
                     "Origin": "https://www.strava.com"},
            json={"query": "{ __schema { queryType { name } mutationType { name } } }"},
            timeout=20,
        )
        print(f"  HTTP {r.status_code}  {r.text[:400]}")
    except requests.RequestException as e:
        print(f"  网络错误: {e}")


if __name__ == "__main__":
    main()
