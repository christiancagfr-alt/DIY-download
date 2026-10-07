# DIY下载器 v1.6.0

## 安全加固

- 更新源迁移到个人仓库 `christiancagfr-alt/DIY-download`。
- 自动更新只接受受信任 GitHub HTTPS Release 资产，并强制校验 GitHub SHA-256 digest 与文件大小。
- 公共文件下载改为流式写入 `.part` 后原子替换，避免大文件一次性占满内存。
- macOS/Linux OAuth token 使用私有目录权限和 `0600` 文件权限。
- yt-dlp 固定到经过审核的安全版本，并强制忽略用户级配置。
- 视频下载入口限制为 YouTube / Facebook。
- Windows FFmpeg 自动安装只接受带 SHA-256 digest 的 GitHub Release 资产；移除不可验证镜像回退。
- GitHub Actions 固定第三方 Action commit SHA，并收紧默认权限。
- 新增 `pip-audit`、Bandit、高置信密钥/危险模式扫描与 Dependabot。

## 功能修复

- Google Sheets 表格下载恢复 **起始行 / 结束行** 自定义范围。
- 预览、单次下载、保存方案、执行配置均使用所选行范围。
- 表格页未选择表格时不再自动跳到粘贴下载。
- **粘贴链接下载完全独立于 Google Sheets**：
  - 不需要表格 ID / 工作表
  - 不匹配名称列或链接列
  - 不回填表格
  - 普通 HTTP(S) 链接无需 Google 凭据
  - Drive 私有文件/文件夹按需使用全局 Google 授权

## macOS

- 同时支持 Apple Silicon arm64 与 Intel x64 构建。
- Finder 启动时增加 Homebrew / MacPorts ffmpeg 常见路径检测。
- macOS 自动安装 ffmpeg 使用 Homebrew。
- 正式 Release 要求 Developer ID 签名、Apple notarization 与 stapling；缺少签名 Secret 时拒绝发布未签名 Mac 包。
- Mac 更新不会静默执行下载文件，只打开/显示已经 SHA-256 校验的更新包。

## 发布供应链

- 直接依赖固定精确版本。
- `pip-tools` 自动生成 `requirements.lock` / `requirements-build.lock` 哈希锁文件。
- 正式构建使用 `pip --require-hashes`。
- Release 生成 `SHA256SUMS.txt` 与 GitHub build provenance attestation。
