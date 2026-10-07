"""YouTube / Facebook 视频批量下载板块（基于 yt-dlp）。

支持：
- YouTube 单视频 / 播放列表
- Facebook 单视频 / Reels / 可识别的播放清单
- 多链接批量
- 断点续传（.part 续传、已完成跳过）
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

from env_tools import (
    install_missing_components,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
    scan_environment,
)


APP_SECTION = "YouTube / FB 视频"
URL_RE = re.compile(r"https?://[^\s<>\"'，。；、]+", re.I)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# 真实播放列表前缀；RD/UL 等为电台/混播，默认不当整表下载
YOUTUBE_REAL_PLAYLIST_PREFIXES = ("PL", "UU", "FL", "OL", "LL", "WL", "TL")

# 画质策略（所有档位统一）：
# 1) YouTube 高清几乎都是「纯视频轨 + 纯音频轨」，必须 bestvideo*+bestaudio，再由 ffmpeg 无损合并
# 2) 绝不能优先 progressive 单文件（format 18 等），那通常只有 360p，看起来会非常糊
# 3) 不优先 avc1：同分辨率下 vp9/av1 往往码率更高、更清晰；容器用 mkv 兼容合并
# 4) format_sort 强制按分辨率 → 编码质量 → 码率排序
def _height_format(max_h: int) -> str:
    """在不超过 max_h 的前提下，选该档位内画质最好的分离流。"""
    return (
        # 首选：该分辨率上限内最好的视频 + 最好音频（含 storyboard 以外的所有视频轨）
        f"bestvideo*[height<={max_h}]+bestaudio/"
        f"bestvideo[height<={max_h}]+bestaudio/"
        # 少数站点只有合成流时再退一步（仍限高度，避免误选超限）
        f"best[height<={max_h}][vcodec!=none]/"
        # 最后兜底：不限高度的分离流（源本身可能没有目标分辨率）
        f"bestvideo*+bestaudio/best"
    )


QUALITY_OPTIONS = {
    # 原画画质：不限高度，选全站最好的视频轨+音轨
    "最佳质量": "bestvideo*+bestaudio/bestvideo+bestaudio/best",
    "最高 1080p": _height_format(1080),
    "最高 720p": _height_format(720),
    "最高 480p": _height_format(480),
    # 仅音频：尽量高码率
    "仅音频": "bestaudio/best",
}

# 期望分辨率（用于下载前校验是否误选到低清）
QUALITY_TARGET_HEIGHT = {
    "最佳质量": 0,       # 0 = 不强制下限，但会校验是否明显低于源最高分辨率
    "最高 1080p": 1080,
    "最高 720p": 720,
    "最高 480p": 480,
    "仅音频": 0,
}

# 下载模式
MODE_AUTO = "自动识别（推荐）"
MODE_SINGLE = "仅单视频"
MODE_PLAYLIST = "展开播放列表/清单"

DOWNLOAD_MODES = [MODE_AUTO, MODE_SINGLE, MODE_PLAYLIST]


@dataclass
class VideoItem:
    index: int
    url: str
    platform: str
    title: str = ""
    duration: str = ""
    status: str = "待处理"
    filepath: str = ""
    source: str = ""  # 来源：单视频 / 播放列表名
    playlist_title: str = ""
    playlist_index: int = 0
    video_id: str = ""
    is_partial: bool = False


def sanitize_filename(value: str) -> str:
    text = str(value or "video")
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:180] or "video")


def host_matches(host: str, *domains: str) -> bool:
    """安全判断主机名是否等于或属于某域名（避免子串误匹配，满足 CodeQL）。"""
    h = (host or "").lower().strip().split(":")[0].rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    for domain in domains:
        d = (domain or "").lower().strip().rstrip(".")
        if not d:
            continue
        if h == d or h.endswith("." + d):
            return True
    return False


def detect_platform(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host_matches(host, "youtube.com", "youtu.be", "youtube-nocookie.com"):
        return "YouTube"
    if host_matches(host, "facebook.com", "fb.com", "fb.watch", "fbcdn.net"):
        return "Facebook"
    return "其他"


def extract_youtube_video_id(url: str) -> str:
    text = unquote(str(url or "").strip())
    if not text:
        return ""
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query)

    for key in ("v", "video_id"):
        values = query.get(key) or []
        if values and YOUTUBE_ID_RE.fullmatch(values[0]):
            return values[0]

    if host_matches(host, "youtu.be"):
        part = path.strip("/").split("/")[0]
        if YOUTUBE_ID_RE.fullmatch(part):
            return part

    match = re.search(r"/(?:embed|shorts|live|v)/([A-Za-z0-9_-]{11})(?:[/?#]|$)", path)
    if match:
        return match.group(1)

    match = re.search(r"(?:^|[?&#])v=([A-Za-z0-9_-]{11})(?:[&#]|$)", text)
    if match:
        return match.group(1)
    return ""


def extract_youtube_list_id(url: str) -> str:
    text = unquote(str(url or "").strip())
    if not text:
        return ""
    qs = parse_qs(urlparse(text).query)
    values = qs.get("list") or []
    return values[0] if values else ""


def is_youtube_real_playlist_id(list_id: str) -> bool:
    text = str(list_id or "").strip()
    if not text:
        return False
    return text.startswith(YOUTUBE_REAL_PLAYLIST_PREFIXES)


def is_youtube_radio_list_id(list_id: str) -> bool:
    text = str(list_id or "").strip()
    return bool(text) and (text.startswith("RD") or text.startswith("UL") or text.startswith("RDMM"))


def clean_raw_url(url: str) -> str:
    raw = str(url or "").strip()
    raw = raw.strip(" \t\r\n\"'<>")
    raw = raw.rstrip(").,;]}>\"'")
    return raw


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_playlist_url(list_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={list_id}"


def normalize_facebook_url(url: str) -> str:
    raw = clean_raw_url(url)
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    qs = parse_qs(parsed.query)

    if host_matches(host, "fb.watch"):
        return raw.split("?")[0].rstrip("/")

    # /reel/ID or /reels/ID
    match = re.search(r"/reels?/([0-9A-Za-z._-]+)", path)
    if match:
        return f"https://www.facebook.com/reel/{match.group(1)}"

    if qs.get("v"):
        return f"https://www.facebook.com/watch/?v={qs['v'][0]}"

    # 多视频 / 相册类保留原路径，去掉常见追踪参数
    if any(x in path for x in ("/videos/", "/watch/", "/share/", "/groups/")):
        base = f"{parsed.scheme}://{parsed.netloc}{path}"
        keep = []
        for key in ("v", "story_fbid", "id", "set", "theater"):
            if key in qs and qs[key]:
                keep.append(f"{key}={qs[key][0]}")
        if keep:
            return base + "?" + "&".join(keep)
        return base.rstrip("/")

    return raw


def prepare_source_url(url: str, mode: str) -> tuple[str, str, bool]:
    """
    返回 (最终用于解析的 URL, 链接类型说明, 是否应按播放列表展开)。
    """
    raw = clean_raw_url(url)
    if not raw.startswith("http"):
        return "", "无效", False

    platform = detect_platform(raw)

    if platform == "YouTube":
        video_id = extract_youtube_video_id(raw)
        list_id = extract_youtube_list_id(raw)
        path = (urlparse(raw).path or "").lower()
        is_playlist_page = "playlist" in path and bool(list_id)

        if mode == MODE_SINGLE:
            if video_id:
                return youtube_watch_url(video_id), "单视频", False
            if is_playlist_page:
                # 仅单视频模式下，纯播放列表仍展开（否则无内容）
                return youtube_playlist_url(list_id), "播放列表", True
            return raw, "单视频", False

        if mode == MODE_PLAYLIST:
            if list_id:
                # 强制按 list 展开（含电台混播，可能很长）
                if is_playlist_page or not video_id:
                    return youtube_playlist_url(list_id), "播放列表", True
                return f"{youtube_watch_url(video_id)}&list={list_id}", "播放列表", True
            if video_id:
                return youtube_watch_url(video_id), "单视频", False
            return raw, "链接", True

        # MODE_AUTO
        if is_playlist_page and list_id:
            return youtube_playlist_url(list_id), "播放列表", True
        if list_id and is_youtube_real_playlist_id(list_id):
            # watch?v=x&list=PLxxx → 整表
            return youtube_playlist_url(list_id), "播放列表", True
        if video_id:
            # 电台混播 / 普通单视频 → 只下当前视频
            return youtube_watch_url(video_id), "单视频", False
        if list_id:
            return youtube_playlist_url(list_id), "播放列表", True
        return raw, "链接", False

    if platform == "Facebook":
        fb = normalize_facebook_url(raw)
        path = (urlparse(fb).path or "").lower()
        # 看起来像合集/多内容的路径，尝试展开
        looks_like_set = any(
            token in path
            for token in ("/set/", "/playlist", "/videos_by", "/reels_tab", "/reels/")
        ) or "set=" in fb
        # /reel/xxx 通常是单条；/reels/ 页可能是列表
        if re.search(r"/reel/[0-9A-Za-z._-]+/?$", path):
            looks_like_set = False

        if mode == MODE_SINGLE:
            return fb, "单视频", False
        if mode == MODE_PLAYLIST:
            return fb, "清单/视频", True
        # AUTO：单 reel/watch 不强制展开；合集类允许展开
        return fb, "清单" if looks_like_set else "单视频", looks_like_set

    return raw, "链接", mode != MODE_SINGLE


def extract_urls(text: str) -> list[str]:
    """提取原始链接（保留 list 等参数，后续按模式处理）。"""
    seen = set()
    urls = []
    for match in URL_RE.findall(str(text or "")):
        url = clean_raw_url(match)
        if not url.startswith("http"):
            continue
        # 轻量规范化 FB 追踪参数，但 YT 保留 list
        if detect_platform(url) == "Facebook":
            url = normalize_facebook_url(url)
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def default_ydl_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 45,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        # 断点续传核心
        "continuedl": True,
        "nopart": False,
        "updatetime": False,
        # 画质排序：分辨率 > HDR > 编码档次 > 帧率 > 视频码率 > 音频码率
        # 不要强制 android client（会只剩约 360p）
        "format_sort": [
            "res",
            "hdr:12",
            "codec:av01:vp9.2:vp9:h265:h264",
            "fps",
            "vbr",
            "abr",
            "tbr",
            "size",
        ],
        "format_sort_force": True,
        # 允许更大体积的高清流
        "prefer_free_formats": False,
    }
    ffmpeg = resolve_ffmpeg_path()
    if ffmpeg:
        # yt-dlp 接受 ffmpeg 可执行文件所在目录
        opts["ffmpeg_location"] = os.path.dirname(ffmpeg)
    opts.update(extra)
    return opts


def format_duration(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def has_ffmpeg() -> bool:
    return bool(resolve_ffmpeg_path())

def describe_selected_format(info: dict) -> str:
    """用于日志：显示最终选中的分辨率/编码，方便排查画质问题。"""
    if not info:
        return "未知"
    parts = []
    req = info.get("requested_formats")
    if req:
        for f in req:
            h = f.get("height")
            v = f.get("vcodec") or "none"
            a = f.get("acodec") or "none"
            fid = f.get("format_id")
            note = f.get("format_note") or ""
            tbr = f.get("tbr") or f.get("vbr") or f.get("abr")
            br = f"{float(tbr):.0f}kbps" if tbr else ""
            if v != "none":
                parts.append(f"视频 {h or '?'}p/{fid}/{v}/{note}/{br}".strip("/"))
            if a != "none":
                parts.append(f"音频 {fid}/{a}/{br}".strip("/"))
    else:
        h = info.get("height")
        parts.append(
            f"{h or '?'}p id={info.get('format_id')} "
            f"v={info.get('vcodec')} a={info.get('acodec')}"
        )
    return " + ".join(parts) if parts else "未知"


def selected_video_height(info: dict) -> int:
    if not info:
        return 0
    heights = []
    for f in info.get("requested_formats") or [info]:
        try:
            h = int(f.get("height") or 0)
        except (TypeError, ValueError):
            h = 0
        vcodec = (f.get("vcodec") or "none").lower()
        if h > 0 and vcodec != "none":
            heights.append(h)
    return max(heights) if heights else 0


def available_max_video_height(info: dict) -> int:
    """源视频提供的最高视频高度（用于判断是否被错误选到低清）。"""
    if not info:
        return 0
    heights = []
    for f in info.get("formats") or []:
        vcodec = (f.get("vcodec") or "none").lower()
        if vcodec in ("none", ""):
            continue
        try:
            h = int(f.get("height") or 0)
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            heights.append(h)
    return max(heights) if heights else 0


def quality_selection_ok(quality_key: str, info: dict) -> tuple[bool, str]:
    """
    校验当前选择是否合理。
    - 若源有更高清晰度，但我们选到了明显更低的流，判定失败（避免糊成 360p）。
    """
    if quality_key == "仅音频":
        return True, ""
    sel = selected_video_height(info)
    avail = available_max_video_height(info)
    target = int(QUALITY_TARGET_HEIGHT.get(quality_key, 0) or 0)

    if not sel:
        return False, "未能选中视频流（可能缺少 ffmpeg 或源无可用格式）"

    # 源本身最高就很低：允许，但提示
    if avail and avail <= 360 and sel <= avail:
        return True, f"源视频最高约 {avail}p，已选 {sel}p"

    # 期望档位：源具备目标分辨率，但实际选中远低于目标
    if target and avail >= target and sel < int(target * 0.85):
        return False, f"期望约 {target}p（源最高 {avail}p），实际只选到 {sel}p"

    # 最佳质量 / 其它：源有高清却选到了很低
    if avail >= 720 and sel <= 360:
        return False, f"源最高 {avail}p，实际只选到 {sel}p（疑似低清回退）"

    if target and sel > target + 16:
        # 理论不应发生；高度过滤失效时提示
        return True, f"实际 {sel}p 略高于目标 {target}p"

    return True, f"已选 {sel}p（源最高 {avail or sel}p）"

def entry_to_url(entry: dict, fallback: str = "") -> str:
    if not entry:
        return fallback
    for key in ("webpage_url", "original_url", "url"):
        value = entry.get(key)
        if value and str(value).startswith("http"):
            return str(value)
    video_id = entry.get("id")
    # YouTube flat entry
    if video_id and YOUTUBE_ID_RE.fullmatch(str(video_id)):
        return youtube_watch_url(str(video_id))
    if video_id and str(video_id).isdigit():
        # Facebook numeric id 常见
        return f"https://www.facebook.com/reel/{video_id}"
    return fallback


def find_existing_or_partial(target_dir: str, title: str, video_id: str) -> tuple[str, bool]:
    """
    返回 (path, is_complete)。
    完整文件优先；否则返回 .part 以便续传提示。
    """
    if not os.path.isdir(target_dir):
        return "", False

    safe_title = sanitize_filename(title)
    complete = []
    partial = []

    for name in os.listdir(target_dir):
        path = os.path.join(target_dir, name)
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            continue
        matched = False
        if video_id and f"[{video_id}]" in name:
            matched = True
        elif safe_title and (name.startswith(safe_title) or safe_title in name):
            matched = True
        if not matched:
            continue
        if name.endswith(".part"):
            partial.append(path)
        else:
            # 跳过 yt-dlp 中间分片 f401/f251 等未合并文件时的误判：仅当无主文件时才算
            if re.search(r"\.f\d+\.(mp4|webm|m4a)$", name, re.I):
                partial.append(path)
            else:
                complete.append(path)

    if complete:
        # 优先非临时扩展
        complete.sort(key=lambda p: (0 if p.lower().endswith((".mp4", ".webm", ".mkv", ".mp3", ".m4a")) else 1, -os.path.getsize(p)))
        return complete[0], True
    if partial:
        partial.sort(key=lambda p: -os.path.getsize(p))
        return partial[0], False
    return "", False


def expand_source_urls(
    urls: list[str],
    mode: str,
    playlist_limit: int = 0,
    stop_event: threading.Event | None = None,
    on_item: Callable[[VideoItem], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> list[VideoItem]:
    """
    自动解析并展开单视频 / 播放列表 / FB 清单。
    下载前无需单独预览，开始下载时会调用本函数。
    """
    def log(msg: str):
        if on_log:
            on_log(msg)

    if yt_dlp is None:
        raise RuntimeError("未安装 yt-dlp。请执行：pip install yt-dlp")

    items: list[VideoItem] = []
    index = 0
    limit = max(0, int(playlist_limit or 0))

    for raw_url in urls:
        if stop_event is not None and stop_event.is_set():
            log("加载已停止。")
            break

        source_url, kind, expand = prepare_source_url(raw_url, mode)
        platform = detect_platform(source_url or raw_url)
        if source_url != raw_url:
            log(f"自动加载 [{kind}]：{raw_url} -> {source_url}")
        else:
            log(f"自动加载 [{platform}/{kind}]：{source_url}")

        opts = default_ydl_opts(
            skip_download=True,
            extract_flat="in_playlist",
            noplaylist=not expand,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source_url, download=False)
        except Exception as exc:
            index += 1
            item = VideoItem(
                index=index,
                url=source_url or raw_url,
                platform=platform,
                title=f"链接-{index}",
                status=f"解析失败：{exc}",
                source=kind,
            )
            items.append(item)
            if on_item:
                on_item(item)
            log(f"解析失败：{source_url} -> {exc}")
            continue

        if info is None:
            index += 1
            item = VideoItem(
                index=index,
                url=source_url,
                platform=platform,
                title=f"链接-{index}",
                status="解析失败：无结果",
                source=kind,
            )
            items.append(item)
            if on_item:
                on_item(item)
            continue

        playlist_title = ""
        if info.get("_type") == "playlist" or (expand and info.get("entries") is not None):
            playlist_title = sanitize_filename(info.get("title") or info.get("id") or "播放列表")
            entries = [e for e in (info.get("entries") or []) if e]
            if limit > 0:
                entries = entries[:limit]
            if not entries and info.get("id") and info.get("_type") != "playlist":
                entries = [info]
            log(f"播放列表/清单「{playlist_title}」自动展开 {len(entries)} 项。")
        else:
            entries = [info]

        if not entries:
            index += 1
            item = VideoItem(
                index=index,
                url=source_url,
                platform=platform,
                title=playlist_title or f"链接-{index}",
                status="解析失败：列表为空（可能需登录或无公开权限）",
                source=kind,
                playlist_title=playlist_title,
            )
            items.append(item)
            if on_item:
                on_item(item)
            continue

        for pos, entry in enumerate(entries, start=1):
            if stop_event is not None and stop_event.is_set():
                break
            index += 1
            entry_url = entry_to_url(entry, source_url)
            if detect_platform(entry_url) == "YouTube":
                vid = extract_youtube_video_id(entry_url) or (
                    entry.get("id") if YOUTUBE_ID_RE.fullmatch(str(entry.get("id") or "")) else ""
                )
                if vid:
                    entry_url = youtube_watch_url(str(vid))

            title = sanitize_filename(
                entry.get("title")
                or entry.get("id")
                or f"{playlist_title or '视频'}-{pos}"
            )
            video_id = str(entry.get("id") or extract_youtube_video_id(entry_url) or "")
            item = VideoItem(
                index=index,
                url=entry_url,
                platform=detect_platform(entry_url) or platform,
                title=title,
                duration=format_duration(entry.get("duration")),
                status="待下载",
                source=playlist_title if playlist_title else kind,
                playlist_title=playlist_title,
                playlist_index=pos if playlist_title else 0,
                video_id=video_id,
            )
            items.append(item)
            if on_item:
                on_item(item)

        if playlist_title:
            log(f"已自动展开「{playlist_title}」→ {len(entries)} 个视频")
        elif items:
            log(f"加载成功 [{platform}] {items[-1].title}")

    return items


class Card(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(18, 16, 18, 16)
        self.layout.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        self.layout.addWidget(title_label)


class VideoPreviewWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    item_ready = Signal(object)
    done = Signal(list)

    def __init__(self, urls: list[str], mode: str, playlist_limit: int):
        super().__init__()
        self.urls = urls
        self.mode = mode
        self.playlist_limit = max(0, int(playlist_limit or 0))
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        if yt_dlp is None:
            self.failed.emit("未安装 yt-dlp。请执行：pip install yt-dlp")
            self.done.emit([])
            return
        try:
            items = expand_source_urls(
                self.urls,
                self.mode,
                self.playlist_limit,
                stop_event=self.stop_event,
                on_item=lambda item: self.item_ready.emit(item),
                on_log=lambda msg: self.log.emit(msg),
            )
            self.done.emit(items)
        except Exception as exc:
            self.failed.emit(f"预览失败：{exc}")
            self.done.emit([])


class VideoDownloadWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    item_update = Signal(object)
    progress = Signal(dict)
    done = Signal()

    def __init__(
        self,
        source_urls: list[str],
        output_dir: str,
        quality_key: str,
        skip_existing: bool,
        split_by_platform: bool,
        playlist_subfolder: bool,
        resume: bool,
        mode: str,
        playlist_limit: int,
    ):
        super().__init__()
        self.source_urls = list(source_urls)
        self.items: list[VideoItem] = []
        self.output_dir = output_dir
        self.quality_key = quality_key
        self.skip_existing = skip_existing
        self.split_by_platform = split_by_platform
        self.playlist_subfolder = playlist_subfolder
        self.resume = resume
        self.mode = mode
        self.playlist_limit = max(0, int(playlist_limit or 0))
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def _target_dir(self, item: VideoItem) -> str:
        parts = [self.output_dir]
        if self.split_by_platform and item.platform in ("YouTube", "Facebook"):
            parts.append(item.platform)
        if self.playlist_subfolder and item.playlist_title:
            parts.append(sanitize_filename(item.playlist_title))
        path = os.path.join(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    def _outtmpl(self, target_dir: str, item: VideoItem) -> str:
        if item.playlist_index:
            # 播放列表按序号命名，方便排序与续传识别
            name = f"{item.playlist_index:03d} - %(title).160B [%(id)s].%(ext)s"
        else:
            name = "%(title).180B [%(id)s].%(ext)s"
        return os.path.join(target_dir, name)

    def _build_opts(self, target_dir: str, item: VideoItem, progress_hook: Callable, format_override: str = "") -> dict:
        fmt = format_override or QUALITY_OPTIONS.get(self.quality_key, QUALITY_OPTIONS["最佳质量"])
        ffmpeg = resolve_ffmpeg_path()
        opts = default_ydl_opts(
            outtmpl=self._outtmpl(target_dir, item),
            format=fmt,
            progress_hooks=[progress_hook],
            ignoreerrors=False,
            continuedl=self.resume,
            noplaylist=True,  # 单项下载，列表已在开始时自动展开
            noprogress=True,
            # 断点续传相关
            overwrites=not self.skip_existing,
            # 网络不稳时更耐用
            retries=15,
            fragment_retries=15,
        )
        if ffmpeg:
            opts["ffmpeg_location"] = os.path.dirname(ffmpeg)
            # mkv 可无损封装 H.264/VP9/AV1+多种音频，避免为塞进 mp4 而选低清或转码变糊
            opts["merge_output_format"] = "mkv"
            self.log.emit(f"使用 ffmpeg：{ffmpeg}（合并容器 mkv，保留原画编码）")
        else:
            # 切勿降级为单文件 b（YouTube 常只有 360p）
            self.log.emit(
                "警告：未检测到 ffmpeg。所有高清档位（480p/720p/1080p/最佳）"
                "都需要合并视频+音频；缺少 ffmpeg 时会失败或严重掉画质。"
                "请安装 ffmpeg 并加入 PATH。"
            )
        if self.quality_key == "仅音频":
            if ffmpeg:
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }]
            else:
                self.log.emit("未检测到 ffmpeg，仅音频将保留原始音频格式。")
        return opts

    def _retry_format_candidates(self) -> list[str]:
        """画质异常时的备选格式串：仍然只走分离高清流，绝不回退 360p progressive。"""
        target = int(QUALITY_TARGET_HEIGHT.get(self.quality_key, 0) or 0)
        cands = []
        if target > 0:
            cands.append(f"bestvideo*[height<={target}]+bestaudio")
            cands.append(f"bestvideo[height<={target}]+bestaudio")
            cands.append(f"bestvideo*[height<={target}][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo*[height<={target}]+bestaudio")
        if self.quality_key == "最佳质量":
            cands.extend([
                "bestvideo*+bestaudio",
                "bestvideo+bestaudio",
                "bestvideo*[vcodec^=av01]+bestaudio/bestvideo*[vcodec^=vp09]+bestaudio/bestvideo*+bestaudio",
            ])
        # 去重并去掉与主格式完全相同的
        primary = QUALITY_OPTIONS.get(self.quality_key, "")
        out = []
        for c in cands:
            if c and c != primary and c not in out:
                out.append(c)
        return out

    def run(self):
        if yt_dlp is None:
            self.failed.emit("未安装 yt-dlp。请执行：pip install yt-dlp")
            self.done.emit()
            return

        success = skipped = failed = resumed = 0
        os.makedirs(self.output_dir, exist_ok=True)

        # 自动加载：单视频 / 播放列表无需先点解析预览
        self.log.emit(f"正在自动加载 {len(self.source_urls)} 个链接（{self.mode}）...")
        try:
            items = expand_source_urls(
                self.source_urls,
                self.mode,
                self.playlist_limit,
                stop_event=self.stop_event,
                on_item=lambda item: self.item_update.emit(item),
                on_log=lambda msg: self.log.emit(msg),
            )
        except Exception as exc:
            self.failed.emit(f"自动加载失败：{exc}")
            self.done.emit()
            return

        self.items = items
        downloadable = [i for i in items if not str(i.status).startswith("解析失败")]
        self.log.emit(
            f"自动加载完成：共 {len(items)} 项，可下载 {len(downloadable)} | "
            f"断点续传：{'开' if self.resume else '关'} | 目录：{self.output_dir}"
        )
        if not downloadable:
            self.log.emit("没有可下载的视频。")
            self.done.emit()
            return

        for item in downloadable:
            if self.stop_event.is_set():
                self.log.emit("任务已停止。未完成的文件已保留，可再次开始续传。")
                break

            item.status = "下载中"
            self.item_update.emit(item)
            target_dir = self._target_dir(item)
            last_file = {"path": ""}

            def hook(d, current=item, file_box=last_file):
                if self.stop_event.is_set():
                    raise Exception("用户停止下载")
                status = d.get("status")
                if status == "downloading":
                    percent = (d.get("_percent_str") or "").strip()
                    speed = (d.get("_speed_str") or "").strip()
                    eta = (d.get("_eta_str") or "").strip()
                    downloaded = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    if total and downloaded:
                        # yt-dlp 续传时 percent 会从已有进度继续
                        current.status = f"下载中 {percent} {speed} ETA {eta}".strip()
                    else:
                        current.status = f"下载中 {percent} {speed}".strip()
                    self.item_update.emit(current)
                elif status == "finished":
                    filename = d.get("filename") or ""
                    if filename:
                        file_box["path"] = filename
                    current.status = "处理中/合并"
                    self.item_update.emit(current)

            try:
                # 规范化单条 URL
                if detect_platform(item.url) == "YouTube":
                    vid = extract_youtube_video_id(item.url) or item.video_id
                    if vid and YOUTUBE_ID_RE.fullmatch(str(vid)):
                        item.url = youtube_watch_url(str(vid))
                        item.video_id = str(vid)
                elif detect_platform(item.url) == "Facebook":
                    item.url = normalize_facebook_url(item.url)

                path, is_complete = find_existing_or_partial(
                    target_dir, item.title, item.video_id or extract_youtube_video_id(item.url)
                )
                if path and is_complete and self.skip_existing:
                    item.filepath = path
                    item.status = "已存在，跳过"
                    skipped += 1
                    self.item_update.emit(item)
                    self.progress.emit({
                        "success": success, "skipped": skipped, "failed": failed, "resumed": resumed,
                    })
                    self.log.emit(f"已存在，跳过：{path}")
                    continue
                if path and not is_complete and self.resume:
                    resumed += 1
                    item.is_partial = True
                    item.status = "断点续传中"
                    self.item_update.emit(item)
                    self.log.emit(f"发现未完成文件，继续下载：{path}")

                # 主格式 + 备选格式：全部档位都走高清分离流，失败才换下一串
                format_try_list = [QUALITY_OPTIONS.get(self.quality_key, QUALITY_OPTIONS["最佳质量"])]
                format_try_list.extend(self._retry_format_candidates())
                last_err = None
                downloaded = False

                for fmt_idx, fmt in enumerate(format_try_list):
                    if self.stop_event.is_set():
                        break
                    opts = self._build_opts(target_dir, item, hook, format_override=fmt)
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(item.url, download=False)
                            if info:
                                if info.get("_type") == "playlist":
                                    entries = [e for e in (info.get("entries") or []) if e]
                                    info = entries[0] if entries else info
                                item.title = sanitize_filename(info.get("title") or item.title)
                                item.duration = item.duration or format_duration(info.get("duration"))
                                item.video_id = str(info.get("id") or item.video_id or "")
                                if detect_platform(item.url) == "YouTube" and item.video_id and YOUTUBE_ID_RE.fullmatch(item.video_id):
                                    item.url = youtube_watch_url(item.video_id)
                                fmt_desc = describe_selected_format(info)
                                self.log.emit(
                                    f"画质尝试 {fmt_idx + 1}/{len(format_try_list)} [{self.quality_key}]：{fmt_desc}"
                                )
                                ok, reason = quality_selection_ok(self.quality_key, info)
                                self.log.emit(f"画质校验：{reason}")
                                if not ok:
                                    # 源明明有高清却选到低清：换下一组格式串重试
                                    last_err = RuntimeError(reason)
                                    self.log.emit(f"画质不达标，换用备用格式…（{reason}）")
                                    continue

                            ydl.download([item.url])
                            downloaded = True
                            break
                    except Exception as exc:
                        last_err = exc
                        self.log.emit(f"格式串失败，尝试备用：{exc}")
                        continue

                if not downloaded:
                    raise last_err or RuntimeError("所有画质格式均失败")

                saved = last_file["path"] or find_existing_or_partial(
                    target_dir, item.title, item.video_id
                )[0]
                # 合并后清理误指向中间分片
                if saved and re.search(r"\.f\d+\.(mp4|webm|m4a)(\.part)?$", saved, re.I):
                    final_path, complete = find_existing_or_partial(target_dir, item.title, item.video_id)
                    if final_path and complete:
                        saved = final_path

                item.filepath = saved
                item.status = "成功"
                success += 1
                self.item_update.emit(item)
                self.progress.emit({
                    "success": success, "skipped": skipped, "failed": failed, "resumed": resumed,
                })
                self.log.emit(f"成功：{saved or item.title}")
                time.sleep(0.05)
            except Exception as exc:
                if self.stop_event.is_set() or "用户停止" in str(exc):
                    item.status = "已停止（可续传）"
                    self.item_update.emit(item)
                    self.log.emit("任务已停止。未完成文件已保留，下次开始可断点续传。")
                    break
                failed += 1
                item.status = f"失败：{exc}"
                self.item_update.emit(item)
                self.progress.emit({
                    "success": success, "skipped": skipped, "failed": failed, "resumed": resumed,
                })
                self.log.emit(f"失败：{item.url} -> {exc}")

        self.log.emit(
            f"完成：成功 {success}，跳过 {skipped}，续传 {resumed}，失败 {failed}。"
        )
        self.done.emit()


class EnvInstallWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            report = install_missing_components(
                progress=lambda d, t: self.progress.emit(d, t),
                log=lambda m: self.log.emit(m),
            )
            self.finished_ok.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))


class VideoBatchPage(QWidget):
    """独立的 YouTube / Facebook 批量下载板块。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.env_worker = None
        self.preview_items: list[VideoItem] = []
        self.build_ui()
        self.connect_signals()
        self.refresh_env_status(silent=True)

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        tip = QLabel(
            "粘贴链接后可直接点「开始下载」：会自动加载单视频/播放列表并开始下载，"
            "无需先解析。也可先点「解析预览」只看列表不下载。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        root.addWidget(tip)

        # 环境检测条
        env_bar = QFrame()
        env_bar.setObjectName("compactPanel")
        env_layout = QHBoxLayout(env_bar)
        env_layout.setContentsMargins(14, 10, 14, 10)
        env_layout.setSpacing(10)
        root.addWidget(env_bar)

        self.env_status_label = QLabel("正在检测运行环境…")
        self.env_status_label.setObjectName("subtitle")
        self.env_status_label.setWordWrap(True)
        env_layout.addWidget(self.env_status_label, 1)

        self.env_check_btn = QPushButton("检测环境")
        self.env_check_btn.setObjectName("secondaryButton")
        self.env_install_btn = QPushButton("一键安装组件")
        self.env_install_btn.setObjectName("primaryButton")
        self.env_install_btn.setToolTip(
            "自动下载并安装高清合并所需的 ffmpeg / ffprobe 到本机用户目录。\n"
            "源码运行时如缺 yt-dlp 也会尝试 pip 安装。"
        )
        env_layout.addWidget(self.env_check_btn)
        env_layout.addWidget(self.env_install_btn)

        settings = QFrame()
        settings.setObjectName("compactPanel")
        grid = QGridLayout(settings)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        root.addWidget(settings)

        self.output_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "Downloads", "视频批量下载"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_OPTIONS.keys()))
        self.quality_combo.setCurrentText("最佳质量")
        self.quality_combo.setToolTip(
            "全部档位均使用「最佳视频轨+最佳音轨」合并，不会故意下 360p 糊图。\n"
            "· 最佳质量：源站最高分辨率/码率（可能 1440p/4K）\n"
            "· 最高 1080p / 720p / 480p：不超过该高度内的最高画质\n"
            "· 仅音频：最高码率音轨\n"
            "需要本机 ffmpeg 才能合并高清（日志会提示是否检测到）。"
        )

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(DOWNLOAD_MODES)
        self.mode_combo.setCurrentText(MODE_AUTO)
        self.mode_combo.setToolTip(
            "自动识别：真实播放列表(PL/UU等)整表下载；电台混播(RD)只下当前视频。\n"
            "仅单视频：忽略列表参数，只下载当前视频。\n"
            "展开播放列表/清单：强制展开 YouTube 列表与 FB 可识别清单。"
        )

        self.playlist_limit_spin = QSpinBox()
        self.playlist_limit_spin.setRange(0, 5000)
        self.playlist_limit_spin.setValue(0)
        self.playlist_limit_spin.setSpecialValueText("不限制")
        self.playlist_limit_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.playlist_limit_spin.setToolTip("0 表示不限制。可防止超长列表一次下太多。")

        self.skip_existing_check = QCheckBox("已完成则跳过")
        self.skip_existing_check.setChecked(True)
        self.resume_check = QCheckBox("断点续传")
        self.resume_check.setChecked(True)
        self.resume_check.setToolTip("保留 .part 临时文件，中断后再次开始可从断点继续。")
        self.split_platform_check = QCheckBox("按平台分文件夹")
        self.split_platform_check.setChecked(True)
        self.playlist_folder_check = QCheckBox("播放列表分子文件夹")
        self.playlist_folder_check.setChecked(True)

        self._add_field(grid, "下载目录", self.output_edit, 0, 0, 1, 4)
        choose_btn = QPushButton("选择")
        choose_btn.setObjectName("secondaryButton")
        choose_btn.clicked.connect(self.choose_output_dir)
        grid.addWidget(self._wrap_button(choose_btn), 0, 4)
        self._add_field(grid, "画质", self.quality_combo, 0, 5)

        self._add_field(grid, "下载模式", self.mode_combo, 1, 0, 1, 2)
        self._add_field(grid, "列表上限", self.playlist_limit_spin, 1, 2)

        options = QHBoxLayout()
        options.setSpacing(12)
        options.addWidget(self.resume_check)
        options.addWidget(self.skip_existing_check)
        options.addWidget(self.split_platform_check)
        options.addWidget(self.playlist_folder_check)
        options.addStretch()
        grid.addLayout(options, 1, 3, 1, 3)

        for col in range(6):
            grid.setColumnStretch(col, 1)

        body = QHBoxLayout()
        body.setSpacing(10)
        root.addLayout(body, 5)

        left = Card("视频 / 播放列表链接（支持批量）")
        right_preview = Card("预览 / 进度")
        right_log = Card("日志")
        body.addWidget(left, 4)
        right_col = QVBoxLayout()
        right_col.setSpacing(10)
        right_col.addWidget(right_preview, 3)
        right_col.addWidget(right_log, 2)
        body.addLayout(right_col, 6)

        self.links_edit = QTextEdit()
        self.links_edit.setObjectName("pasteTextBox")
        self.links_edit.setPlaceholderText(
            "每行一个链接，可混合粘贴多个，例如：\n\n"
            "【YouTube 单视频】\n"
            "https://www.youtube.com/watch?v=xxxxxxxx\n"
            "https://youtu.be/xxxxxxxx\n"
            "https://www.youtube.com/watch?v=xxxxxxxx&list=RDxxxx&start_radio=1\n"
            "  （电台混播在自动模式下只下当前视频）\n\n"
            "【YouTube 播放列表】\n"
            "https://www.youtube.com/playlist?list=PLxxxxxxxx\n"
            "https://www.youtube.com/watch?v=xxxx&list=PLxxxxxxxx\n\n"
            "【Facebook】\n"
            "https://www.facebook.com/reel/xxxxxxxx\n"
            "https://www.facebook.com/watch/?v=xxxxxxxx\n"
            "https://fb.watch/xxxxxxxx/\n"
            "以及可公开访问的 Reels/视频清单链接\n\n"
            "直接点「开始下载」即可：播放列表会自动展开并逐个下载。\n"
            "「解析预览」可选，只用于提前查看列表内容。"
        )
        left.layout.addWidget(self.links_edit, 1)

        link_actions_top = QHBoxLayout()
        link_actions_top.setSpacing(8)
        link_actions_bottom = QHBoxLayout()
        link_actions_bottom.setSpacing(8)
        left.layout.addLayout(link_actions_top)
        left.layout.addLayout(link_actions_bottom)

        self.clipboard_btn = QPushButton("从剪贴板读取")
        self.clipboard_btn.setObjectName("secondaryButton")
        self.clear_links_btn = QPushButton("清空链接")
        self.clear_links_btn.setObjectName("ghostButton")
        self.preview_btn = QPushButton("解析预览")
        self.preview_btn.setObjectName("secondaryButton")
        self.start_btn = QPushButton("开始下载")
        self.start_btn.setObjectName("primaryButton")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.open_folder_btn = QPushButton("打开文件夹")
        self.open_folder_btn.setObjectName("secondaryButton")

        for btn in (self.clipboard_btn, self.clear_links_btn, self.preview_btn):
            link_actions_top.addWidget(btn)
        link_actions_top.addStretch()
        for btn in (self.start_btn, self.stop_btn, self.open_folder_btn):
            link_actions_bottom.addWidget(btn)
        link_actions_bottom.addStretch()

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "平台", "来源", "标题", "时长", "状态", "链接"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 220)
        self.table.setColumnWidth(4, 70)
        self.table.setColumnWidth(5, 150)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setAlternatingRowColors(True)
        right_preview.layout.addWidget(self.table)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        right_log.layout.addWidget(self.log_box)

        self.status_row = QLabel("等待开始 · 粘贴后可直接下载（自动加载列表）")
        self.status_row.setObjectName("status")
        root.addWidget(self.status_row)

        env_tip = "yt-dlp 已就绪" if yt_dlp is not None else "未安装 yt-dlp，请先 pip install yt-dlp"
        if yt_dlp is not None:
            ff = resolve_ffmpeg_path()
            if ff:
                env_tip += f" · ffmpeg 已就绪（高清合并）"
            else:
                env_tip += " · 未检测到 ffmpeg：1080p 可能失败或严重掉画质，请安装 ffmpeg"
        env_tip += " · 断点续传默认开启 · 开始下载时自动加载链接"
        self.log(env_tip)

    def _wrap_button(self, button: QPushButton) -> QFrame:
        field = QFrame()
        field.setObjectName("fieldBox")
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        caption = QLabel(" ")
        caption.setObjectName("fieldLabel")
        box.addWidget(caption)
        box.addWidget(button)
        return field

    def _add_field(self, grid, label, widget, row, col, row_span=1, col_span=1):
        field = QFrame()
        field.setObjectName("fieldBox")
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        box.addWidget(caption)
        box.addWidget(widget)
        grid.addWidget(field, row, col, row_span, col_span)
        return field

    def connect_signals(self):
        self.clipboard_btn.clicked.connect(self.load_clipboard)
        self.clear_links_btn.clicked.connect(self.links_edit.clear)
        self.preview_btn.clicked.connect(self.start_preview)
        self.start_btn.clicked.connect(self.start_download)
        self.stop_btn.clicked.connect(self.stop_task)
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        self.env_check_btn.clicked.connect(lambda: self.refresh_env_status(silent=False))
        self.env_install_btn.clicked.connect(self.install_env_components)

    def refresh_env_status(self, silent: bool = True):
        report = scan_environment()
        self.env_status_label.setText(report.summary_line())
        missing = report.missing_required
        self.env_install_btn.setEnabled(bool(missing) or not report.ready_for_hd)
        if report.ready_for_hd:
            self.env_status_label.setStyleSheet("color: #15803d; font-weight: 800;")
            self.env_install_btn.setText("组件已就绪")
        else:
            self.env_status_label.setStyleSheet("color: #ca8a04; font-weight: 800;")
            names = "、".join(c.name for c in missing) or "组件"
            self.env_install_btn.setText(f"一键安装（缺 {names}）")
        if not silent:
            self.log(report.summary_line())
            for c in report.components:
                mark = "OK" if c.ok else "缺"
                detail = c.detail or c.path or ""
                self.log(f"[{mark}] {c.name}: {detail}")
            if missing:
                self.log("可点击「一键安装组件」自动下载安装 ffmpeg 等。")
            else:
                self.log("高清下载环境已就绪。")
        return report

    def install_env_components(self):
        if self.env_worker and self.env_worker.isRunning():
            QMessageBox.information(self, APP_SECTION, "正在安装组件，请稍候。")
            return
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "请先停止当前下载任务，再安装组件。")
            return

        report = scan_environment()
        if report.ready_for_hd:
            QMessageBox.information(
                self,
                APP_SECTION,
                "必需组件已就绪。\n\n"
                f"{report.summary_line()}\n\n"
                "如需重装 ffmpeg：删除 %LOCALAPPDATA%\\DIYDownloader\\tools 后再次点击安装。",
            )
            self.refresh_env_status(silent=False)
            return

        reply = QMessageBox.question(
            self,
            APP_SECTION,
            "将自动下载并安装高清视频合并所需组件：\n\n"
            "· ffmpeg / ffprobe（必需，约数十 MB）\n"
            "· yt-dlp（仅源码运行且缺失时）\n\n"
            "安装位置：%LOCALAPPDATA%\\DIYDownloader\\tools\\bin\n\n"
            "是否开始一键安装？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.env_check_btn.setEnabled(False)
        self.env_install_btn.setEnabled(False)
        self.env_status_label.setText("正在安装组件，请稍候…")
        self.log("开始一键安装运行环境组件…")

        worker = EnvInstallWorker()
        worker.log.connect(self.log)
        worker.progress.connect(self.on_env_install_progress)
        worker.finished_ok.connect(self.on_env_install_done)
        worker.failed.connect(self.on_env_install_failed)
        worker.finished.connect(self.on_env_worker_finished)
        self.env_worker = worker
        worker.start()

    def on_env_install_progress(self, done: int, total: int):
        if total:
            pct = int(done * 100 / max(total, 1))
            self.env_status_label.setText(f"正在下载组件… {pct}%")
            self.status_row.setText(f"组件下载 {pct}%")

    def on_env_install_done(self, report):
        self.refresh_env_status(silent=False)
        if report.ready_for_hd:
            QMessageBox.information(
                self,
                APP_SECTION,
                "组件安装完成，高清下载环境已就绪。\n\n"
                f"{report.summary_line()}\n\n"
                "请重新选择画质并开始下载。",
            )
            self.log("组件安装完成，可以开始高清下载。")
        else:
            missing = "、".join(c.name for c in report.missing_required) or "未知"
            QMessageBox.warning(
                self,
                APP_SECTION,
                f"安装流程结束，但仍缺少：{missing}\n\n请查看日志或手动安装 ffmpeg。",
            )

    def on_env_install_failed(self, message: str):
        self.log(f"组件安装失败：{message}")
        self.env_status_label.setText(f"安装失败：{message}")
        self.refresh_env_status(silent=True)
        QMessageBox.warning(
            self,
            APP_SECTION,
            f"一键安装失败：\n{message}\n\n"
            "可手动安装 ffmpeg 并加入 PATH，或检查网络后重试。",
        )

    def on_env_worker_finished(self):
        self.env_check_btn.setEnabled(True)
        self.env_install_btn.setEnabled(True)
        if self.sender() is self.env_worker:
            self.env_worker = None

    def log(self, message: str):
        now = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{now}] {message}")

    def choose_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择视频下载目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def open_output_folder(self):
        path = self.output_edit.text().strip()
        if not path:
            QMessageBox.information(self, APP_SECTION, "请先选择下载目录。")
            return
        os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def load_clipboard(self):
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, APP_SECTION, "剪贴板为空。")
            return
        current = self.links_edit.toPlainText().strip()
        if current:
            self.links_edit.setPlainText(current + "\n" + text.strip())
        else:
            self.links_edit.setPlainText(text.strip())
        self.log("已从剪贴板读取内容。")

    def has_running_worker(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def set_running_state(self, running: bool):
        self.preview_btn.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.clipboard_btn.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def collect_urls(self) -> list[str]:
        return extract_urls(self.links_edit.toPlainText())

    def current_mode(self) -> str:
        return self.mode_combo.currentText()

    def _row_values(self, item: VideoItem) -> list:
        return [
            item.index,
            item.platform,
            item.source or "",
            item.title,
            item.duration,
            item.status,
            item.url,
        ]

    def fill_table(self, items: list[VideoItem]):
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            for col, value in enumerate(self._row_values(item)):
                cell = QTableWidgetItem("" if value is None else str(value))
                self._style_cell(cell, col, item)
                self.table.setItem(row, col, cell)

    def _style_cell(self, cell: QTableWidgetItem, col: int, item: VideoItem):
        if col == 1:
            if item.platform == "YouTube":
                cell.setForeground(QColor("#ef4444"))
            elif item.platform == "Facebook":
                cell.setForeground(QColor("#3b82f6"))
        if col == 5:
            status = str(item.status or "")
            if status.startswith("成功") or status == "待下载":
                cell.setForeground(QColor("#15803d"))
            elif "失败" in status or "解析失败" in status:
                cell.setForeground(QColor("#dc2626"))
            elif "跳过" in status or "续传" in status or "停止" in status:
                cell.setForeground(QColor("#ca8a04"))

    def upsert_table_item(self, item: VideoItem):
        for row in range(self.table.rowCount()):
            index_item = self.table.item(row, 0)
            if index_item and index_item.text() == str(item.index):
                for col, value in enumerate(self._row_values(item)):
                    cell = self.table.item(row, col)
                    if cell is None:
                        cell = QTableWidgetItem()
                        self.table.setItem(row, col, cell)
                    cell.setText("" if value is None else str(value))
                    self._style_cell(cell, col, item)
                return
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, value in enumerate(self._row_values(item)):
            cell = QTableWidgetItem("" if value is None else str(value))
            self._style_cell(cell, col, item)
            self.table.setItem(row, col, cell)

    def start_preview(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "当前任务还在运行，请结束后再预览。")
            return
        if yt_dlp is None:
            QMessageBox.warning(self, APP_SECTION, "未安装 yt-dlp。\n请先执行：pip install yt-dlp")
            return
        urls = self.collect_urls()
        if not urls:
            QMessageBox.information(self, APP_SECTION, "没有识别到有效链接。")
            return

        mode = self.current_mode()
        limit = self.playlist_limit_spin.value()
        self.preview_items = []
        self.table.setRowCount(0)
        self.set_running_state(True)
        self.status_row.setText(f"正在解析 {len(urls)} 个链接（{mode}）...")
        self.log(f"开始解析 {len(urls)} 个链接，模式：{mode}" + (f"，列表上限 {limit}" if limit else ""))

        worker = VideoPreviewWorker(urls, mode, limit)
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.item_ready.connect(self.upsert_table_item)
        worker.done.connect(self.on_preview_done)
        worker.finished.connect(self.on_worker_finished)
        self.worker = worker
        worker.start()

    def on_preview_done(self, items: list[VideoItem]):
        self.preview_items = list(items)
        self.fill_table(self.preview_items)
        ok = sum(1 for i in items if i.status == "待下载")
        playlists = len({i.playlist_title for i in items if i.playlist_title})
        self.status_row.setText(f"预览完成：{ok}/{len(items)} 可下载" + (f"，含 {playlists} 个列表" if playlists else ""))
        self.log(f"预览完成：可下载 {ok}，共 {len(items)} 项" + (f"，播放列表 {playlists} 个" if playlists else "") + "。")

    def start_download(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "当前任务还在运行。")
            return
        if yt_dlp is None:
            QMessageBox.warning(self, APP_SECTION, "未安装 yt-dlp。\n请先执行：pip install yt-dlp")
            return

        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, APP_SECTION, "请先选择下载目录。")
            return

        urls = self.collect_urls()
        if not urls:
            QMessageBox.information(self, APP_SECTION, "请先粘贴视频或播放列表链接。")
            return

        # 下载前强制检查高清环境
        env = self.refresh_env_status(silent=True)
        if not env.ready_for_hd:
            missing = "、".join(c.name for c in env.missing_required) or "ffmpeg"
            ask = QMessageBox.question(
                self,
                APP_SECTION,
                f"检测到缺少必需组件：{missing}\n\n"
                "没有 ffmpeg 时高清视频会合并失败或严重掉画质。\n\n"
                "是否现在一键安装组件？\n"
                "（选「No」将仍尝试下载，但不推荐）",
            )
            if ask == QMessageBox.StandardButton.Yes:
                self.install_env_components()
                return

        mode = self.current_mode()
        limit = self.playlist_limit_spin.value()

        # 直接下载：自动加载列表，不依赖「解析预览」
        self.preview_items = []
        self.table.setRowCount(0)
        self.set_running_state(True)
        self.status_row.setText(f"正在自动加载并下载（{len(urls)} 个链接）...")
        self.log(f"开始：自动加载 {len(urls)} 个链接后下载，模式：{mode}")

        worker = VideoDownloadWorker(
            source_urls=urls,
            output_dir=output_dir,
            quality_key=self.quality_combo.currentText(),
            skip_existing=self.skip_existing_check.isChecked(),
            split_by_platform=self.split_platform_check.isChecked(),
            playlist_subfolder=self.playlist_folder_check.isChecked(),
            resume=self.resume_check.isChecked(),
            mode=mode,
            playlist_limit=limit,
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.item_update.connect(self.on_item_update)
        worker.progress.connect(self.on_progress)
        worker.done.connect(self.on_download_done)
        worker.finished.connect(self.on_worker_finished)
        self.worker = worker
        worker.start()

    def on_item_update(self, item: VideoItem):
        found = False
        for idx, old in enumerate(self.preview_items):
            if old.index == item.index:
                self.preview_items[idx] = item
                found = True
                break
        if not found:
            self.preview_items.append(item)
        self.upsert_table_item(item)

    def on_progress(self, stats: dict):
        self.status_row.setText(
            f"成功 {stats.get('success', 0)}，跳过 {stats.get('skipped', 0)}，"
            f"续传 {stats.get('resumed', 0)}，失败 {stats.get('failed', 0)}"
        )

    def on_download_done(self):
        self.status_row.setText("视频下载任务结束（未完成文件可断点续传）")

    def stop_task(self):
        if self.worker and hasattr(self.worker, "stop"):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.status_row.setText("正在停止（已下载部分会保留以便续传）...")
            self.log("已请求停止。未完成文件会保留，再次开始可断点续传。")
        else:
            self.set_running_state(False)

    def on_worker_finished(self):
        if self.sender() is self.worker:
            self.worker = None
        self.set_running_state(False)

    def show_error(self, message: str):
        self.log(message)
        self.status_row.setText("出现错误")
        QMessageBox.warning(self, APP_SECTION, message)

    def request_close(self) -> bool:
        if not self.has_running_worker():
            return True
        if hasattr(self.worker, "stop"):
            self.stop_task()
        if not self.worker.wait(3000):
            return False
        return True
