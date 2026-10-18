# DIY下载器 v1.6.2

## 本次修复

- 粘贴链接下载直接保存到用户选择的目录，不再为每个文件创建文件夹；Drive 文件夹内的文件也汇总到所选目录。
- 保留文件名冲突保护与 Drive ID 排重。
- 去掉内置的个人 Google Drive 文件夹 ID、业务工作表名称和凭据预填路径，改由用户自行填写。
- 打包流程检查并阻止个人配置、保存方案和授权缓存混入安装包。
- 用户以后自行保存的设置仍可恢复；更新不会自动删除其他用户的本机配置。

## 安装包与签名

- Windows：安装版和便携版。macOS：Apple Silicon arm64 和 Intel x86_64。
- 更新源为个人仓库 christiancagfr-alt/DIY-download。
- 提供 SHA256SUMS.txt 校验文件。
- 缺少 Apple 凭据时，按用户授权发布未签名 / 未公证的 Mac 包（无 Developer ID 签名；可能含 PyInstaller 临时签名），Gatekeeper 可能阻止启动。
- 各架构实际签名状态由构建流程附在下方。
