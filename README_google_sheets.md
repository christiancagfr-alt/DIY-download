# DIY下载器

这个版本可以直接读取指定 Google 表格和 Sheet，不需要复制链接。

## 功能

- 输入 Google Spreadsheet ID。
- 自动加载不同的 Sheet。
- 自定义名称列，例如 `A`，用于创建文件夹。
- 自定义链接列，例如 `P`，自动读取隐藏超链接。
- 自动读取单元格隐藏超链接、公式链接、富文本链接、智能链接里的 URL。
- 使用 Google OAuth 和 Drive API 下载私有 Drive 文件。
- 文件名使用 Drive 源文件名，不再用行号命名。
- 可以扫描整个 Sheet。
- 可以设置“包含名称”，只下载 A 列里包含指定文字的行。
- 可以停止任务。
- 可以跳过已下载过的同名文件。
- 本地按 A 列名称创建文件夹，例如：

```text
张三/
  12-ZB-张三-祷告男-李四-不要划走这个视频，因为这条信息是给你的。-46211-FF-2026-7-9.mp4
```

## 安装依赖

```powershell
pip install -r requirements_google.txt
```

## 准备凭据 JSON

客户端支持两种凭据：

- 服务账号 JSON，例如 `谷歌服务账号.json`
- OAuth 桌面应用 JSON，例如 `credentials.json`

如果使用服务账号，把 `谷歌服务账号.json` 放到本文件夹即可，程序会优先自动选择它。

重要：服务账号有自己的邮箱，通常长这样：

```text
xxx@xxx.iam.gserviceaccount.com
```

你必须把 Google 表格，以及要下载的 Drive 文件或上级文件夹，共享给这个服务账号邮箱。否则程序能打开，但读取表格或下载文件会报权限错误。

如果使用 OAuth 桌面应用：

1. 打开 Google Cloud Console。
2. 创建项目或选择项目。
3. 启用 Google Sheets API 和 Google Drive API。
4. 创建 OAuth 客户端 ID，类型选择“桌面应用”。
5. 下载 JSON，命名为 `credentials.json`，放到本文件夹。

## 运行

```powershell
python sheets_batch_downloader.py
```

如果使用服务账号，不会打开浏览器登录。

如果使用 OAuth 桌面应用，第一次运行会打开浏览器，让你登录 Google 并授权。授权后会生成 `token.json`，下次不用重复登录。

## 表格 ID 在哪里

表格链接一般是：

```text
https://docs.google.com/spreadsheets/d/这里就是表格ID/edit
```

复制 `/d/` 和 `/edit` 中间那一段填进去。

## 推荐配置

```text
名称列：A
链接列：P
起始行：2
扫描整个 Sheet：勾选
文件夹命名：person
已下载过则跳过：勾选
```

如果只想下载某个人，例如“张三”，就在“包含名称”里填：

```text
张三
```
