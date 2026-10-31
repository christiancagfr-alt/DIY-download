# DIY下载器 v1.6.5

- 更新 yt-dlp 至 2026.10.26，加入 curl-cffi 0.17.0 浏览器兼容请求组件，并在 Windows / macOS 包中收集该组件。
- 修复部分 TikTok 网页解析失败：用户反馈的视频已通过软件解析入口验证成功；未验证完整视频下载，不保证所有地区和视频都可访问。
- 视频板块新增可选“选择 Cookie / 清除 Cookie”，解析预览、下载前解析和正式下载均使用所选文件；支持 Netscape 格式 cookies.txt。
- 默认不读取浏览器账号，Cookie 路径只在本次运行使用；Cookie 文件不应分享，常见 Cookie 文件加入打包拦截。
- 保留默认 MP4、MKV 选择、原有画质和续传处理。Instagram 登录与平台限流仍可能需要有效 Cookie。

## 安装包与签名

Windows 提供安装版和便携版；Mac 提供 Apple Silicon arm64 与 Intel x86_64；附 SHA256SUMS.txt。
更新源保持为个人仓库 christiancagfr-alt/DIY-download。
缺少 Apple 凭据时，按用户授权发布未签名 / 未公证 Mac 包（无 Developer ID，可能含临时签名），Gatekeeper 可能阻止启动。实际状态附在下方。
