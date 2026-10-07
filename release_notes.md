# DIY下载器 v1.6.1

## 修复与改进

- 加强下载文件名与保存目录检查，确保文件保存在指定目录中。
- 下载遇到同名文件时保留已有文件，改善并发下载处理。
- 加强下载地址、重定向和更新包完整性检查。
- 改善授权凭据迁移，迁移失败时保留原凭据。
- 改善大文件流式下载和资源限制。
- 表格回填使用纯文本，避免内容被解释为公式。

## 使用变化

- 普通 HTTP/HTTPS 下载只允许公网地址；localhost、NAS 和其他局域网地址将被阻止。
- 普通 HTTP/HTTPS 下载不使用系统代理，以确保下载地址检查有效。
- 更新源始终为个人仓库 christiancagfr-alt/DIY-download。

## 安装包与签名

- Windows 提供安装版与便携版；macOS 提供 Apple Silicon arm64 与 Intel x86_64 包。
- 提供 SHA256SUMS.txt 校验文件。
- Apple 凭据齐全时执行 Developer ID 签名与 Apple 公证；缺少凭据时按用户授权发布**未签名 / 未公证**的 Mac 包。此类包没有 Developer ID 签名，可能具有 PyInstaller 的临时签名，Gatekeeper 可能阻止启动。
- 各架构的实际签名状态由构建流程附在下方。
