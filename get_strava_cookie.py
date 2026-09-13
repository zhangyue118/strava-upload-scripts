#!/usr/bin/env python3
"""
从 Chromium 系浏览器（Edge / Chrome）提取 Strava 的 _strava4_session cookie
=============================================================================

原理：
  Chromium 把 cookie 存在 SQLite 里，值用「主密钥 + AES-256-GCM」加密；
  主密钥存在 Local State 的 os_crypt.encrypted_key 中，再用 Windows DPAPI
  （当前登录用户凭据）加一层。两者都在本机、当前用户权限内，所以可以自动解开。

用法：
  python get_strava_cookie.py                    # 自动找 Edge，验证后写入 ~/.strava_cookie
  python get_strava_cookie.py --browser chrome   # 换成 Chrome
  python get_strava_cookie.py --list             # 只列出候选，不写入
  python get_strava_cookie.py --show             # 顺便打印完整 cookie 值
  python get_strava_cookie.py --no-verify        # 跳过在线验证

重要：
  浏览器运行时会**独占锁定** Cookies 数据库，必须先完全退出浏览器
  （注意 Edge 的「启动增强」会让它驻留后台，见 --check 提示）。

依赖：pip install requests cryptography
"""

import argparse
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

import requests

# Windows 控制台默认 GBK，重设为 UTF-8，否则 emoji / 部分中文会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

COOKIE_NAME = "_strava4_session"
VERIFY_URL = "https://www.strava.com/dashboard"
ATHLETE_URL = "https://www.strava.com/api/v3/athlete"

BROWSERS = {
    "edge": Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data",
    "chrome": Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
    "brave": Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "User Data",
}


# ── DPAPI（用 stdlib ctypes 调 crypt32，不需要 pywin32） ────────────────

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    """用当前用户的 DPAPI 凭据解密一段数据"""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ── Chromium cookie 解密 ──────────────────────────────────────────────

def get_master_key(user_data: Path) -> bytes:
    """从 Local State 取出并解开 AES 主密钥"""
    local_state = user_data / "Local State"
    if not local_state.exists():
        raise FileNotFoundError(f"找不到 {local_state}")

    with local_state.open(encoding="utf-8") as f:
        state = json.load(f)

    os_crypt = state.get("os_crypt", {})
    b64_key = os_crypt.get("encrypted_key")
    if not b64_key:
        raise ValueError("Local State 里没有 os_crypt.encrypted_key")

    import base64
    blob = base64.b64decode(b64_key)
    if not blob.startswith(b"DPAPI"):
        raise ValueError(f"不认识的主密钥格式: {blob[:5]!r}")
    return dpapi_unprotect(blob[5:])          # 去掉 "DPAPI" 前缀再解


