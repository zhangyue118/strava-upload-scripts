#!/usr/bin/env python3
"""
Strava 网页端上传协议 —— 共享模块
==================================

这是整套脚本唯一一处实现上传协议的地方，其他脚本都从这里 import。
（以前 strava_cookie_upload.py 和 strava_watch_upload.py 各抄了一份，
 Strava 改协议后两边同时失效，所以现在抽出来共用。）

协议（2026-09 抓包确认）
------------------------

旧办法走 `POST /api/v3/uploads` + `_strava4_session`，**已经失效**：
/api/v3/* 现在只认 OAuth Bearer access_token，带 session cookie 一律 401
（`{"field":"access_token","code":"invalid"}`）。

现在网页端走三个端点（2026-09 抓 HAR 确认）：

    GET  /upload/select                        → 从 <meta name="csrf-token"> 取 CSRF token
    POST /upload/files                         → multipart 上传，返回 upload_id
    GET  /upload/progress.json?ids[]=<id>      → 轮询处理状态，拿到活动 id
    POST /athlete/training_activities/bulk_update  → 改活动名称/描述（可选）

1) POST /upload/files 的细节：

    头:      X-CSRF-Token: <token>
             X-Requested-With: XMLHttpRequest
             Origin / Referer: https://www.strava.com
    表单:    _method=post
             authenticity_token=<同一个 token>
             files[]=<文件>

   响应（200, application/json）是一个数组：

       [{"id":21297820922,"name":null,"progress":0,
         "workflow":"new","start_date":null,"error":null}]

   注意 id 是 **upload_id，不是活动 id**。活动 id 要轮询才拿得到。

2) GET /upload/progress.json?ids[]=<upload_id> 的响应：

       [{"id":21298050491,"workflow":"success","progress":100, "error":null,
         "activity":{"id":20149224069,"name":"傍晚骑行","type":"VirtualRide",
                     "description":null,"visibility":"everyone",
                     "activity_url":"https://www.strava.com/activities/20149224069", ...}}]

   workflow == "success" 且带 activity 就是处理完了。活动是异步创建的，
   所以要轮询等。

3) POST /athlete/training_activities/bulk_update（JSON body）：

       {"activities":[{"name":"...","description":"...","private_note":"...",
                       "sport_type":"VirtualRide","visibility":"everyone",
                       "id":<activity_id>, ...}]}   →  {"success":true}

   这就是上传页「保存并查看」按钮做的事，也是 --name / --desc 的落点。
   注意它一次能改多个活动（activities 是数组），而且字段是**整体提交**的，
   所以构造 payload 时要带上活动现有的 type / visibility 等，别把它们清掉。

其他：
  · 不需要 data_type —— Strava 自己嗅探文件类型（旧 API 才需要显式传）
  · CSRF token 与会话绑定，每次运行要重新取；批量上传时同一个 token 可复用，
    万一 422 就重取再试。
"""

import re
import time
from pathlib import Path

import requests

BASE = "https://www.strava.com"
SELECT_URL = f"{BASE}/upload/select"
UPLOAD_URL = f"{BASE}/upload/files"
PROGRESS_URL = f"{BASE}/upload/progress.json"
BULK_UPDATE_URL = f"{BASE}/athlete/training_activities/bulk_update"
COOKIE_NAME = "_strava4_session"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CSRF_RE = re.compile(
    r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', re.I
)

SUPPORTED_TYPES = {
    ".fit": "fit",
    ".fit.gz": "fit.gz",
    ".gpx": "gpx",
    ".gpx.gz": "gpx.gz",
    ".tcx": "tcx",
    ".tcx.gz": "tcx.gz",
}


class StravaError(RuntimeError):
    """协议层的可预期错误"""


class AuthError(StravaError):
    """cookie 失效 / 未登录"""


class CsrfError(StravaError):
    """页面里取不到 CSRF token，或 token 被拒"""


def detect_data_type(file_path: str | Path) -> str:
    """校验扩展名。注意：新协议不再需要把类型发给 Strava，这里只用来挡掉不支持的文件。"""
    name = str(file_path).lower()
    if name.endswith(".fit.gz"):
        return "fit.gz"
    if name.endswith(".gpx.gz"):
        return "gpx.gz"
    if name.endswith(".tcx.gz"):
        return "tcx.gz"
    suffix = Path(file_path).suffix.lower()
    data_type = SUPPORTED_TYPES.get(suffix)
    if not data_type:
        raise ValueError(
            f"不支持的文件类型: {suffix}，支持的类型: {', '.join(SUPPORTED_TYPES)}"
        )
    return data_type


