# DIY下载器安全代码审核报告

**审核日期：** 2026-10-07  
**目标版本：** v1.6.0  
**目标仓库：** `christiancagfr-alt/DIY-download`  
**审核分支：** `security-mac-hardening-2026-10-07`

## 1. 项目概况

这是一个 Python 3.12 桌面应用，主界面使用 PySide6，并同时保留部分 Tkinter 兼容代码。主要功能包括 Google Sheets/Drive 批量下载、Google Drive 批量上传、独立链接下载、YouTube/Facebook 视频下载、自动更新，以及 Windows/macOS 打包发布。

审核覆盖：

- Python 主程序及下载/上传/视频/更新/环境安装模块。
- Google OAuth token 与凭据处理。
- HTTP(S) 下载、Google Drive 下载、FFmpeg 安装、yt-dlp 调用。
- GitHub Actions 安全审计、构建和 Release 工作流。
- Windows Inno Setup 安装器。
- macOS Intel x86_64 与 Apple Silicon arm64 构建路径。
- 常见硬编码密钥、私钥、Token、敏感配置文件。
- Python 直接依赖与本次 Linux CI 实际解析出的关键间接依赖。

### 1.1 直接运行时依赖

| 依赖 | 审核后版本 |
|---|---:|
| google-api-python-client | 2.201.0 |
| google-auth | 2.60.0 |
| google-auth-oauthlib | 1.5.0 |
| google-auth-httplib2 | 0.4.4 |
| httplib2 | 0.32.0 |
| PySide6 | 6.11.2 |
| yt-dlp | 2026.8.19 |

未使用的 Pillow 已从运行时依赖移除。

### 1.2 Linux / Python 3.12 安全 CI 实际解析的关键间接依赖

以下不是跨平台 lock 文件，而是安全流水线本次实际安装结果：

- google-api-core 2.41.0
- googleapis-common-protos 1.75.5
- protobuf 7.36.2
- proto-plus 1.29.0
- cryptography 50.0.2
- requests 2.34.2
- requests-oauthlib 2.0.0
- urllib3 2.8.0
- certifi 2026.7.22
- charset-normalizer 3.5.2
- idna 3.20
- oauthlib 4.0.0
- opentelemetry-api 1.45.1
- pyasn1 0.6.4
- pyasn1-modules 0.4.2
- pyparsing 3.3.3
- shiboken6 / PySide6 Essentials / PySide6 Addons 6.11.2
- uritemplate 4.2.0

**待确认：** 当前仓库没有带 hashes 的跨平台完整依赖锁文件。直接依赖已经精确固定，并由 `pip-audit` 审核，但间接依赖仍由 pip 根据平台解析。若需要最高等级的可复现构建，建议后续生成按平台验证的 hash lock。

## 2. 问题汇总

| 编号 | 类别 | 严重级别 | 位置 | 状态 |
|---|---|---|---|---|
| SEC-01 | 依赖 | High | `requirements_google.txt:8` | 已修复 |
| SEC-02 | 依赖 | High | `requirements_google.txt:6` | 已修复 |
| SEC-03 | 供应链/更新 | High | `updater.py:120,173,252` | 已修复 |
| SEC-04 | 供应链/下载 | High | `env_tools.py:291,332,425` | 已修复 |
| SEC-05 | 代码/yt-dlp | High | `video_batch_downloader.py:321,324,352` | 已修复 |
| SEC-06 | 凭据 | Medium | `sheets_batch_downloader.py:23,145` | 已修复 |
| SEC-07 | 代码/资源耗尽 | Medium | `sheets_batch_downloader.py:1182` | 已修复 |
| SEC-08 | CI/CD | Medium | `.github/workflows/*.yml` | 已修复 |
| SEC-09 | 密钥泄露 | High | 全仓库 / `scripts/security_checks.py` | 未发现泄露；持续检查已加入 |
| SEC-10 | 网络/SSRF | Medium | `sheets_batch_downloader.py:1189` | 待开发者决定 |
| SEC-11 | OAuth 权限 | Medium | `sheets_batch_downloader.py:17-20` | 已确认保留完整 Drive 权限 |
| SEC-12 | 依赖可复现性 | Low | 依赖管理 | 建议关注 |
| SEC-13 | macOS 发布 | Medium | `.github/workflows/release.yml:149-240` | 代码已完成；需配置 Apple Secrets |
| FUNC-01 | 功能修复 | — | `sheets_batch_downloader_modern.py:916,2206` | 已修复 |
| FUNC-02 | 功能隔离 | — | `paste_link_download_page.py:298,1185` | 已修复 |

## 3. 已修复问题

### SEC-01 — yt-dlp 已知高危漏洞暴露

原项目允许非常旧的 `yt-dlp>=2024.1.0`。2026 年 yt-dlp 修复了多项 High 级问题，包括命令执行/任意文件写入相关风险。

**修复：**

- 固定 `yt-dlp==2026.8.19`。
- 视频输入只接受 YouTube / Facebook 明确域名。
- 设置 `ignoreconfig=True`，不读取用户目录或当前目录中的 yt-dlp 外部配置，从而避免外部 `--exec`、外部 downloader 等改变应用安全边界。