def decrypt_value(enc: bytes, key: bytes) -> str:
    """解密单条 cookie 的 encrypted_value"""
    if not enc:
        return ""

    version = enc[:3]

    if version in (b"v10", b"v11"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce, payload = enc[3:15], enc[15:]
        try:                                    # Chrome/Edge 80+ : AES-256-GCM
            return AESGCM(key).decrypt(nonce, payload, None).decode("utf-8", "replace")
        except Exception:
            # 更老的 AES-256-CBC，IV 固定为 16 个空格
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
            plain = decryptor.update(enc[3:]) + decryptor.finalize()
            pad = plain[-1]                     # 去 PKCS7 padding
            return plain[:-pad].decode("utf-8", "replace")

    if version == b"v20":
        raise RuntimeError(
            "这条 cookie 用 v20（App-Bound Encryption）加密，"
            "密钥由浏览器的提权服务保管，脱离浏览器解不开。\n"
            "      → 请改用 --list 之外的手动方式（见脚本末尾说明）"
        )

    # 没有版本前缀的老格式，直接 DPAPI
    return dpapi_unprotect(enc).decode("utf-8", "replace")


# ── 读取 cookie 库 ────────────────────────────────────────────────────

def list_profiles(user_data: Path) -> list[tuple[str, str]]:
    """返回 [(目录名, 显示名), ...]"""
    info = {}
    local_state = user_data / "Local State"
    if local_state.exists():
        try:
            with local_state.open(encoding="utf-8") as f:
                info = json.load(f).get("profile", {}).get("info_cache", {})
        except Exception:
            pass

    if info:
        return [(d, v.get("name", d)) for d, v in info.items()]

    # 兜底：扫目录
    return [(p.name, p.name) for p in user_data.iterdir()
            if p.is_dir() and (p / "Network" / "Cookies").exists()]


def snapshot_cookies_db(user_data: Path, profile: str) -> Path:
    """把锁着的 Cookies 库复制到临时文件，返回临时路径"""
    src = user_data / profile / "Network" / "Cookies"
    if not src.exists():
        raise FileNotFoundError(f"找不到 {src}")

    tmp = Path(tempfile.gettempdir()) / f"strava_ck_{profile.replace(' ', '_')}.db"
    try:
        shutil.copy2(src, tmp)
    except PermissionError as e:
        raise PermissionError(
            f"读不了 Cookies 数据库：{e}\n"
            "      → 浏览器正在运行并独占了该文件，请先**完全退出浏览器**再试。\n"
            "      → Edge 的「启动增强 / Startup boost」会让它驻留后台，"
            "关掉所有窗口后在任务管理器里确认没有 msedge.exe。"
        ) from e
    return tmp


def chromium_time(expires_utc: int) -> str:
    """Chromium 时间戳（1601-01-01 起的微秒）→ 可读字符串"""
    if not expires_utc:
        return "session"
    dt = datetime(1601, 1, 1) + timedelta(microseconds=expires_utc)
    if dt > datetime(9999, 1, 1):
        return "never"
    return dt.strftime("%Y-%m-%d %H:%M")


def read_candidates(user_data: Path, profile: str, cookie_name: str) -> list[dict]:
    """从某个 profile 里读出所有匹配的 cookie 候选"""
    key = get_master_key(user_data)
    db = snapshot_cookies_db(user_data, profile)

    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT host_key, name, encrypted_value, value, expires_utc, path "
            "FROM cookies WHERE name = ? AND host_key LIKE '%strava%' "
            "ORDER BY expires_utc DESC",
            (cookie_name,),
        ).fetchall()
    finally:
        con.close()
        db.unlink(missing_ok=True)

    out = []
    for host, name, enc, plain, expires, path in rows:
        try:
            value = plain or decrypt_value(enc, key)
        except Exception as e:
            out.append({"profile": profile, "host": host, "value": None,
                        "expires": chromium_time(expires), "path": path, "error": str(e)})
            continue
        if value:
            out.append({"profile": profile, "host": host, "value": value,
                        "expires": chromium_time(expires), "path": path, "error": None})
    return out


# ── 验证 ──────────────────────────────────────────────────────────────

