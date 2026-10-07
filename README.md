# DIY下载器

一个本地桌面下载工具，支持 Google Sheets 批量下载、独立粘贴链接下载、YouTube / Facebook 视频下载，以及 Google Drive 批量上传。

## 主要功能

- Google Sheets 批量下载：
  - 自定义 **起始行 / 结束行**
  - 名称列、链接列、关键字筛选
  - 本地分类目录、断点跳过、回填
  - 保存多个配置方案并顺序执行
- 独立 **粘贴链接下载**：
  - 只处理当前粘贴的 HTTP(S) / Google Drive 链接
  - **不读取、不匹配、不回填 Google Sheets**
  - 普通公开 HTTP(S) 链接不需要 Google 凭据
  - 私有 Google Drive 内容才按需使用全局 Google 授权
  - 支持暂停/继续、Drive ID 排重、文件夹断点续传
- YouTube / Facebook 视频批量下载：
  - 仅接受 YouTube / Facebook 链接
  - 支持播放列表、画质选择、断点续传
  - 使用安全下限以上的固定 yt-dlp 版本
- Google Drive 批量上传
- Windows / macOS Intel / macOS Apple Silicon
- GitHub Release 自动更新：
  - 更新源只指向个人仓库 `christiancagfr-alt/DIY-download`
  - 自动更新下载必须通过 GitHub Release SHA-256 digest 校验
  - macOS 不静默执行下载内容，只打开/显示已校验的更新包

## 下载

正式发布地址：

https://github.com/christiancagfr-alt/DIY-download/releases

预期产物：

| 文件 | 平台 |
|---|---|
| `DIYDownloader-v*-windows-setup.exe` | Windows 安装版 |
| `DIYDownloader-v*-windows.zip` | Windows 便携版 |
| `DIYDownloader-v*-macos-arm64.zip` | macOS Apple Silicon |
| `DIYDownloader-v*-macos-x64.zip` | macOS Intel |
| `SHA256SUMS.txt` | 发布文件 SHA-256 清单 |

macOS 正式 Release 必须经过 Developer ID 签名、Apple notarization 和 stapling；如果签名/公证 Secret 未配置，正式发布流水线会失败，不会发布未签名的 Mac 包。

## 开发环境

Python 3.12。

直接依赖定义在：

- `requirements.in`
- `requirements_google.txt`

合并到 `main` 后，GitHub Actions 会生成：

- `requirements.lock`
- `requirements-build.lock`

正式发布构建使用 `pip --require-hashes` 安装哈希锁定依赖。

开发运行：

```bash
python -m pip install -r requirements_google.txt
python sheets_batch_downloader_modern.py
```

## Google Sheets 批量下载

1. 在顶部 **全局设置** 配置 Google OAuth 客户端 JSON 或服务账号 JSON。
2. 填写表格 ID，并加载工作表。
3. 设置名称列 / 链接列。
4. 设置 **起始行 / 结束行**。
5. 可先预览，再开始下载。

表格下载页如果没有选择表格，会提示先加载工作表；不会再自动跳入粘贴下载模式。

## 独立粘贴链接下载

切换到顶部 **粘贴链接下载** 标签页：

1. 选择本地下载目录。
2. 每行粘贴一个链接。
3. 点击 **开始下载**。

这个页面和 Google Sheets 完全独立：

- 不需要表格 ID
- 不需要工作表
- 不读取名称列 / 链接列
- 不匹配表格行
- 不写回数量、状态、人员或日期

Google Drive 私有文件或文件夹仍需要全局 Google 授权；普通公开 HTTP(S) 链接不需要 Google 登录。

## YouTube / Facebook

视频页只接受审核过的 YouTube / Facebook 域名。其他 yt-dlp 支持的网站不会交给 yt-dlp 处理。

应用强制忽略用户级 yt-dlp 配置文件，避免本机外部配置向程序注入额外执行器或参数。

高清合并需要 ffmpeg：

- Windows：应用只从 GitHub Release 下载**带 SHA-256 digest**的 FFmpeg 资产，校验通过后才解压。
- macOS：优先检测 Homebrew / MacPorts 常见路径；自动安装使用 Homebrew `brew install ffmpeg`。
- 不再使用无法验证摘要的 FFmpeg 镜像回退。

## Google 凭据与本地数据

不要把以下文件提交到仓库：

- `token.json`
- `credentials.json`
- `谷歌服务账号.json`
- `client_secret*.json`
- 服务账号 JSON
- 私钥 / 证书 / `.env`

OAuth token：

- Windows：保存在本机用户数据目录。
- macOS：保存在 `~/Library/Application Support/DIYDownloader/`。
- macOS/Linux 上目录使用私有权限，token 文件使用 `0600` 权限。

程序目前需要 Google Sheets 权限和完整 Google Drive 权限，因为它需要读取/下载用户已有的任意 Drive 文件和文件夹，并支持批量上传。

## 自动更新

版本和更新仓库定义在 `version.py`。

当前源：

```text
christiancagfr-alt/DIY-download
```

自动更新流程：

1. 查询这个个人仓库的最新 GitHub Release。
2. 只接受 GitHub HTTPS Release 资产。
3. 要求 GitHub API 提供 `sha256:...` digest。
4. 流式下载到 `.part`。
5. 校验文件大小和 SHA-256。
6. 校验成功后才进入安装/打开流程。

macOS 仅打开或在 Finder 中显示已校验的 `.zip` / `.dmg`，不会静默执行下载文件。

## 安全检查

仓库包含：

- `.github/workflows/security.yml`
  - `pip-audit`
  - Bandit High gate
  - Python 编译检查
  - 自定义密钥 / 高置信危险模式 / Action SHA 检查
- `.github/workflows/build-smoke.yml`
  - Windows 实际 PyInstaller 构建
  - macOS arm64 实际 PyInstaller 构建
  - macOS Intel 实际 PyInstaller 构建
- Dependabot：Python 和 GitHub Actions 每周检查
- Release Actions 全部固定到完整 commit SHA

详见 `SECURITY.md` 与 `SECURITY_AUDIT_REPORT.md`。

## macOS 正式发布所需 Secrets

在个人仓库 Settings → Secrets and variables → Actions 中配置：

- `APPLE_CERTIFICATE_P12_BASE64`
- `APPLE_CERTIFICATE_PASSWORD`
- `APPLE_SIGNING_IDENTITY`
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_PASSWORD`

这些 Secret 只进入 GitHub Actions 临时 runner，不应写入源码。

## 发布

1. 更新 `version.py` 与 `release_notes.md`。
2. 确认依赖锁文件已生成并提交。
3. 确认安全 CI 和三平台 smoke build 通过。
4. 创建 `v*` tag。
5. Release workflow 会重新执行安全门禁、构建、Mac 签名/公证、生成 SHA256SUMS、生成 build provenance attestation，并发布到当前个人仓库。

不要手工把本地构建产物当成正式 Release 上传。