参考：
- GHSA-69qj-pvh9-c5wg
- GHSA-vx4q-3cr2-7cg2
- GHSA-c6mh-fpjc-4pr3
- GHSA-f7j3-774f-rfhj

### SEC-02 — httplib2 gzip/deflate 解压资源耗尽

旧依赖范围可能解析到 `httplib2<0.32.0`，存在已公开的响应解压内存耗尽问题。

**修复：** 固定 `httplib2==0.32.0`。

参考：GHSA-j5g9-f88f-gfj3。

### SEC-03 — 自动更新缺少发布资产完整性验证

原更新流程仅依赖 HTTPS 下载 GitHub Release 资产，下载后可直接启动 Windows 安装程序，没有对资产内容做独立摘要验证。

**修复：**

- 更新源只读取 `christiancagfr-alt/DIY-download`。
- Release asset 必须包含合法 SHA-256 digest。
- 下载时边写入边计算 SHA-256；不一致立即删除。
- 下载 URL 限制为 HTTPS + GitHub 受信 host。
- 资产文件名进行 basename 检查。
- Windows 只自动启动名称明确包含 setup/installer 的 EXE。
- macOS/Linux 不执行下载产物；ZIP 只打开所在目录。

### SEC-04 — FFmpeg 第三方二进制自动下载缺少完整性校验

原 Windows 自动安装有无固定摘要的 fallback 下载。

**修复：**

- Windows 只接受 GitHub Release API 返回、且带 SHA-256 digest 的 win64 ZIP。
- 下载过程中验证 SHA-256。
- 删除无摘要 fallback。
- macOS 不下载未知预编译二进制；使用 Homebrew 参数数组调用 `brew install ffmpeg`。
- macOS 增加 Homebrew / MacPorts 常见 FFmpeg 路径检测。

### SEC-05 — yt-dlp generic extractor / 外部配置扩大攻击面

原实现允许“其他 yt-dlp 支持的站点”，会将任意 URL 交给 generic extractor。

**修复：**

- 仅接受 YouTube 和 Facebook 域名。
- 使用严格 host suffix 判断，避免伪域名。
- 禁用外部 yt-dlp 配置文件。

### SEC-06 — OAuth token 在 macOS/Linux 权限不足

原 token 路径和权限主要围绕 Windows 设计。

**修复：**

- macOS token 迁移到 `~/Library/Application Support/DIYDownloader/token.json`。
- Linux 使用 XDG data 路径。
- POSIX 配置目录尽量设置为 `0700`。
- OAuth token 写入后设置为 `0600`。
- Windows 继续使用 `%LOCALAPPDATA%\DIYDownloader`。

### SEC-07 — 公共文件下载一次性读取整个响应

原 `PublicDownloader` 使用 `response.read()` 将完整文件载入内存，大文件可能造成内存耗尽。

**修复：**

- 改为 256 KiB 分块流式写入。
- 先写 `.part`。
- 成功后原子替换目标文件。
- 失败时清理未完成文件。

### SEC-08 — GitHub Actions 使用可移动版本标签 / 权限过宽

原 Release workflow 使用 `actions/*@v4`、`@v5` 等可移动引用，并在工作流顶层给予写权限。

**修复：**

- 第三方 Action 固定到完整 commit SHA。
- 默认 `contents: read`。
- 只有最终 release job 拥有 `contents: write`、`id-token: write`、`attestations: write`。
- PyInstaller 和 Inno Setup 版本固定。
- 安全流水线持续运行 compile、pip-audit、Bandit 和自定义策略检查。

### SEC-09 — 密钥与凭据泄露

审核未发现已提交的 Google API Key、GitHub PAT、AWS AccessKey、OpenAI-style key、Slack token 或私钥正文。

**修复/预防：**

- `.gitignore` 已覆盖 `token.json`、`credentials.json`、`谷歌服务账号.json`、`.env` / `.env.*`。
- `scripts/security_checks.py` 会阻止常见密钥格式以及敏感本地配置文件进入仓库。

如果未来任何真实密钥曾经提交到 Git 历史或公开渠道，必须在对应平台**作废并重新生成**；仅从最新代码删除不等于密钥已安全。

### FUNC-01 — 恢复自定义表格行范围

**修复：**

- 常用设置区域增加“起始行 / 结束行”。
- 预览严格遵守所选范围。
- 正式下载严格遵守所选范围。
- 配置方案保存和恢复行范围。
- 表格页不再因为未选表格而自动跳进粘贴模式。

### FUNC-02 — 粘贴链接下载完全独立

**修复：**

- 不读取 Google Sheets。
- 不按表格名称或链接列匹配。
- 不回填 Sheet。
- 保留私有 Google Drive OAuth 下载能力。
- 保留暂停/继续、断点续传、Drive ID 排重。

## 4. macOS 兼容与发布安全

### 已实现

- Apple Silicon：`macos-15 / arm64`。
- Intel：`macos-15-intel / x86_64`。
- Finder 启动时也会查找常见 Homebrew/MacPorts FFmpeg 路径。
- 正式 Release 强制 Developer ID 签名。
- 启用 Hardened Runtime（`--options runtime`）。
- 使用 Apple notarytool 公证。
- stapler staple + validate。
- Intel 与 Apple Silicon 产物分开发布。