def verify(cookie: str) -> tuple[bool, str]:
    """拿 cookie 访问 Strava，确认是否真的登录有效"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
    }
    try:
        resp = requests.get(VERIFY_URL, cookies={COOKIE_NAME: cookie},
                            headers=headers, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"网络错误: {e}"

    if "/login" in resp.url or "login" in resp.url.split("/")[3:4]:
        return False, "被重定向到登录页 — cookie 无效或已过期"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    # 顺便取一下运动员名字，确认是哪个账号。
    # 注意：/api/v3/athlete 现在只认 OAuth Bearer token，带 session cookie 必然
    # 401 —— 所以 who 基本永远是空的，这里静默跳过。登录有效性由上面的
    # /dashboard 跳转判断，那个是准的。
    who = ""
    try:
        r = requests.get(ATHLETE_URL, cookies={COOKIE_NAME: cookie},
                         headers=headers, timeout=15)
        if r.ok:
            a = r.json()
            who = f"（{a.get('firstname','')} {a.get('lastname','')} / id={a.get('id')}）"
    except Exception:
        pass

    return True, f"登录有效 {who}".strip()


def mask(value: str) -> str:
    if len(value) <= 20:
        return value[:4] + "..." + value[-4:]
    return f"{value[:12]}...{value[-4:]} ({len(value)} 字符)"


# ── 主入口 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="从 Edge / Chrome 提取 Strava session cookie",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python get_strava_cookie.py                 # Edge → 验证 → 写入 ~/.strava_cookie
  python get_strava_cookie.py --show          # 同时打印完整值，方便手动复制
  python get_strava_cookie.py --list          # 只看找到什么，不验证不写入
  python get_strava_cookie.py --no-save       # 只验证并打印，不落盘

手动方式（脚本搞不定时的兜底）:
  1. 浏览器打开 https://www.strava.com 并确认已登录
  2. F12 → Application → Cookies → https://www.strava.com
  3. 找到 _strava4_session，双击 Value 全选复制
  4. python strava_cookie_upload.py --cookie "粘贴的值" ride.fit
        """,
    )
    parser.add_argument("--browser", "-b", default="edge",
                        choices=sorted(BROWSERS), help="浏览器（默认 edge）")
    parser.add_argument("--user-data-dir", help="手动指定 User Data 目录")
    parser.add_argument("--profile", "-p", help="指定 profile 目录名，如 'Default'、'Profile 1'")
    parser.add_argument("--cookie-name", default=COOKIE_NAME, help=f"cookie 名（默认 {COOKIE_NAME}）")
    parser.add_argument("--out", help="保存路径（默认 ~/.strava_cookie）")
    parser.add_argument("--list", action="store_true", help="只列出候选，不验证不写入")
    parser.add_argument("--show", action="store_true", help="打印完整 cookie 值")
    parser.add_argument("--no-verify", action="store_true", help="跳过在线验证")
    parser.add_argument("--no-save", action="store_true", help="不写入文件")

    args = parser.parse_args()

    user_data = Path(args.user_data_dir) if args.user_data_dir else BROWSERS[args.browser]
    if not user_data.exists():
        print(f"❌ 找不到 {args.browser} 的 User Data 目录: {user_data}")
        sys.exit(1)

    print(f"浏览器数据目录: {user_data}")

    profiles = list_profiles(user_data)
    if args.profile:
        profiles = [p for p in profiles if p[0] == args.profile]
        if not profiles:
            print(f"❌ 没有名为 {args.profile!r} 的 profile")
            sys.exit(1)

    # 逐个 profile 收集候选
    candidates: list[dict] = []
    errors: list[str] = []
    for dir_name, display in profiles:
        try:
            found = read_candidates(user_data, dir_name, args.cookie_name)
        except (PermissionError, FileNotFoundError) as e:
            errors.append(f"[{display}] {e}")
            continue
        except Exception as e:
            errors.append(f"[{display}] {e}")
            continue
        for c in found:
            c["display"] = display
        candidates.extend(found)

    if errors:
        print()
        for e in errors:
            print(f"⚠ {e}")

    if not candidates:
        print(f"\n❌ 没有找到 {args.cookie_name} 的 cookie")
        print()
        print("   本机实测过这个组合，多半是下面两个原因（都不是你操作的问题）：")
        print("     ① 它是**会话 cookie**：Edge 默认「启动时打开新标签页」= 不恢复会话，")
        print("        Chromium 会在退出时删掉所有会话 cookie，所以磁盘上根本没有它。")
        print("     ② 就算它被持久化了，这个 Edge 写的也是 **v20 App-Bound Encryption**，")
        print("        密钥由浏览器提权服务保管，脱离浏览器解不开。")
        print()
        print(f"   → 改用剪贴板方案，不影响浏览器运行：")
        print(f"     python save_strava_cookie.py        # 先把 cookie 复制到剪贴板")
        sys.exit(1)

    print(f"\n找到 {len(candidates)} 个候选:")
    for i, c in enumerate(candidates, 1):
        mark = "✗" if c["error"] else "·"
        val = mask(c["value"]) if c["value"] else f"(解密失败) {c['error']}"
        print(f"  {mark} [{i}] {c['display']} | {c['host']}{c['path']} | "
              f"过期: {c['expires']} | {val}")

    if args.list:
        return

    # 验证：优先挑最新的那个，不行再试其它
    chosen = None
    if args.no_verify:
        chosen = next((c for c in candidates if c["value"]), None)
    else:
        without_err = [c for c in candidates if c["value"]]
        if len(without_err) > 1:
            print(f"\n验证中（{len(without_err)} 个候选）...")
        for c in without_err:
            ok, msg = verify(c["value"])
            print(f"  {'✓' if ok else '✗'} {c['display']} | {c['host']}: {msg}")
            if ok and chosen is None:
                chosen = c

    if not chosen:
        print("\n❌ 没有可用的 cookie")
        print("   → 去浏览器重新登录一次 strava.com，然后退出浏览器再跑本脚本")
        sys.exit(1)

    print(f"\n✓ 选用: {chosen['display']} | {chosen['host']} | 过期: {chosen['expires']}")

    if args.show:
        print(f"\n完整 cookie:\n{chosen['value']}\n")

    if not args.no_save:
        out = Path(args.out) if args.out else Path.home() / ".strava_cookie"
        out.write_text(chosen["value"], encoding="utf-8")
        print(f"✓ 已写入 {out}")
        print("  现在可以直接跑: python strava_cookie_upload.py ride.fit")


if __name__ == "__main__":
    main()
