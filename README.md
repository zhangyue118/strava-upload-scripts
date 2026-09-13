# Strava Upload Scripts — 绕过 API 订阅限制

Strava 2024年底收紧了 API 政策，开发者必须持有付费订阅（$11.99/月）才能创建 API 应用。
这些脚本利用 **浏览器 session cookie** 模拟网页端上传行为，完全绕过 API 订阅要求。

## 原理

用 session cookie `_strava4_session` 认证，复现网页端的上传流程：

```
浏览器登录 → 拿 _strava4_session
  → GET  /upload/select                          取 CSRF token
  → POST /upload/files                           上传，返回 upload_id
  → GET  /upload/progress.json?ids[]=<id>        轮询，拿到活动 id
  → POST /athlete/training_activities/bulk_update 设置名称/描述（可选）
```

> ⚠️ **协议在 2026-09 变过。** 旧办法走 `POST /api/v3/uploads`，
> 现在 `/api/v3/*` 只认 OAuth Bearer token，带 session cookie 一律返回
> `401 {"field":"access_token","code":"invalid"}`。所以旧脚本会"认证失败"，
> 那不是 cookie 的问题。完整的协议说明见 `strava_web.py` 的模块文档。

注意上传返回的 `id` 是 **upload_id，不是活动 id**；活动 id 要轮询
`progress.json` 才拿得到。活动名也不是上传接口设的，而是「保存并查看」
那一步调 `bulk_update` 设的。

## 脚本一览

| 脚本 | 用途 | 适用场景 |
|------|------|---------|
| `启动界面.bat` | **双击启动图形界面** | 推荐入口，发给别人也用这个 |
| `strava_gui.py` | 界面本体 | 开发排查时用（`python strava_gui.py`） |
| `strava_gui.pyw` | 同上，走 `pythonw` | 仅当 `.pyw` 有文件关联时可用，见下 |
| `strava_web.py` | **上传协议实现（共享模块）** | 不直接运行；其他脚本 import 它 |
| `strava_cookie_upload.py` | 手动上传、批量上传 | 偶尔上传几个文件 |
| `strava_watch_upload.py` | 自动监听文件夹、持续同步 | 码表/手表自动同步后自动上传 |
| `save_strava_cookie.py` | 从剪贴板保存 + 验证 cookie | 命令行下获取 cookie |
| `get_strava_cookie.py` | 自动解密浏览器 cookie 库 | 旧版浏览器 / Chrome，新版 Edge 上无效 |

> 上传协议只实现一处（`strava_web.py`）。以前两个上传脚本各抄了一份，
> Strava 改协议时两边同时失效，所以现在抽出来共用。

## 安装

```bash
pip install -r requirements.txt
```

## 使用

### 第一步：获取 Cookie

推荐用剪贴板脚本（自动验证 + 落盘，值不经过聊天/日志）：

1. 浏览器打开 https://www.strava.com 并登录
2. `F12` → **Network** 标签 → `F5` 刷新
3. 点任意一条 strava.com 请求 → 右键 → **Copy → Copy as cURL (bash)**
4. 运行：
   ```bash
   python save_strava_cookie.py
   ```

> ⚠️ **别用 Application 面板抄 Value** —— 那个单元格会截断长值，而且在某些
> Edge 版本上 `_strava4_session` 是 HttpOnly，行为不一致。`Copy as cURL` 复制的
> 是整个请求，Cookie 请求头完整无截断。
>
> cookie 值是 **32 位随机串**（形如 `4s4vnjbp3eoq...ovna`），不是很长的字符串。

手动指定也可以：

```bash
python strava_cookie_upload.py --save-cookie "<你的cookie值>"
```

#### 为什么不能用脚本自动读浏览器 cookie

`get_strava_cookie.py` 实现了这条路（解密 Edge/Chrome 的 SQLite cookie 库），
但在较新的 Edge 上**必然失败**，两个独立原因：

1. `_strava4_session` 是**会话 cookie**。Edge 默认「启动时打开新标签页」= 不恢复会话，
   Chromium 会在退出时删掉所有会话 cookie，磁盘上根本没有它。
2. 就算持久化了，新 Edge 写入的是 **v20 App-Bound Encryption**，密钥由浏览器
   提权服务保管，用 Windows DPAPI 解不开（这正是它设计来阻止外部读取 cookie 的机制）。

保留该脚本仅为兼容旧版浏览器 / Chrome 场景。

### 第二步：上传

#### strava_cookie_upload.py — 手动上传

```bash
# 单文件
python strava_cookie_upload.py ride.fit

# 带活动名称 / 描述
python strava_cookie_upload.py --name "晨骑" --desc "早上的骑行" ride.fit

# 批量上传（--name/--desc 会应用到每个活动）
python strava_cookie_upload.py *.fit

# 指定 cookie
python strava_cookie_upload.py --cookie "<你的cookie值>" ride.fit
```

上传完会轮询到处理结束，打印活动链接：

```
✓ 完成! 活动 id: 20149224069
  https://www.strava.com/activities/20149224069
```

> `--name` / `--desc` 是在上传成功后**单独调 `bulk_update` 端点**实现的
> （上传接口本身不接受这两个参数）。那个端点需要带上活动现有的
> `sport_type` / `visibility` 等属性一起提交，否则可能被清掉，
> 所以这一步必须在拿到活动 id 之后做。

#### strava_watch_upload.py — 自动同步

```bash
# 一次性扫描目录
python strava_watch_upload.py -w ~/Garmin/Activities --once

# 持续监听，每 30 秒扫描一次
python strava_watch_upload.py -w ~/Garmin/Activities

# 上传后移动文件（避免重复上传）
python strava_watch_upload.py -w ~/Garmin/Activities --move-uploaded ~/Garmin/Activities/uploaded

# 自定义扫描间隔
python strava_watch_upload.py -w ~/Documents/FIT --interval 60
```

