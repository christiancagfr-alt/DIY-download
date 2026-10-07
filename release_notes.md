# DIY下载器 v1.6.0

## Security hardening

- 更新源迁移到个人仓库 christiancagfr-alt/DIY-download。
- 固定并升级已审核运行时依赖，移除未使用的 Pillow。
- yt-dlp 仅允许 YouTube / Facebook，并忽略外部 yt-dlp 配置文件。
- OAuth token 在 macOS/Linux 使用更严格的文件权限。
- 公共文件下载改为流式写入，减少大文件内存耗尽风险。
- 自动更新要求 GitHub Release SHA-256 digest 校验。
- FFmpeg Windows 自动安装仅接受带 SHA-256 digest 的 GitHub Release 资产。
- GitHub Actions 固定到 commit SHA，并按 job 使用最小权限。

## Functionality

- 表格下载恢复自定义“起始行 / 结束行”，预览和下载均遵守范围。
- 粘贴链接下载与 Google Sheets 完全独立，不读取、不匹配、不回填表格。
- macOS 同时构建 Apple Silicon arm64 与 Intel x86_64。
- 正式 macOS Release 强制 Developer ID 签名、Apple notarization 与 stapling。