### 正式发布前必须配置的 GitHub Secrets

仓库代码中不保存这些值：

- `MACOS_CERTIFICATE_P12`
- `MACOS_CERTIFICATE_PASSWORD`
- `MACOS_SIGNING_IDENTITY`
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_PASSWORD`

未配置时正式 macOS Release 会失败，而不会悄悄发布未签名应用。

## 5. CI / 自动化验证结果

### 安全审计

在 GitHub Actions 的 Python 3.12 Linux runner 上：

- `python -m compileall -q .`：通过。
- `pip check`：通过。
- `pip-audit -r requirements_google.txt`：**No known vulnerabilities found**。
- Bandit High gate：**High = 0**。
- 自定义 repository security policy：通过。
- 常见密钥/敏感配置策略：通过。

Bandit 的 Medium 明细均为 B310 / `urllib.request.urlopen` 通用提示：

1. `env_tools.py:307` — 下载 URL 在调用前已强制 HTTPS + github.com，并校验 SHA-256，判定为已缓解。
2. `env_tools.py:347` — Release API 列表是源码硬编码 GitHub API 地址，判定为已缓解。
3. `updater.py:67` — 调用源为应用固定的个人 GitHub Release API，判定为已缓解。
4. `updater.py:192` — 下载前验证 HTTPS + GitHub host + SHA-256，判定为已缓解。
5. `sheets_batch_downloader.py:1189` — 用户提供的普通 HTTP(S) 下载链接，存在内网访问能力，保留为 SEC-10。

### 跨平台构建

代码修改后的 Build Smoke 已验证：

- Windows onedir PyInstaller：通过。
- macOS Apple Silicon arm64 PyInstaller：通过。
- macOS Intel x86_64 PyInstaller：通过。

正式 Release 的 Developer ID 签名和 Apple notarization 需要真实 Apple 凭据，因此只能在上述 Secrets 配置后执行。

## 6. 需要开发者决定的问题

### SEC-10 — 是否禁止普通链接访问私网/本机地址

**位置：** `sheets_batch_downloader.py:1189`  
**等级：** Medium  
**利用条件：** 攻击者需要能让用户下载其提供的 HTTP(S) URL，或者能够控制表格/粘贴内容。

当前普通链接下载允许 HTTP(S) URL，包括可能指向：

- `127.0.0.1`
- RFC1918 私网
- link-local
- 内部服务地址

这是桌面工具中的 SSRF/本机网络探测面。

可选方案：

- **A（推荐给纯互联网下载场景）：** 拒绝 loopback、private、link-local、reserved IP，并对 DNS 解析结果做同样检查。
- **B：** 保持当前能力，以兼容 NAS、公司内网、家庭局域网下载；在 UI 中明确显示目标 host 并要求用户确认私网 URL。

本次未擅自封锁，因为这会改变可用业务场景。

### SEC-12 — 是否引入 hash lock

直接依赖目前已经精确固定，CI 本次依赖解析也通过 pip-audit，但没有 `--require-hashes` 的跨平台锁文件。

建议为 Windows、macOS arm64、macOS x86_64 生成并在 CI 验证 hash lock。由于不同平台 wheel 集不同，应使用经三平台 CI 验证的锁策略，而不是手工复制 Linux freeze。

## 7. 已确认的业务权限取舍

Google Drive 使用完整 `drive` scope 是当前批量上传和访问已有任意 Drive 文件/文件夹所需的业务能力。将其强制改成 `drive.file` 可能破坏现有文件访问，因此本次按开发者要求保留。

风险缓解依赖：

- OAuth token 仅保存在用户本机应用数据目录。
- POSIX token 权限收紧。
- 凭据文件被 gitignore 和安全策略阻止提交。

## 8. 剩余风险与后续建议

1. 选择 SEC-10 的私网 URL 策略。
2. 生成并维护跨平台 hash lock。
3. 配置 Apple Developer ID / notarization Secrets 后再创建正式 v1.6.0 二进制 Release。
4. Windows 若面向大量外部用户分发，建议后续增加 Authenticode 代码签名。
5. 保持每周 Security Audit workflow，并定期更新固定依赖版本；更新前继续执行 pip-audit 和跨平台构建。
6. 如果任何凭据曾出现在 Git 历史、Issue、聊天、构建日志或公开 Release 中，应立即轮换，而不是仅删除文件。

## 9. 最终结论

在本次可安全自动修复的范围内，已修复已知 High 依赖风险、更新供应链完整性、FFmpeg 二进制完整性、yt-dlp 攻击面、POSIX token 权限、公共下载内存耗尽风险和 CI/CD 可移动 Action 引用等问题。

当前安全流水线未发现已知依赖漏洞，Bandit High 为 0。剩余主要安全决策是是否允许用户主动下载局域网 HTTP(S) 地址，以及是否进一步引入跨平台 hash lock。正式 macOS Release 还需要开发者提供 Apple Developer 签名/公证 Secrets。
