# DIY下载器 v1.6.4

- 视频板块增加 TikTok 和 Instagram（IG）链接支持，包含 TikTok 短链接。
- 输入框包含 TikTok 链接时，显示红色“下载 TikTok 需要使用 VPN”提醒；移除链接后自动隐藏。提醒不检测或建立 VPN 连接。
- 保持默认 MP4、可选 MKV，以及原有画质、续传、跳过已有文件和仅音频处理逻辑。
- 平台识别和提醒切换已验证；本次未实测在线 TikTok/Instagram 下载，实际可用性取决于网络、平台限制及视频访问权限。

## 安装包与签名

Windows 提供安装版和便携版；Mac 提供 Apple Silicon arm64 与 Intel x86_64。附 SHA256SUMS.txt。
更新源保持为个人仓库 christiancagfr-alt/DIY-download。
缺少 Apple 凭据时，按用户授权发布未签名 / 未公证 Mac 包（无 Developer ID，可能含临时签名），Gatekeeper 可能阻止启动。实际签名状态附在下方。