def new_session(cookie: str) -> requests.Session:
    """建一个带 session cookie 的会话。CSRF token 是绑定会话的，所以整个批次共用它。"""
    s = requests.Session()
    s.cookies.set(COOKIE_NAME, cookie)
    s.headers["User-Agent"] = USER_AGENT
    return s


def fetch_csrf(session: requests.Session, timeout: int = 30) -> str:
    """取上传页面的 CSRF token。顺带能验证 cookie 是否还有效。"""
    resp = session.get(SELECT_URL, timeout=timeout)

    if resp.status_code in (401, 403):
        raise AuthError(f"打开上传页面被拒 (HTTP {resp.status_code})，cookie 可能已过期")

    if "/login" in resp.url:
        raise AuthError("被重定向到登录页，cookie 已失效")

    m = CSRF_RE.search(resp.text)
    if not m:
        raise CsrfError(
            "上传页面里没有 csrf-token meta 标签 —— "
            "可能页面结构变了，或者 cookie 对应的不是已登录会话"
        )
    return m.group(1)


def upload_file(
    session: requests.Session,
    file_path: str | Path,
    csrf: str,
    timeout: int = 120,
) -> dict:
    """
    上传单个文件，返回 Strava 的 upload 对象。

    成功: {"id": ..., "progress": 0, "workflow": "new", "error": None, ...}
    失败: {"error": "auth_failed" | "rate_limited" | "csrf_failed" | "http_error", ...}

    返回值的形状沿用旧脚本的约定，调用方照旧用 .get("error") 判断。
    """
    path = Path(file_path)

    try:
        detect_data_type(path)      # 挡掉不支持的扩展名
    except ValueError as e:
        return {"error": "unsupported_type", "detail": str(e)}

    if not path.is_file():
        return {"error": "missing_file", "detail": f"文件不存在: {path}"}

    headers = {
        "Accept": "text/plain, */*; q=0.01",
        "Origin": BASE,
        "Referer": SELECT_URL,
        "X-CSRF-Token": csrf,
        "X-Requested-With": "XMLHttpRequest",
    }
    data = {"_method": "post", "authenticity_token": csrf}

    with path.open("rb") as fh:
        files = {"files[]": (path.name, fh, "application/octet-stream")}
        resp = session.post(
            UPLOAD_URL, data=data, files=files, headers=headers, timeout=timeout
        )

    if resp.status_code == 429:
        return {"error": "rate_limited", "status_code": 429}

    if resp.status_code in (401, 403):
        return {"error": "auth_failed", "status_code": resp.status_code}

    if resp.status_code == 422:
        # Rails 的 InvalidAuthenticityToken —— token 过期，调用方重取后重试
        return {"error": "csrf_failed", "status_code": 422, "detail": resp.text[:200]}

    if not resp.ok:
        return {
            "error": "http_error",
            "status_code": resp.status_code,
            "detail": resp.text[:200],
        }

    try:
        payload = resp.json()
    except ValueError:
        return {"error": "http_error", "status_code": resp.status_code,
                "detail": f"响应不是 JSON: {resp.text[:200]}"}

    # 正常是一个数组，每个元素对应一个上传的文件
    if not isinstance(payload, list) or not payload:
        return {"error": "http_error", "status_code": resp.status_code,
                "detail": f"响应结构异常: {str(payload)[:200]}"}

    item = payload[0]
    if item.get("error"):
        return {"error": "upload_failed", "status_code": resp.status_code,
                "detail": item.get("error"), "raw": item}
    if not (item.get("id") or item.get("id_str")):
        return {"error": "http_error", "status_code": resp.status_code,
                "detail": f"响应里没有 id: {str(item)[:200]}"}

    return item


def upload_with_retry(
    session: requests.Session,
    file_path: str | Path,
    csrf: str,
    timeout: int = 120,
) -> tuple[dict, str]:
    """
    上传并在 CSRF 失效时自动重取 token 重试一次。

    返回 (结果, 可能更新过的 csrf)，方便同一个批次里继续复用新 token。
    """
    result = upload_file(session, file_path, csrf, timeout=timeout)
    if result.get("error") != "csrf_failed":
        return result, csrf

    csrf = fetch_csrf(session)
    return upload_file(session, file_path, csrf, timeout=timeout), csrf