## 图形界面

不想碰命令行就双击 **`启动界面.bat`**。

### 为什么用 `.bat` 而不是直接双击 `.pyw`

`.pyw` 靠文件关联决定用什么程序打开，而这个关联**只有 python.org 的官方安装包会注册**。
用 Miniconda / Anaconda 装的 Python 不注册，双击 `.pyw` 会弹"选择一个应用以打开此文件"。

`.bat` 是 Windows 必有关联的，所以任何机器上双击都能跑。这个 `.bat` 做两件事：

1. 检查 `pythonw` 在不在 PATH 里（不在就给出中文提示，而不是闪一下就消失）
2. 用 `pythonw` 启动 `strava_gui.pyw` —— **不弹控制台窗口**

`.bat` 里的中文是 **GBK 编码**的（cmd 默认代码页 936），改成 UTF-8 会乱码。

界面就做四件事：

1. **粘贴 cookie** —— 有个「验证并保存」按钮，会真的去 Strava 验一次再存盘
2. **选择文件** —— 可多选，不支持的类型会跳过并提示
3. **上传** —— 逐个上传、轮询到处理完，每行显示状态，下面有日志
4. **改标题和说明** —— **只在恰好选中一个文件时可用**；多文件时输入框会禁用，
   走 Strava 从文件里读出的自动名称

> 多文件不支持改标题是刻意的 v1 取舍。要做"逐行编辑表格"，在 Tkinter 里
> 要自己叠 `Entry` 控件同步坐标、处理三个平台不同的滚轮事件，是整个界面里
> 最贵的一块，而按实际使用频率它又是最少用的。留到以后。

### 前提条件（发给别人时要注意）

- 需要 **Python**，以及 `pip install requests`。Tkinter 是 Python 自带的，
  但 `requests` 不是。
- **安装 Python 时必须勾选 "Add Python to PATH"** —— 否则 `启动界面.bat`
  找不到 `pythonw`（它会给出提示而不是静默失败）。
- **只在 Windows 上测过。** Tkinter 本身跨平台，但 Linux 上要额外装系统包
  (`python3-tk`)，macOS 用 Homebrew 装的 Python 不含 Tk（要 `brew install python-tk`）。

## 环境变量

两个脚本都支持通过环境变量传入 cookie：

```bash
export STRAVA_SESSION_COOKIE="你的cookie值"
python strava_cookie_upload.py ride.fit
```

## ⚠️ 注意事项

1. **Cookie 会过期** — 过期后重新跑 `python save_strava_cookie.py`。
2. **限速** — 短时间内大量上传会触发 HTTP 429，脚本已内置延迟和重试。
3. **非官方方式** — 这是绕过 Strava 官方 API 限制的变通方案，理论上违反 ToS，
   随时可能被 Strava 封堵。仅用于上传你自己的活动数据。
4. **批量限制** — nrc2strava 报告 600+ 活动会触发严格限速。
5. **上传是异步的** — 脚本会轮询 `progress.json` 直到处理完，拿到活动 id 才算
   成功。如果超时（默认 120 秒），文件其实已经传上去了，只是没等到结果，
   可以稍后去 Strava 上确认。
6. **协议随时可能再变** — Strava 改过一次，就可能再改。真出问题时用
   `debug_strava_auth.py` 探端点、`parse_curl.py` / `parse_har.py` 重新抓包。

## 未决清单

下面这些是**明确推迟的**，不是没想到。每条写清代价，免得以后被当成"已经解决了"。

| 项目 | 现状 | 推迟的代价 |
|------|------|-----------|
| 重复上传检测 | 没有 | 传过的文件会再传一遍，且没有任何提示。2026-09 开发期间就因此多传出一条重复活动 `20149224069` |
| 多文件逐行改标题 | 不支持 | 多文件只能用 Strava 自动名 |
| 跨平台 | 只在 Windows 上测过 | 非 Windows 用户是事实上的 QA |
| 自动更新 | 没有 | Strava 再改协议时，每个用户得自己重新下载 |
| cookie 明文落盘 | `~/.strava_cookie` 明文 | 本机上的任何程序都能读到 |

有一件事**没有**推迟：**失败原因分类**。`strava_gui.py` 里的
`classify_exception()` 把失败分成 cookie 失效 / 协议变更 / 网络问题 / 其他
四类，各给不同的处置建议。

它不能推迟，是因为"以后再迭代"这个策略的前提，就是**你能认出"这次是协议坏了"**。
分不出来的话，你收到的每条反馈都会长成"上传失败"，而你分不清该让用户重粘
cookie，还是该去改代码。

同理：Strava 已经改过一次上传协议（`/api/v3/uploads` → `/upload/files`），
所以这不是会不会再改的问题。下次变更的症状就是"上传失败"，分类器会把它
报成 `csrf` 那一类，日志里明说"这不是 cookie 的问题"。

## 支持的格式

- `.fit` / `.fit.gz` — Garmin, Wahoo, 多数码表
- `.gpx` / `.gpx.gz` — GPS 轨迹通用格式
- `.tcx` / `.tcx.gz` — Garmin Training Center XML

## 许可证

[MIT](LICENSE) © 2026 zhangyue118

## 替代方案：自建平台

如果不想依赖 Strava，推荐 **[Endurain](https://github.com/endurain-project/endurain)** —
开源、自托管的运动追踪平台，Docker 一键部署，支持 FIT/GPX/TCX 导入，
20+ 运动类型，完全掌控自己的数据。
