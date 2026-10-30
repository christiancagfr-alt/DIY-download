# DIY下载器 v1.6.3

- YouTube / Facebook 下载新增“下载格式”选择，默认 MP4，可切换 MKV。
- 保持原有画质、续传、已下载文件跳过和播放列表处理规则。
- “仅音频”仍使用原有 MP3 提取逻辑；视频格式选择不影响音频下载。
- 视频封装需要 FFmpeg，不通过降低画质或视频转码改变原有画质选择。

## 安装包

Windows 提供安装版和便携版；Mac 提供 Apple Silicon arm64 与 Intel x86_64。
更新源保持为个人仓库 christiancagfr-alt/DIY-download，附 SHA256SUMS.txt。
缺少 Apple 凭据时按用户授权发布未签名 / 未公证 Mac 包（无 Developer ID，可能含临时签名），Gatekeeper 可能阻止启动。实际状态附在下方。
