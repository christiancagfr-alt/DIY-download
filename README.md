# DIY下载器

一个支持云端链接下载、视频下载和 Google Drive 批量上传的本地客户端。

## 主要功能

- **云端链接下载**：粘贴 Drive 文件/父文件夹或公开 HTTP(S) 链接，递归扫描下载。
- 文件命名可选原文件名或连续序号；默认从 `01` 开始，也可指定 `05` 等起始号，保留扩展名。
- 文件夹可保留子目录结构，或将全部文件放入以云端父文件夹命名的目录。
- 支持暂停/继续、Drive ID 排重；已有文件不会被覆盖，同名文件自动加后缀。
- **批量上传云端**：回填字段可添加、删除、改名、指定目标列及数据来源，也支持填写自定义值。
- 分类读取列、名称和起始行可配置；一、二、三级和类型可独立选择。
- **视频下载**：基于 `yt-dlp`，支持 YouTube / Facebook / TikTok / Instagram。
- 暗黑模式、全局凭据设置和 GitHub Release 更新检查。

## 下载安装（推荐）

到 [Releases](https://github.com/christiancagfr-alt/DIY-download/releases) 下载：

| 文件 | 说明 |
|------|------|
| `DIYDownloader-v*-windows-setup.exe` | **Windows 可安装版**（推荐） |
| `DIYDownloader-v*-windows.exe` | Windows 便携版（绿色运行） |
| `DIYDownloader-v*-macos-arm64.dmg` / `DIYDownloader-v*-macos-x86_64.dmg` | macOS Apple Silicon / Intel 安装镜像，打开后拖入 Applications；签名状态以该次发布说明为准 |

安装版会创建开始菜单/桌面快捷方式；后续有新版本时，打开软件点 **检查更新**，下载完成后会询问是否安装。

## 安装依赖

```powershell
pip install -r requirements_google.txt
```

## 运行

```powershell
python sheets_batch_downloader_modern.py
```

## 云端链接下载

粘贴纯文本 URL 或从表格复制的 HTML 超链接，选择下载目录后开始下载。此功能不读取或回填 Google 表格；原“表格下载”标签页已移除。

链接输入采用单行列表：粘贴后自动省略超长显示，悬停查看完整地址；复制和下载始终使用原始链接。支持按钮、右键和 Ctrl+V / ⌘V 粘贴，Delete 删除所选。左侧设置常驻，右侧主要展示任务与进度，日志默认折叠。

- 原文件名：使用 Drive 文件名或 HTTP 响应中的文件名，不为无扩展名文件补 `.jpg`。不适用于本地文件系统的字符会安全替换。
- 序号：整批从指定序号连续编号，如 `05.mp4`、`06.jpg`；重复 Drive ID 不占新编号，已有序号会避开。
- 文件夹：按 Drive 名称顺序递归读取。保留目录时保存为 `下载目录/父文件夹/子文件夹/文件`；合并时保存为 `下载目录/父文件夹/文件`。
- 已下载的 Drive 文件按 ID 排重，子目录也有效；如需重新以另一种命名或目录结构下载，可选择新的下载目录。

## 上传列映射和分类

点击顶部“表格与列映射设置”打开配置弹窗，保存后返回主页面。主页面按人员、分类、文件拖拽区排列，右侧显示上传队列；回执和日志按需展开。

在上传设置中，入库映射仅显示“表头名称、目标列”，回填内容保持原有字段对应关系。添加字段时选择需要的自动回填字段；删除字段或将目标列留空可停用。目标列不可重复。列映射区域可单独展开/折叠，保存设置后自动收起。

默认只按指定列写入数据；勾选“上传时将映射中的表头名称写入表格”才会更新云端表头。表头行和插入数据行均可调整，数据行需在表头之后。旧版配置自动兼容。

“分类和人员读取列”设置各级、类型、上传者和审核人员所在列。点击“加载分类目录”会自动从起始数据行的上一行读取表头名称，并更新分类和人员选项。人员列初始留空，填写其实际列字母后即可读取。所有下拉框独立选择；上传者、审核人员也可以直接输入姓名，手动姓名不会写回分类目录。空表头使用默认名称。

下拉框和数字输入框只有在获得焦点后才响应滚轮，未聚焦时滚轮用于滚动页面。

## macOS 打包

```bash
python scripts/macos_icon.py
python -m PyInstaller --noconfirm DIYDownloader.spec
bash scripts/package_macos.sh dist/DIYDownloader.app release/DIYDownloader.dmg
```

DMG 内含原生图标应用与 Applications 快捷入口。发布流程为 Apple Silicon 和 Intel 分别构建 DMG，保留 ZIP 兼容旧版本更新器；配有 Developer ID 时签名、公证应用和 DMG。

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

- 与云端链接下载、批量上传是独立板块，配置互不影响。
- 公开的 YouTube / Facebook 视频一般可直接下载；需登录或隐私限制的内容可能失败。
- Facebook Reels「播放清单」取决于链接是否公开且 yt-dlp 能解析为多条目；私密清单无法下载。
- 出于安全边界考虑，视频下载仅接受 YouTube / Facebook 域名。

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
- 在 Apple Silicon 与 Intel macOS runner 构建、Developer ID 签名并 notarize app
- 生成 Artifact Attestation 并创建 Release

产物：

- `DIYDownloader-v*-windows-setup.exe`（可安装版）
- `DIYDownloader-v*-windows.exe`（便携版）
- `DIYDownloader-v*-macos-arm64.zip` / `DIYDownloader-v*-macos-x86_64.zip`

不要手动在 GitHub Release 页面拖拽上传产物，否则严格 L2 Attestation 校验可能失败。

### 读取目标入库表头

打开“表格与列映射设置”，展开“入库列映射”，指定入库表头行，点击“读取整行表头”。程序从左到右生成表头名称和实际列字母，跳过空表头但不压缩列号，无需先添加字段。校对时可修改名称和映射列，保存后按实际映射列回填，重启不会自动重新读取或覆盖修改。只有主动重新读取才会重建列表。

已识别的回填字段保持其内容来源，修改显示名称不会改变它。未识别的业务字段暂不回填，需填写时可右键指定回填内容；不会按列位置猜测日期、链接等。分类与人员依然从分类工作表单独读取。

表格单元格采用双击或编辑键进入编辑，避免 macOS 中文输入法直接向表格提交时递归触发编辑。进入编辑后可正常输入中文。

### 分类与人员的写入目标

在“分类和人员读取列”同一行设置“分类表读取列”和“写入入库表的哪一列”。右边可选项来自入库列映射，例如“国家 → K 列”，选择后当前下拉值会写入该列；选择“不写入”则不会回填。不同下拉字段不能占用同一个目标。修改入库列后点击“更新可选写入目标”，校对并保存。

上传者、审核人员读取列留空时，加载分类目录会按明确的人员表头自动识别；同名多列或未找到时需手动填写实际列字母。姓名可与其他下拉框共用读取列，写入列不能冲突。加载后会显示分别读取的人数，仍可手动输入姓名。