def poll_upload(
    session: requests.Session,
    upload_id,
    poll_interval: float = 1.5,
    max_wait: float = 120,
) -> dict:
    """
    轮询 /upload/progress.json 直到处理完成。

    成功: progress 对象，含 "activity"（里面有活动 id、名称、类型等）
    失败: {"error": "upload_failed" | "timeout" | "http_error", ...}

    轮询间隔和总时长沿用旧脚本的默认值（1.5 秒 / 120 秒）。
    """
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": SELECT_URL,
    }
    elapsed = 0.0
    last = None

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        try:
            resp = session.get(PROGRESS_URL, params={"ids[]": upload_id},
                               headers=headers, timeout=20)
        except requests.RequestException as e:
            return {"error": "http_error", "detail": f"轮询请求失败: {e}"}

        if resp.status_code == 429:
            time.sleep(15)
            elapsed += 15
            continue

        if not resp.ok:
            return {"error": "http_error", "status_code": resp.status_code,
                    "detail": resp.text[:200]}

        try:
            payload = resp.json()
        except ValueError:
            return {"error": "http_error",
                    "detail": f"轮询响应不是 JSON: {resp.text[:200]}"}

        if not payload:
            continue                    # 还没登记进来，继续等

        item = payload[0]
        last = item

        if item.get("error"):
            return {"error": "upload_failed", "detail": item["error"], "raw": item}

        workflow = item.get("workflow")
        if workflow == "success" and item.get("activity"):
            return item
        if workflow in ("error", "failed", "invalid"):
            return {"error": "upload_failed", "detail": f"workflow={workflow}",
                    "raw": item}

    return {"error": "timeout", "detail": f"等待 {max_wait} 秒仍未处理完", "raw": last}


def set_activity_metadata(
    session: requests.Session,
    activity: dict,
    csrf: str,
    name: str = None,
    description: str = None,
    private_note: str = None,
    timeout: int = 30,
) -> dict:
    """
    设置活动名称 / 描述 / 私人备注 —— 对应上传页的「保存并查看」。

    参数:
        activity: poll_upload() 返回对象里的 "activity" 字典（要含 id 和现有属性）
        name / description / private_note: 传 None 表示保持原值

    返回: {"ok": True, "id": ...} 或 {"error": ..., "detail": ...}

    注意：这个端点是**整体提交**的，payload 里带上活动现有的 sport_type /
    visibility / trainer 等，否则可能被清掉。所以必须传进真实的 activity 对象，
    而不是自己拼一个只有 id 的。
    """
    activity_id = activity.get("id") or activity.get("id_str")
    if not activity_id:
        return {"error": "bad_activity",
                "detail": f"活动对象里没有 id: {str(activity)[:200]}"}

    entry = {
        "name": name if name is not None else (activity.get("name") or ""),
        "description": (description if description is not None
                        else activity.get("description")),
        "private_note": (private_note if private_note is not None
                         else (activity.get("private_note") or "")),
        "activity[tags][]": "",
        "activity[trainer]": "1" if activity.get("trainer") else "0",
        "perceived_exertion": "",
        "visibility": activity.get("visibility") or "everyone",
        "commute": bool(activity.get("commute")),
        "trainer": bool(activity.get("trainer")),
        "sport_type": activity.get("type") or activity.get("sport_type") or "Ride",
        "bike_id": activity.get("bike_id"),
        "athlete_gear_id": activity.get("athlete_gear_id"),
        "selected_polyline_style": "default",
        "id": activity_id,
    }

    headers = {
        "Accept": ("text/javascript, application/javascript, "
                   "application/ecmascript, application/x-ecmascript"),
        "Content-Type": "application/json; charset=utf-8",
        "Origin": BASE,
        "Referer": SELECT_URL,
        "X-CSRF-Token": csrf,
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        resp = session.post(BULK_UPDATE_URL, json={"activities": [entry]},
                            headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return {"error": "http_error", "detail": f"请求失败: {e}"}

    if resp.status_code == 422:
        return {"error": "csrf_failed", "status_code": 422, "detail": resp.text[:200]}
    if resp.status_code in (401, 403):
        return {"error": "auth_failed", "status_code": resp.status_code}
    if not resp.ok:
        return {"error": "http_error", "status_code": resp.status_code,
                "detail": resp.text[:200]}

    try:
        data = resp.json()
    except ValueError:
        return {"error": "http_error",
                "detail": f"响应不是 JSON: {resp.text[:200]}"}

    if not data.get("success"):
        return {"error": "metadata_failed", "detail": str(data)[:200]}

    return {"ok": True, "id": activity_id}
