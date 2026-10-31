# DIY下载器 v1.6.7

- **修复 Instagram / TikTok 同博主多条视频被误判「已存在，跳过」的问题**：新增 `extract_url_media_id` 从链接直接提取唯一 shortcode / 视频 ID，并严格按 `[video_id]` 精确匹配本地已下载文件，不再因通用标题（如 `Video by xxx`）前缀相同而误跳过同博主的其它视频；同时在进度列表中为通用标题自动附带 `[ID]` 便于区分。
- **视频批量下载板块 UI 重构（紧凑左右结构）**：顶部极简单行控制条 + 可折叠高级设置抽屉（`⚙`），左侧放大「粘贴视频/播放列表链接」主操作区，右侧为紧凑「实时进度看板 + 紧凑队列表」，日志默认隐藏于右上角「📋 总日志（查错视图）」抽屉中，按钮全面采用紧凑图标 + 鼠标悬浮文字提示。
- **Instagram 账号浏览器一键登录授权**：将原手动选择 Cookie 改为「🔑 登录 IG 账号」，点击后自动弹出独立浏览器前往 Instagram 登录，登录完成后自动捕获并加密保存私有授权凭据（`sessionid` / `ds_user_id`）。
- **全平台最高画质优先与自选画质增强**：默认「最佳质量」优先按最高分辨率、最高视频码率（`res`, `vbr`, `tbr`, `fps`）下载并合并音视频；新增「最高 4K (2160p)」「最高 2K (1440p)」档位，同时兼容 16:9 横屏与 TikTok / Instagram 9:16 竖屏高清分辨率判定与低清回退拦截。
- **应用安全加固**：统一 `env_tools.py` 源码回退安装 `yt-dlp==2026.10.26`；加固 `env_tools.py` 与 `updater.py` 的公网请求边界（`open_public` SSRF 防护、响应体大小上限、ZIP 白名单与 `confined_path` 校验）；收紧本地配置文件私有目录 ACL 权限与 CI/CD 工作流环境变量引用。

## 安装包与签名

Windows 提供安装版和便携版；Mac 提供 Apple Silicon arm64 与 Intel x86_64；附 SHA256SUMS.txt。
更新源保持为个人仓库 christiancagfr-alt/DIY-download。
缺少 Apple 凭据时，按用户授权发布未签名 / 未公证 Mac 包（无 Developer ID，可能含临时签名），Gatekeeper 可能阻止启动。实际状态附在下方。
