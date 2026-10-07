# DIY下载器

一个用于从 Google 表格读取链接并批量下载文件的本地客户端。

## 主要功能

- 读取 Google 表格指定工作表。
- 默认按 `A` 列名称创建文件夹，按 `P` 列链接下载源文件。
- 支持筛选“只下载包含”的人名或组合名。
- 支持下载成功后回填到指定列，默认 `Q` 列。
- 支持跳过本地已存在文件。
- 支持保存多个配置方案，并一键按顺序执行所有方案。
- 支持暗黑模式切换、预览、停止、打开下载文件夹。
- 支持直接粘贴链接下载：兼容从表格复制的 HTML 超链接和纯文本 URL。
- **独立板块：YouTube / Facebook 视频批量下载**（基于 `yt-dlp`，与表格下载互不影响）。
- **自动检查更新**：启动静默检查 GitHub Release，可一键下载并提示是否安装。

## 下载安装（推荐）

到 [Releases](https://github.com/secure-artifacts/DIY-sheets_batch_downloader/releases) 下载：

| 文件 | 说明 |
|------|------|
| `DIYDownloader-v*-windows-setup.exe` | **Windows 可安装版**（推荐） |
| `DIYDownloader-v*-windows.exe` | Windows 便携版（绿色运行） |
| `DIYDownloader-v*-macos.zip` | macOS 应用包 |

安装版会创建开始菜单/桌面快捷方式；后续有新版本时，打开软件点 **检查更新**，下载完成后会询问是否安装。

## 安装依赖

```powershell
pip install -r requirements_google.txt
```

## 运行

```powershell
python sheets_batch_downloader_modern.py
```

## 粘贴链接下载

点击界面里的 `粘贴链接下载`，可以粘贴：

- 从 Google 表格复制的超链接单元格。
- HTML `<a href="...">文件名</a>` 格式。
- 普通纯文本链接，例如 `张三 https://drive.google.com/file/d/...`。

粘贴后可以先预览，也可以直接下载。纯 Drive 链接没有显示文件名时，程序会通过 Drive API 获取真实文件名。

粘贴链接下载不需要填写表格 ID、工作表或回填列；这些配置只用于 Google 表格读取模式。粘贴链接下载不会回填表格。

没有填写表格 ID 或没有选择工作表时，点击主界面的 `开始下载` 会自动进入粘贴链接下载模式。

如果已经点击 `预览粘贴` 并把链接显示在预览表格里，再点击主界面的 `开始下载`，会直接下载当前预览表格里的这批粘贴链接。

## YouTube / Facebook 视频批量下载

切换到界面顶部的 **「YouTube / FB 视频」** 标签页：

1. 粘贴一条或多条链接（支持混贴文本自动提取 URL，支持批量）。
2. 选择下载目录、画质、下载模式。
3. **直接点「开始下载」即可**：会自动加载单视频/展开播放列表并开始下载，无需先解析。
4. 「解析预览」为可选，只用于提前查看列表内容。
5. 支持断点续传：中途停止或失败后保留临时文件，再次开始可继续。

### 下载模式

| 模式 | 行为 |
|------|------|
| **自动识别（推荐）** | YouTube 真实播放列表（`PL`/`UU` 等）整表下载；电台混播（`RD`）只下当前视频；FB 单条 Reels/视频按单条处理 |
| **仅单视频** | 忽略列表参数，只下载当前视频 |
| **展开播放列表/清单** | 强制展开 YouTube 播放列表，以及 Facebook 可识别的清单/合集 |

其他选项：

- **断点续传**：默认开启（yt-dlp `.part` 续传）。
- **已完成则跳过**：本地已有完整文件时跳过。
- **播放列表分子文件夹**：每个列表一个子目录，文件名带序号 `001 - 标题 [id].ext`。
- **列表上限**：限制单个播放列表最多展开多少条（0=不限制）。

依赖：

```powershell
pip install yt-dlp
```

建议安装 [ffmpeg](https://ffmpeg.org/) 并加入 PATH，以便最佳画质下的音视频合并，以及仅音频导出 mp3。

说明：

- 与 Google 表格下载是独立板块，互不抢配置。
- 公开的 YouTube / Facebook 视频一般可直接下载；需登录或隐私限制的内容可能失败。
- Facebook Reels「播放清单」取决于链接是否公开且 yt-dlp 能解析为多条目；私密清单无法下载。
- 其他 yt-dlp 支持的站点链接也可尝试解析下载。

## Google 凭据

程序需要 Google OAuth 客户端 JSON 文件或服务账号 JSON 文件。

为了安全，仓库不要提交下面这些本地文件：

- `token.json`
- `谷歌服务账号.json`
- `credentials.json`
- `diy_downloader_configs.json`

首次运行时在界面里选择你的凭据文件即可。授权生成的 `token.json` 会保存在程序目录。

## 自动更新

1. 启动后约 2 秒会静默检查最新 GitHub Release。
2. 也可点击右上角 **检查更新**。
3. 发现新版本可下载；下载完成后询问是否立即安装。
4. 安装包会打开安装向导；便携版会自动替换并重启。

## 打包 exe

本地打包便携版：

```powershell
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "DIYDownloader" --icon "logo.ico" --add-data "assets;assets" --add-data "logo.png;." --hidden-import yt_dlp --collect-all yt_dlp sheets_batch_downloader_modern.py
```

不建议把包含凭据的 exe 或 JSON 上传到公开仓库。

## GitHub Release / Attestation

仓库已包含 `.github/workflows/release.yml`。

发布方式：

```powershell
# 1. 修改 version.py 中的 APP_VERSION
# 2. 更新 release_notes.md
git add -A
git commit -m "Release v1.1.0"
git tag v1.1.0
git push origin main
git push origin v1.1.0
```

该 workflow 会：

- 在 Windows runner 构建便携 exe + **Inno Setup 安装包**
- 在 macOS runner 构建 app 压缩包
- 生成 Artifact Attestation 并创建 Release

产物：

- `DIYDownloader-v*-windows-setup.exe`（可安装版）
- `DIYDownloader-v*-windows.exe`（便携版）
- `DIYDownloader-v*-macos.zip`

不要手动在 GitHub Release 页面拖拽上传产物，否则严格 L2 Attestation 校验可能失败。
