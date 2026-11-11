"""YouTube / Facebook / TikTok / Instagram 视频批量下载板块（基于 yt-dlp）。

支持：
- YouTube 单视频 / 播放列表
- Facebook 单视频 / Reels / 可识别的播放清单
- 多链接批量
- 断点续传（.part 续传、已完成跳过）
"""

from __future__ import annotations

import base64
import http.client
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess  # nosec B404
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
    QProgressBar,
    QPushButton,
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


from transfer_widgets import SafeEditTable as QTableWidget
from focus_widgets import FocusComboBox as QComboBox, FocusSpinBox as QSpinBox
from secure_storage import atomic_private_text, private_directory, user_data_directory


APP_SECTION = "YouTube / FB / TikTok / IG 视频"
IG_LOGIN_URL = "https://www.instagram.com/accounts/login/"
IG_HOME_URL = "https://www.instagram.com/"


def default_ig_cookie_path() -> str:
    return os.path.join(user_data_directory(), "ig_cookies.txt")


def ig_browser_profile_dir() -> str:
    return private_directory(os.path.join(user_data_directory(), "ig_browser_profile"))


def find_chromium_browser() -> str:
    """查找本机可用的 Chrome / Edge / Brave / Chromium 浏览器可执行文件。"""
    candidates: list[str] = []
    if os.name == "nt":
        env_dirs = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rel_paths = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
            r"Chromium\Application\chrome.exe",
        ]
        for base in env_dirs:
            if not base:
                continue
            for rel in rel_paths:
                candidates.append(os.path.join(base, rel))
        for cmd in ("chrome", "msedge", "brave"):
            found = shutil.which(cmd)
            if found:
                candidates.append(found)
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ])
    else:
        for cmd in ("google-chrome", "microsoft-edge", "chromium", "chromium-browser", "brave-browser"):
            found = shutil.which(cmd)
            if found:
                candidates.append(found)

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_get_local_json(port: int, path: str, timeout: float = 2.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read(512 * 1024)
        if resp.status != 200:
            return None
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ws_recv_exact(sock: socket.socket, buf: bytearray, n: int) -> bytes:
    while len(buf) < n:
        chunk = sock.recv(max(4096, n - len(buf)))
        if not chunk:
            raise ConnectionError("WebSocket connection closed")
        buf.extend(chunk)
    out = bytes(buf[:n])
    del buf[:n]
    return out


def _cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 3.0) -> dict:
    parsed = urlparse(ws_url)
    if (parsed.hostname or "") != "127.0.0.1" or not parsed.port:
        raise ValueError("Invalid local CDP WebSocket URL")
    port = int(parsed.port)
    ws_path = parsed.path or "/"
    if parsed.query:
        ws_path = f"{ws_path}?{parsed.query}"

    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {ws_path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode("ascii"))

        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket handshake failed")
            buf.extend(chunk)
            if len(buf) > 65536:
                raise ValueError("CDP WebSocket header too large")
        header_end = buf.index(b"\r\n\r\n") + 4
        status_line = bytes(buf[:header_end]).split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            raise ConnectionError("CDP WebSocket upgrade rejected")
        del buf[:header_end]

        payload = json.dumps({"id": 1, "method": method, "params": params or {}}).encode("utf-8")
        mask_key = os.urandom(4)
        frame = bytearray([0x81])
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, "big"))
        frame.extend(mask_key)
        frame.extend(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(frame))

        for _ in range(32):
            b0, b1 = _ws_recv_exact(sock, buf, 2)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            plen = b1 & 0x7F
            if plen == 126:
                plen = int.from_bytes(_ws_recv_exact(sock, buf, 2), "big")
            elif plen == 127:
                plen = int.from_bytes(_ws_recv_exact(sock, buf, 8), "big")
            if plen > 16 * 1024 * 1024:
                raise ValueError("CDP WebSocket frame too large")
            mkey = _ws_recv_exact(sock, buf, 4) if masked else b""
            data = _ws_recv_exact(sock, buf, plen)
            if masked:
                data = bytes(b ^ mkey[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                break
            if opcode == 0x1:
                msg = json.loads(data.decode("utf-8", errors="replace"))
                if isinstance(msg, dict) and msg.get("id") == 1:
                    return msg.get("result") or {}
    return {}


def _cdp_get_cookies_and_pages(port: int) -> tuple[list[dict], list[str]]:
    pages_data = _http_get_local_json(port, "/json/list")
    page_urls: list[str] = []
    ws_candidates: list[str] = []
    if isinstance(pages_data, list):
        for entry in pages_data:
            if not isinstance(entry, dict):
                continue
            u = str(entry.get("url") or "")
            if u:
                page_urls.append(u)
            ws = str(entry.get("webSocketDebuggerUrl") or "")
            if ws and entry.get("type") == "page":
                ws_candidates.append(ws)

    ver_data = _http_get_local_json(port, "/json/version")
    if isinstance(ver_data, dict):
        bws = str(ver_data.get("webSocketDebuggerUrl") or "")
        if bws:
            ws_candidates.append(bws)

    for ws in ws_candidates:
        try:
            res = _cdp_call(ws, "Storage.getCookies")
            cookies = res.get("cookies")
            if isinstance(cookies, list):
                return cookies, page_urls
        except Exception:
            continue
    return [], page_urls


def cookies_to_netscape_text(cookies: list[dict]) -> str:
    """将 CDP / 浏览器 Cookie 字典列表序列化为 Netscape HTTP Cookie File 格式。"""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by DIYDownloader Instagram Login",
        "",
    ]
    default_expiry = int(time.time()) + 86400 * 90
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "").strip()
        name = re.sub(r"[\r\n\t]", "", str(c.get("name") or "").strip())
        value = re.sub(r"[\r\n\t]", "", str(c.get("value") or ""))
        if not domain or not name:
            continue
        host_check = domain.lstrip(".").lower()
        if not host_check == "instagram.com" and not host_check.endswith(".instagram.com"):
            continue
        if not domain.startswith("."):
            domain = "." + domain
        include_subdomains = "TRUE"
        path = re.sub(r"[\r\n\t]", "", str(c.get("path") or "/").strip()) or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        try:
            exp = int(float(c.get("expires") or 0))
        except (TypeError, ValueError):
            exp = 0
        if exp <= 0:
            exp = default_expiry
        lines.append(f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{exp}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def extract_ig_login_info(cookies: list[dict], page_urls: list[str] | None = None) -> tuple[bool, str]:
    """检查 Cookie 列表中是否已包含有效的 Instagram 登录态（sessionid + ds_user_id）。"""
    ig_cookies: dict[str, str] = {}
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "").lstrip(".").lower()
        if domain == "instagram.com" or domain.endswith(".instagram.com"):
            name = str(c.get("name") or "").strip()
            val = str(c.get("value") or "").strip().strip('"')
            if name and val:
                ig_cookies[name] = val

    session_id = ig_cookies.get("sessionid", "")
    user_id = ig_cookies.get("ds_user_id", "")
    if not session_id or len(session_id) < 6:
        return False, ""

    # 若仍在登录/验证挑战页，继续等待跳转完成
    if page_urls:
        for url in page_urls:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            path = (parsed.path or "").lower()
            if host_matches(host, "instagram.com"):
                if any(p in path for p in ("/accounts/login", "/challenge/", "/two_factor")):
                    return False, ""

    return True, user_id or "已授权"


def read_saved_ig_cookie_status(cookie_path: str = "") -> tuple[bool, str, str]:
    """验证本地保存的 Netscape Cookie 文件是否包含有效 Instagram sessionid。
    返回 (is_valid, user_id_or_label, cookie_path)。
    """
    path = cookie_path or default_ig_cookie_path()
    if not path or not os.path.isfile(path):
        return False, "", ""
    try:
        jar = http.cookiejar.MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=False)
    except Exception:
        return False, "", ""

    now = time.time()
    session_id = ""
    user_id = ""
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if domain != "instagram.com" and not domain.endswith(".instagram.com"):
            continue
        if cookie.expires and cookie.expires < now:
            continue
        val = (cookie.value or "").strip().strip('"')
        if cookie.name == "sessionid" and val:
            session_id = val
        elif cookie.name == "ds_user_id" and val:
            user_id = val

    if not session_id:
        return False, "", ""
    return True, (user_id or "已授权"), path


def clear_saved_ig_auth() -> None:
    """清除已保存的 Instagram 授权 Cookie 文件与独立浏览器登录缓存。"""
    cookie_path = default_ig_cookie_path()
    if os.path.lexists(cookie_path):
        try:
            os.remove(cookie_path)
        except OSError:
            pass
    profile_dir = os.path.join(user_data_directory(), "ig_browser_profile")
    if os.path.isdir(profile_dir):
        shutil.rmtree(profile_dir, ignore_errors=True)
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
    """在不超过 max_h（兼容横屏 height 与竖屏 width 短边分辨率）的前提下，选该档位内画质最好的流。"""
    return (
        f"bestvideo*[height<={max_h}]+bestaudio/"
        f"bestvideo*[width<={max_h}]+bestaudio/"
        f"bestvideo[height<={max_h}]+bestaudio/"
        f"bestvideo[width<={max_h}]+bestaudio/"
        f"best[height<={max_h}][vcodec!=none]/"
        f"best[width<={max_h}][vcodec!=none]/"
        f"bestvideo*+bestaudio/best"
    )




QUALITY_OPTIONS = {
    # 原画画质：不限高度，选全站最好的视频轨+音轨
    "最佳质量": "bestvideo*+bestaudio/bestvideo+bestaudio/best",
    "最高 4K (2160p)": _height_format(2160),
    "最高 2K (1440p)": _height_format(1440),
    "最高 1080p": _height_format(1080),
    "最高 720p": _height_format(720),
    "最高 480p": _height_format(480),
    # 仅音频：尽量高码率
    "仅音频": "bestaudio/best",
}

# 期望分辨率（用于下载前校验是否误选到低清）
QUALITY_TARGET_HEIGHT = {
    "最佳质量": 0,       # 0 = 不强制下限，但会校验是否明显低于源最高分辨率
    "最高 4K (2160p)": 2160,
    "最高 2K (1440p)": 1440,
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
    if host_matches(host, "tiktok.com"):
        return "TikTok"
    if host_matches(host, "instagram.com", "instagr.am"):
        return "Instagram"
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


def extract_url_media_id(url: str) -> str:
    """从 YouTube / Instagram / TikTok / Facebook 链接中提取唯一视频 ID 或 shortcode。"""
    raw = clean_raw_url(url)
    if not raw:
        return ""
    yt_id = extract_youtube_video_id(raw)
    if yt_id:
        return yt_id
    parsed = urlparse(raw)
    path = parsed.path or ""
    platform = detect_platform(raw)
    if platform == "Instagram":
        m = re.search(r"/(?:reels?|p|tv)/([A-Za-z0-9_-]{5,64})", path)
        if m:
            return m.group(1)
    elif platform == "TikTok":
        m = re.search(r"/video/(\d{8,32})", path)
        if m:
            return m.group(1)
    elif platform == "Facebook":
        m = re.search(r"/reels?/([0-9A-Za-z._-]{5,64})", path)
        if m:
            return m.group(1)
        qs = parse_qs(parsed.query)
        if qs.get("v") and qs["v"][0]:
            return str(qs["v"][0]).strip()
    return ""


GENERIC_VIDEO_TITLE_RE = re.compile(
    r"^(?:video(?:\s+by\s+.+)?|reel(?:\s+by\s+.+)?|post(?:\s+by\s+.+)?|"
    r"photo(?:\s+by\s+.+)?|audio(?:\s+by\s+.+)?|tiktok\s+video.*|"
    r"instagram\s+video.*|facebook\s+video.*|链接-\d+|视频-\d+)$",
    re.I,
)
BRACKET_MEDIA_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{4,64})\]")


def is_generic_video_title(title: str) -> bool:
    t = sanitize_filename(title).strip()
    if not t:
        return True
    return bool(GENERIC_VIDEO_TITLE_RE.fullmatch(t))


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

    if platform in ("TikTok", "Instagram"):
        return raw, "清单/视频" if mode == MODE_PLAYLIST else "视频", mode == MODE_PLAYLIST

    # 其它站点不进入 generic extractor。
    return "", "不支持的网站", False


def extract_urls(text: str) -> list[str]:
    """提取原始链接（保留 list 等参数，后续按模式处理）。"""
    seen = set()
    urls = []
    for match in URL_RE.findall(str(text or "")):
        url = clean_raw_url(match)
        if not url.startswith("http"):
            continue
        platform_name = detect_platform(url)
        # 安全边界：本板块只接受明确支持的四个平台域名，
        # 禁止把任意 URL 交给 yt-dlp 的 generic extractor。
        if platform_name not in ("YouTube", "Facebook", "TikTok", "Instagram"):
            continue
        if platform_name == "Facebook":
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
        # 不读取用户目录/当前目录中的 yt-dlp.conf，避免外部配置注入 --exec、
        # 外部 downloader 等改变应用安全边界。
        "ignoreconfig": True,
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
            "vbr",
            "tbr",
            "fps",
            "hdr:12",
            "codec:av01:vp9.2:vp9:h265:h264",
            "abr",
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


def _stream_nominal_res(f: dict) -> int:
    """单条流的名义短边分辨率（兼容 16:9 横屏与 TikTok/IG 9:16 竖屏）。"""
    if not isinstance(f, dict):
        return 0
    try:
        h = int(f.get("height") or 0)
    except (TypeError, ValueError):
        h = 0
    try:
        w = int(f.get("width") or 0)
    except (TypeError, ValueError):
        w = 0
    if h > 0 and w > 0:
        return min(h, w)
    return max(h, w, 0)


def selected_video_height(info: dict) -> int:
    if not info:
        return 0
    res_list = []
    for f in info.get("requested_formats") or [info]:
        vcodec = (f.get("vcodec") or "none").lower()
        r = _stream_nominal_res(f)
        if r > 0 and vcodec != "none":
            res_list.append(r)
    return max(res_list) if res_list else 0




def available_max_video_height(info: dict) -> int:
    """源视频提供的最高名义短边分辨率（用于判断是否被错误选到低清）。"""
    if not info:
        return 0
    res_list = []
    for f in info.get("formats") or []:
        vcodec = (f.get("vcodec") or "none").lower()
        if vcodec in ("none", ""):
            continue
        r = _stream_nominal_res(f)
        if r > 0:
            res_list.append(r)
    return max(res_list) if res_list else 0




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
    注意：Instagram / TikTok 同一博主的多个视频往往共用标题（如 "Video by xxx"），
    因此只要已知 video_id，必须严格按 [video_id] 精确匹配，绝不能退化为按标题前缀模糊匹配。
    """
    if not os.path.isdir(target_dir):
        return "", False

    vid = str(video_id or "").strip()
    safe_title = sanitize_filename(title)
    allow_title_fallback = (not vid) and bool(safe_title) and (not is_generic_video_title(safe_title))
    complete = []
    partial = []

    for name in os.listdir(target_dir):
        path = os.path.join(target_dir, name)
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            continue
        matched = False
        if vid:
            if f"[{vid}]" in name:
                matched = True
        elif allow_title_fallback:
            # 仅当无 video_id 且本地文件名自身不带其它 [id] 标记、且标题非通用模板时才按标题匹配
            if not BRACKET_MEDIA_ID_RE.search(name):
                stem = os.path.splitext(name[:-5] if name.endswith(".part") else name)[0]
                if stem == safe_title:
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
    cookie_file: str = "",
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
            cookiefile=cookie_file or None,
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
            video_id = str(entry.get("id") or extract_url_media_id(entry_url) or "")
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

    def __init__(self, urls: list[str], mode: str, playlist_limit: int, cookie_file: str = ""):
        super().__init__()
        self.urls = urls
        self.mode = mode
        self.cookie_file = cookie_file
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
                cookie_file=self.cookie_file,
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
        output_format: str = "mp4",
        cookie_file: str = "",
    ):
        super().__init__()
        self.source_urls = list(source_urls)
        self.items: list[VideoItem] = []
        self.output_dir = output_dir
        self.output_format = output_format.lower()
        if self.output_format not in ("mp4", "mkv"):
            raise ValueError("Unsupported video output format")
        self.quality_key = quality_key
        self.skip_existing = skip_existing
        self.split_by_platform = split_by_platform
        self.playlist_subfolder = playlist_subfolder
        self.resume = resume
        self.mode = mode
        self.cookie_file = cookie_file
        self.playlist_limit = max(0, int(playlist_limit or 0))
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def _target_dir(self, item: VideoItem) -> str:
        parts = [self.output_dir]
        if self.split_by_platform and item.platform in ("YouTube", "Facebook", "TikTok", "Instagram"):
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
            cookiefile=self.cookie_file or None,
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
            # 仅改变封装容器，不改变原有画质选择或转码视频。
            opts["merge_output_format"] = self.output_format
            if self.quality_key != "仅音频":
                opts["postprocessors"] = [{
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": self.output_format,
                }]
            self.log.emit(f"使用 ffmpeg：{ffmpeg}（输出容器 {self.output_format}，保留原画编码）")
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
            cands.append(f"bestvideo*[height<={target}]+bestaudio/bestvideo*[width<={target}]+bestaudio")
            cands.append(f"bestvideo[height<={target}]+bestaudio/bestvideo[width<={target}]+bestaudio")
            cands.append(
                f"bestvideo*[height<={target}][vcodec^=avc1]+bestaudio[ext=m4a]/"
                f"bestvideo*[width<={target}][vcodec^=avc1]+bestaudio[ext=m4a]/"
                f"bestvideo*[height<={target}]+bestaudio/"
                f"bestvideo*[width<={target}]+bestaudio"
            )
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
                cookie_file=self.cookie_file,
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

                item.video_id = str(item.video_id or extract_url_media_id(item.url) or "")
                path, is_complete = find_existing_or_partial(
                    target_dir, item.title, item.video_id
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
                                extracted_id = str(info.get("id") or "").strip()
                                if extracted_id and extracted_id != item.video_id:
                                    item.video_id = extracted_id
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


class InstagramLoginWorker(QThread):
    """打开独立浏览器引导用户登录 Instagram，并在登录完成后自动提取保存授权 Cookie。"""

    log = Signal(str)
    finished_ok = Signal(str, str)  # (cookie_file_path, user_id)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.stop_event = threading.Event()
        self._proc: subprocess.Popen | None = None

    def stop(self):
        self.stop_event.set()
        self._terminate_browser()

    def _terminate_browser(self):
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def _try_browser_cookie_fallback(self) -> tuple[bool, str, str]:
        """当本机未找到独立 Chromium 可执行文件时，尝试从系统浏览器读取已登录的 IG Cookie。"""
        if yt_dlp is None:
            return False, "", ""
        extract_fn = getattr(getattr(yt_dlp, "cookies", None), "extract_cookies_from_browser", None)
        if not callable(extract_fn):
            return False, "", ""

        for browser_name in ("chrome", "edge", "brave", "firefox", "safari"):
            try:
                jar = extract_fn(browser_name)
            except Exception:
                continue
            cookies = []
            for c in jar:
                domain = (c.domain or "").lstrip(".").lower()
                if domain == "instagram.com" or domain.endswith(".instagram.com"):
                    cookies.append({
                        "domain": c.domain,
                        "name": c.name,
                        "value": c.value,
                        "path": c.path or "/",
                        "secure": bool(c.secure),
                        "expires": c.expires or 0,
                    })
            ok, user_id = extract_ig_login_info(cookies)
            if ok:
                cookie_path = default_ig_cookie_path()
                atomic_private_text(cookie_path, cookies_to_netscape_text(cookies))
                return True, user_id, cookie_path
        return False, "", ""

    def run(self):
        browser_exe = find_chromium_browser()
        if not browser_exe:
            self.log.emit("未检测到 Chrome / Edge / Brave，正在尝试读取系统浏览器或打开默认浏览器…")
            ok, user_id, cookie_path = self._try_browser_cookie_fallback()
            if ok:
                self.finished_ok.emit(cookie_path, user_id)
                return
            self.failed.emit(
                "未检测到本机 Chrome 或 Microsoft Edge 浏览器，无法自动完成浏览器授权抓取。\n"
                "请安装 Chrome 或 Edge 浏览器后重试。"
            )
            return

        try:
            port = _allocate_local_port()
            profile_dir = ig_browser_profile_dir()
            browser_name = os.path.basename(browser_exe)
            self.log.emit(f"正在启动浏览器（{browser_name}）打开 Instagram 登录页面…")

            cmd = [
                browser_exe,
                f"--user-data-dir={profile_dir}",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=http://127.0.0.1",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                IG_LOGIN_URL,
            ]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self._proc = subprocess.Popen(  # nosec B603
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )

            self.log.emit("请在弹出的浏览器窗口中登录您的 Instagram 账号（支持两步验证），登录成功后将自动完成授权…")
            deadline = time.time() + 300  # 最多等待 5 分钟
            detected_at = 0.0
            last_cookies: list[dict] = []
            last_user_id = ""

            while time.time() < deadline and not self.stop_event.is_set():
                if self._proc.poll() is not None:
                    # 用户关闭了浏览器窗口
                    break

                cookies, page_urls = _cdp_get_cookies_and_pages(port)
                if cookies:
                    ok, user_id = extract_ig_login_info(cookies, page_urls)
                    if ok:
                        last_cookies = cookies
                        last_user_id = user_id
                        if not detected_at:
                            detected_at = time.time()
                            self.log.emit(f"已检测到 Instagram 登录成功（账号 ID: {user_id}），正在同步授权凭据…")
                        elif time.time() - detected_at >= 1.5:
                            cookie_path = default_ig_cookie_path()
                            atomic_private_text(cookie_path, cookies_to_netscape_text(last_cookies))
                            self._terminate_browser()
                            self.finished_ok.emit(cookie_path, last_user_id)
                            return
                    else:
                        detected_at = 0.0

                time.sleep(1.0)

            if self.stop_event.is_set():
                self._terminate_browser()
                self.failed.emit("已取消 Instagram 浏览器登录。")
                return

            if last_cookies and last_user_id:
                cookie_path = default_ig_cookie_path()
                atomic_private_text(cookie_path, cookies_to_netscape_text(last_cookies))
                self._terminate_browser()
                self.finished_ok.emit(cookie_path, last_user_id)
                return

            self._terminate_browser()
            self.failed.emit("浏览器已关闭或登录超时，未检测到有效的 Instagram 账号登录状态。")
        except Exception as exc:
            self._terminate_browser()
            self.failed.emit(f"Instagram 浏览器登录失败：{exc}")


class VideoBatchPage(QWidget):
    """独立的 YouTube / FB / TikTok / IG 视频批量下载板块（紧凑左右结构）。"""

    _ICON_BTN_STYLE = (
        "QPushButton { min-width: 28px; max-width: 32px; min-height: 24px; max-height: 26px; "
        "padding: 2px 4px; border-radius: 7px; font-size: 13px; }"
    )
    _COMPACT_BTN_STYLE = (
        "QPushButton { min-height: 24px; max-height: 26px; padding: 2px 10px; "
        "border-radius: 7px; font-size: 12px; font-weight: 800; }"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.env_worker = None
        self.ig_login_worker = None
        self._ig_cookie_path = ""
        self.preview_items: list[VideoItem] = []
        self._stats = {"success": 0, "skipped": 0, "failed": 0, "resumed": 0}
        self.build_ui()
        self.connect_signals()
        self.refresh_env_status(silent=True)
        self.refresh_ig_auth_status(silent=True)

    @staticmethod
    def _make_icon_btn(icon_text: str, tooltip: str, object_name: str = "secondaryButton", checkable: bool = False) -> QPushButton:
        btn = QPushButton(icon_text)
        btn.setObjectName(object_name)
        btn.setToolTip(tooltip)
        btn.setCheckable(checkable)
        btn.setStyleSheet(VideoBatchPage._ICON_BTN_STYLE)
        return btn

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ========== 顶部极简一行控制条 ==========
        top_bar = QFrame()
        top_bar.setObjectName("compactPanel")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 5, 10, 5)
        top_layout.setSpacing(6)
        root.addWidget(top_bar)

        self.env_status_label = QLabel("检测环境中…")
        self.env_status_label.setObjectName("subtitle")
        self.env_status_label.setToolTip("高清视频音视频合并环境状态（yt-dlp + ffmpeg + ffprobe）")
        top_layout.addWidget(self.env_status_label)

        self.env_check_btn = self._make_icon_btn("🔄", "重新检测运行环境（yt-dlp / ffmpeg / ffprobe）")
        self.env_install_btn = self._make_icon_btn("⬇", "一键自动下载并安装缺失的 ffmpeg / ffprobe 组件", "primaryButton")
        top_layout.addWidget(self.env_check_btn)
        top_layout.addWidget(self.env_install_btn)

        # 画质选择（默认最佳质量，也可手动选择档位）
        q_label = QLabel("画质:")
        q_label.setObjectName("fieldLabel")
        top_layout.addWidget(q_label)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_OPTIONS.keys()))
        self.quality_combo.setCurrentText("最佳质量")
        self.quality_combo.setMaximumHeight(26)
        self.quality_combo.setToolTip(
            "默认「最佳质量」（按源站最高分辨率与最高码率下载，支持 4K/2K/1080p）。\n"
            "也可手动选择指定分辨率上限或「仅音频」。"
        )
        top_layout.addWidget(self.quality_combo)

        # 格式选择
        f_label = QLabel("格式:")
        f_label.setObjectName("fieldLabel")
        top_layout.addWidget(f_label)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["MP4", "MKV"])
        self.format_combo.setCurrentText("MP4")
        self.format_combo.setMaximumHeight(26)
        self.format_combo.setToolTip("视频封装容器格式（默认 MP4，无损重封装不损失画质）")
        top_layout.addWidget(self.format_combo)
        self.quality_combo.currentTextChanged.connect(
            lambda quality: self.format_combo.setEnabled(quality != "仅音频")
        )

        # IG 授权紧凑内嵌区
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setReadOnly(True)
        self.cookie_edit.setMaximumHeight(25)
        self.cookie_edit.setMinimumWidth(145)
        self.cookie_edit.setMaximumWidth(210)
        self.cookie_edit.setPlaceholderText("● IG 未登录")
        self.cookie_edit.setToolTip("Instagram 账号登录授权状态；点击右侧 🔑 按钮自动打开浏览器登录授权")
        top_layout.addWidget(self.cookie_edit)

        self.ig_login_btn = self._make_icon_btn("🔑", "登录 IG 账号（点击自动打开浏览器登录并完成授权）", "primaryButton")
        self.clear_cookie_btn = self._make_icon_btn("🚪", "退出并清除已保存的 Instagram 登录授权")
        top_layout.addWidget(self.ig_login_btn)
        top_layout.addWidget(self.clear_cookie_btn)

        top_layout.addStretch(1)

        # 右上角：更多设置抽屉开关 + 总日志查错开关
        self.more_settings_btn = self._make_icon_btn("⚙", "展开 / 收起下载目录与高级设置", checkable=True)
        self.toggle_log_btn = self._make_icon_btn("📋", "显示 / 隐藏总日志（用于排查下载错误）", checkable=True)
        top_layout.addWidget(self.more_settings_btn)
        top_layout.addWidget(self.toggle_log_btn)

        # ========== 可折叠更多设置抽屉（默认隐藏，不占主界面高度）==========
        self.more_settings_frame = QFrame()
        self.more_settings_frame.setObjectName("compactPanel")
        self.more_settings_frame.setVisible(False)
        ms_layout = QHBoxLayout(self.more_settings_frame)
        ms_layout.setContentsMargins(10, 6, 10, 6)
        ms_layout.setSpacing(8)
        root.addWidget(self.more_settings_frame)

        dir_label = QLabel("目录:")
        dir_label.setObjectName("fieldLabel")
        ms_layout.addWidget(dir_label)
        self.output_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "Downloads", "视频批量下载"))
        self.output_edit.setMaximumHeight(25)
        self.output_edit.setToolTip("视频保存的本地根目录")
        ms_layout.addWidget(self.output_edit, 1)
        choose_btn = self._make_icon_btn("📁", "选择视频保存目录")
        choose_btn.clicked.connect(self.choose_output_dir)
        ms_layout.addWidget(choose_btn)

        mode_label = QLabel("模式:")
        mode_label.setObjectName("fieldLabel")
        ms_layout.addWidget(mode_label)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(DOWNLOAD_MODES)
        self.mode_combo.setCurrentText(MODE_AUTO)
        self.mode_combo.setMaximumHeight(25)
        self.mode_combo.setToolTip(
            "自动识别：真实播放列表整表下载，电台混播只下当前视频。\n"
            "仅单视频：忽略列表参数只下当前视频。\n"
            "展开播放列表/清单：强制展开列表。"
        )
        ms_layout.addWidget(self.mode_combo)

        lim_label = QLabel("上限:")
        lim_label.setObjectName("fieldLabel")
        ms_layout.addWidget(lim_label)
        self.playlist_limit_spin = QSpinBox()
        self.playlist_limit_spin.setRange(0, 5000)
        self.playlist_limit_spin.setValue(0)
        self.playlist_limit_spin.setSpecialValueText("不限")
        self.playlist_limit_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.playlist_limit_spin.setMaximumWidth(64)
        self.playlist_limit_spin.setMaximumHeight(25)
        self.playlist_limit_spin.setToolTip("播放列表最大下载数量（0 表示不限制）")
        ms_layout.addWidget(self.playlist_limit_spin)

        self.resume_check = QCheckBox("续传")
        self.resume_check.setChecked(True)
        self.resume_check.setToolTip("断点续传：保留 .part 临时文件，中断后再次开始可继续下载")
        self.skip_existing_check = QCheckBox("跳过已有")
        self.skip_existing_check.setChecked(True)
        self.skip_existing_check.setToolTip("本地已存在同名/同 ID 完整视频时自动跳过")
        self.split_platform_check = QCheckBox("按平台分夹")
        self.split_platform_check.setChecked(True)
        self.split_platform_check.setToolTip("按 YouTube / Facebook / TikTok / Instagram 自动建立子文件夹")
        self.playlist_folder_check = QCheckBox("列表分夹")
        self.playlist_folder_check.setChecked(True)
        self.playlist_folder_check.setToolTip("播放列表自动按列表标题建立独立子文件夹")

        for cb in (self.resume_check, self.skip_existing_check, self.split_platform_check, self.playlist_folder_check):
            ms_layout.addWidget(cb)

        # ========== 主体左右大空间布局（左：粘贴链接主操作区 | 右：实时执行进度中心）==========
        body = QHBoxLayout()
        body.setSpacing(8)
        root.addLayout(body, 1)

        # ----- 左栏：粘贴视频链接（主操作区，醒目高亮边框）-----
        left_frame = QFrame()
        left_frame.setObjectName("card")
        left_frame.setStyleSheet(
            "QFrame#card { border: 1.5px solid #2f6df6; border-radius: 12px; }"
        )
        left_layout = QVBoxLayout(left_frame)
        left_layout.setContentsMargins(10, 8, 10, 8)
        left_layout.setSpacing(6)
        body.addWidget(left_frame, 5)

        left_header = QHBoxLayout()
        left_header.setSpacing(4)
        left_title = QLabel("粘贴视频 / 播放列表链接")
        left_title.setObjectName("cardTitle")
        left_title.setStyleSheet("font-size: 14px; font-weight: 900;")
        left_header.addWidget(left_title)
        left_header.addStretch(1)

        self.clipboard_btn = self._make_icon_btn("📋", "从剪贴板读取并追加链接")
        self.clear_links_btn = self._make_icon_btn("🗑", "清空输入框中的所有链接", "ghostButton")
        self.preview_btn = self._make_icon_btn("🔍", "解析预览链接列表（仅查看，不立即下载）")
        self.open_folder_btn = self._make_icon_btn("📂", "打开本地视频下载目录")
        for b in (self.clipboard_btn, self.clear_links_btn, self.preview_btn, self.open_folder_btn):
            left_header.addWidget(b)
        left_layout.addLayout(left_header)

        self.links_edit = QTextEdit()
        self.links_edit.setObjectName("pasteTextBox")
        self.links_edit.setPlaceholderText(
            "每行粘贴一个链接（支持 YouTube / Facebook / TikTok / Instagram 批量混贴）：\n\n"
            "• https://www.instagram.com/reel/xxxxxxxx/\n"
            "• https://www.tiktok.com/@user/video/xxxxxxxx\n"
            "• https://www.youtube.com/watch?v=xxxxxxxx\n"
            "• https://www.youtube.com/playlist?list=PLxxxxxxxx\n"
            "• https://www.facebook.com/reel/xxxxxxxx\n\n"
            "粘贴后直接点击下方「▶ 开始下载」，默认按最高质量自动解析并下载。"
        )
        left_layout.addWidget(self.links_edit, 1)

        self.tiktok_vpn_warning = QLabel("提示：下载 TikTok 需开启 VPN 连接。")
        self.tiktok_vpn_warning.setWordWrap(True)
        self.tiktok_vpn_warning.setStyleSheet("color: #ef4444; font-size: 11px; font-weight: bold;")
        self.tiktok_vpn_warning.hide()
        left_layout.addWidget(self.tiktok_vpn_warning)
        self.links_edit.textChanged.connect(self.update_tiktok_warning)

        left_bottom = QHBoxLayout()
        left_bottom.setSpacing(6)
        self.start_btn = QPushButton("▶ 开始下载")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.setToolTip("按所选画质（默认最佳质量）自动加载列表并开始批量下载")
        self.start_btn.setStyleSheet(
            "QPushButton { min-height: 28px; max-height: 30px; padding: 3px 14px; "
            "border-radius: 8px; font-size: 13px; font-weight: 900; }"
        )
        self.stop_btn = self._make_icon_btn("⏹", "停止当前下载任务（已下载部分会保留以便断点续传）", "dangerButton")
        self.stop_btn.setFixedSize(34, 30)
        self.stop_btn.setEnabled(False)
        left_bottom.addWidget(self.start_btn, 1)
        left_bottom.addWidget(self.stop_btn)
        left_layout.addLayout(left_bottom)

        # ----- 右栏：实时执行进度中心 + 紧凑队列表 + 默认隐藏的总日志 -----
        right_frame = QFrame()
        right_frame.setObjectName("card")
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(10, 8, 10, 8)
        right_layout.setSpacing(6)
        body.addWidget(right_frame, 7)

        # 紧凑实时总进度看板
        prog_panel = QFrame()
        prog_panel.setObjectName("compactPanel")
        prog_layout = QVBoxLayout(prog_panel)
        prog_layout.setContentsMargins(10, 6, 10, 6)
        prog_layout.setSpacing(4)
        right_layout.addWidget(prog_panel)

        prog_top = QHBoxLayout()
        prog_top.setSpacing(6)
        self.status_row = QLabel("等待开始 · 粘贴链接后点左侧「▶ 开始下载」")
        self.status_row.setObjectName("status")
        self.status_row.setStyleSheet("font-size: 12px; font-weight: 800; color: #38bdf8;")
        prog_top.addWidget(self.status_row, 1)

        self.live_speed_label = QLabel("")
        self.live_speed_label.setStyleSheet("font-size: 11px; color: #22c55e; font-weight: bold;")
        prog_top.addWidget(self.live_speed_label)

        self.live_percent_label = QLabel("0%")
        self.live_percent_label.setStyleSheet("font-size: 14px; font-weight: 900;")
        prog_top.addWidget(self.live_percent_label)
        prog_layout.addLayout(prog_top)

        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setRange(0, 100)
        self.overall_progress_bar.setValue(0)
        self.overall_progress_bar.setTextVisible(False)
        self.overall_progress_bar.setFixedHeight(7)
        prog_layout.addWidget(self.overall_progress_bar)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self.stat_total_label = QLabel("总计: 0")
        self.stat_done_label = QLabel("已完成: 0")
        self.stat_done_label.setStyleSheet("color: #22c55e; font-weight: bold; font-size: 11px;")
        self.stat_skip_label = QLabel("跳过/续传: 0")
        self.stat_skip_label.setStyleSheet("color: #eab308; font-weight: bold; font-size: 11px;")
        self.stat_fail_label = QLabel("失败: 0")
        self.stat_fail_label.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 11px;")
        self.stat_total_label.setStyleSheet("font-weight: bold; font-size: 11px;")
        for sl in (self.stat_total_label, self.stat_done_label, self.stat_skip_label, self.stat_fail_label):
            stats_row.addWidget(sl)
        stats_row.addStretch(1)
        prog_layout.addLayout(stats_row)

        # 紧凑实时进度列表
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "平台", "来源", "标题", "时长", "实时状态 / 进度", "链接"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setFixedHeight(24)
        self.table.setColumnWidth(0, 34)
        self.table.setColumnWidth(1, 68)
        self.table.setColumnWidth(2, 86)
        self.table.setColumnWidth(3, 210)
        self.table.setColumnWidth(4, 56)
        self.table.setColumnWidth(5, 165)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("QTableWidget { font-size: 11px; } QHeaderView::section { padding: 2px 4px; font-size: 11px; }")
        right_layout.addWidget(self.table, 1)

        # 默认隐藏的总日志抽屉（仅在点击右上角 📋 按钮时展开查错）
        self.log_drawer = QFrame()
        self.log_drawer.setObjectName("compactPanel")
        self.log_drawer.setVisible(False)
        log_layout = QVBoxLayout(self.log_drawer)
        log_layout.setContentsMargins(8, 6, 8, 6)
        log_layout.setSpacing(4)
        log_header = QHBoxLayout()
        log_title = QLabel("📋 总日志（查错视图）")
        log_title.setObjectName("fieldLabel")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        clear_log_btn = self._make_icon_btn("🧹", "清空总日志", "ghostButton")
        hide_log_btn = self._make_icon_btn("✕", "隐藏总日志", "ghostButton")
        log_header.addWidget(clear_log_btn)
        log_header.addWidget(hide_log_btn)
        log_layout.addLayout(log_header)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(130)
        log_layout.addWidget(self.log_box)
        right_layout.addWidget(self.log_drawer)

        clear_log_btn.clicked.connect(self.log_box.clear)
        hide_log_btn.clicked.connect(lambda: self.toggle_log_btn.setChecked(False))

        env_tip = "yt-dlp 已就绪" if yt_dlp is not None else "未安装 yt-dlp，请先 pip install yt-dlp"
        if yt_dlp is not None:
            ff = resolve_ffmpeg_path()
            if ff:
                env_tip += " · ffmpeg 已就绪（高清合并）"
            else:
                env_tip += " · 未检测到 ffmpeg：高清视频合并需要安装 ffmpeg"
        env_tip += " · 默认最高质量下载"
        self.log(env_tip)

    def _wrap_button(self, button: QPushButton) -> QFrame:
        field = QFrame()
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(button)
        return field

    def _add_field(self, grid, label, widget, row, col, row_span=1, col_span=1):
        field = QFrame()
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel(label))
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
        self.ig_login_btn.clicked.connect(self.start_ig_browser_login)
        self.clear_cookie_btn.clicked.connect(self.clear_ig_auth)
        self.more_settings_btn.toggled.connect(self.more_settings_frame.setVisible)
        self.toggle_log_btn.toggled.connect(self.log_drawer.setVisible)

    def _update_summary_ui(self, current_pct: float | None = None, speed_eta: str = ""):
        total = len(self.preview_items)
        done = self._stats.get("success", 0)
        skipped = self._stats.get("skipped", 0)
        resumed = self._stats.get("resumed", 0)
        failed = self._stats.get("failed", 0)
        self.stat_total_label.setText(f"总计: {total}")
        self.stat_done_label.setText(f"已完成: {done}")
        self.stat_skip_label.setText(f"跳过/续传: {skipped}/{resumed}")
        self.stat_fail_label.setText(f"失败: {failed}")
        if speed_eta is not None:
            self.live_speed_label.setText(speed_eta)

        if total > 0:
            completed_items = done + skipped + failed
            item_fraction = (current_pct or 0.0) / 100.0 if current_pct is not None else 0.0
            overall = int(min(100.0, ((completed_items + item_fraction) * 100.0) / max(total, 1)))
        else:
            overall = int(current_pct or 0)
        self.overall_progress_bar.setValue(overall)
        self.live_percent_label.setText(f"{overall}%")

    def refresh_env_status(self, silent: bool = True):
        report = scan_environment()
        missing = report.missing_required
        self.env_install_btn.setEnabled(bool(missing) or not report.ready_for_hd)
        self.env_install_btn.setVisible(bool(missing) or not report.ready_for_hd)
        if report.ready_for_hd:
            self.env_status_label.setText("✓ 高清环境就绪")
            self.env_status_label.setToolTip(report.summary_line())
            self.env_status_label.setStyleSheet("color: #22c55e; font-weight: 800; font-size: 12px;")
        else:
            names = "、".join(c.name for c in missing) or "组件"
            self.env_status_label.setText(f"⚠ 缺 {names}")
            self.env_status_label.setToolTip(report.summary_line() + "\n点击右侧 ⬇ 按钮一键安装")
            self.env_status_label.setStyleSheet("color: #eab308; font-weight: 800; font-size: 12px;")
        if not silent:
            self.log(report.summary_line())
            for c in report.components:
                mark = "OK" if c.ok else "缺"
                detail = c.detail or c.path or ""
                self.log(f"[{mark}] {c.name}: {detail}")
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
        self.env_status_label.setText("正在安装组件…")
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
            self.env_status_label.setText(f"安装组件 {pct}%")
            self.status_row.setText(f"正在下载环境组件 {pct}%")
            self.overall_progress_bar.setValue(pct)
            self.live_percent_label.setText(f"{pct}%")

    def on_env_install_done(self, report):
        self.refresh_env_status(silent=False)
        if report.ready_for_hd:
            QMessageBox.information(
                self,
                APP_SECTION,
                "组件安装完成，高清下载环境已就绪。\n\n"
                f"{report.summary_line()}\n\n"
                "请重新开始下载。",
            )
            self.log("组件安装完成，可以开始高清下载。")
        else:
            missing = "、".join(c.name for c in report.missing_required) or "未知"
            QMessageBox.warning(
                self,
                APP_SECTION,
                f"安装流程结束，但仍缺少：{missing}\n\n请点右上角 📋 查看总日志或手动安装 ffmpeg。",
            )

    def on_env_install_failed(self, message: str):
        self.log(f"组件安装失败：{message}")
        self.env_status_label.setText("⚠ 安装失败")
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

    def active_cookie_file(self) -> str:
        if self._ig_cookie_path and os.path.isfile(self._ig_cookie_path):
            return self._ig_cookie_path
        ok, _uid, path = read_saved_ig_cookie_status()
        if ok and path:
            self._ig_cookie_path = path
            return path
        raw = self.cookie_edit.text().strip()
        if raw and os.path.isfile(raw):
            return raw
        return ""

    def refresh_ig_auth_status(self, silent: bool = True) -> bool:
        ok, user_id, path = read_saved_ig_cookie_status()
        if ok and path:
            self._ig_cookie_path = path
            self.cookie_edit.setText(f"● IG已授权 ({user_id})")
            self.cookie_edit.setToolTip(f"已登录 Instagram 账号 (ID: {user_id})\n点击右侧 🔑 可重新登录，点击 🚪 可清除授权")
            self.cookie_edit.setStyleSheet("color: #22c55e; font-weight: bold; font-size: 11px;")
            if hasattr(self, "ig_login_btn"):
                self.ig_login_btn.setText("🔑")
                self.ig_login_btn.setToolTip("重新打开浏览器登录 / 切换 Instagram 账号")
            if not silent:
                self.log(f"Instagram 账号授权已就绪（ID: {user_id}）。")
            return True
        self._ig_cookie_path = ""
        self.cookie_edit.clear()
        self.cookie_edit.setToolTip("尚未登录 Instagram 账号；点击右侧 🔑 按钮自动打开浏览器登录授权")
        self.cookie_edit.setStyleSheet("font-size: 11px;")
        if hasattr(self, "ig_login_btn"):
            self.ig_login_btn.setText("🔑")
            self.ig_login_btn.setToolTip("点击自动打开浏览器登录 Instagram 账号并完成授权")
        return False

    def start_ig_browser_login(self):
        if self.ig_login_worker and self.ig_login_worker.isRunning():
            self.ig_login_worker.stop()
            self.log("正在取消 Instagram 浏览器登录…")
            return
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "请先等待当前下载任务结束，再登录 IG 账号。")
            return

        self.ig_login_btn.setText("⏳")
        self.ig_login_btn.setToolTip("正在等待浏览器登录完成（再次点击可取消等待）")
        self.cookie_edit.setText("⏳ 等待浏览器登录…")
        self.cookie_edit.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 11px;")
        self.status_row.setText("请在弹出的浏览器中登录 Instagram 账号，完成后将自动授权…")

        worker = InstagramLoginWorker()
        worker.log.connect(self.log)
        worker.finished_ok.connect(self.on_ig_login_success)
        worker.failed.connect(self.on_ig_login_failed)
        worker.finished.connect(self.on_ig_login_finished)
        self.ig_login_worker = worker
        worker.start()

    def on_ig_login_success(self, cookie_path: str, user_id: str):
        self._ig_cookie_path = cookie_path
        self.refresh_ig_auth_status(silent=True)
        self.status_row.setText(f"Instagram 账号授权成功（ID: {user_id}）")
        self.log(f"Instagram 浏览器登录授权成功（账号 ID: {user_id}），可直接开始下载 IG 视频。")
        QMessageBox.information(
            self,
            APP_SECTION,
            f"Instagram 账号登录授权成功！\n\n账号 ID：{user_id}\n授权状态已自动保存，现在可以直接下载 Instagram 视频。",
        )

    def on_ig_login_failed(self, message: str):
        self.refresh_ig_auth_status(silent=True)
        self.status_row.setText("Instagram 登录未完成")
        self.log(message)
        if "已取消" not in message:
            QMessageBox.warning(self, APP_SECTION, message)

    def on_ig_login_finished(self):
        if self.sender() is self.ig_login_worker:
            self.ig_login_worker = None
        self.refresh_ig_auth_status(silent=True)

    def clear_ig_auth(self):
        if self.ig_login_worker and self.ig_login_worker.isRunning():
            self.ig_login_worker.stop()
        clear_saved_ig_auth()
        self._ig_cookie_path = ""
        self.refresh_ig_auth_status(silent=True)
        self.status_row.setText("已清除 Instagram 账号登录授权")
        self.log("已退出并清除 Instagram 账号授权缓存。")

    def choose_cookie_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Netscape 格式 Cookie 文件", "", "Cookie 文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                header = handle.readline(256).strip()
            if header not in ("# HTTP Cookie File", "# Netscape HTTP Cookie File"):
                raise ValueError("请选择 Netscape 格式的 cookies.txt 文件。")
        except (OSError, UnicodeError, ValueError):
            QMessageBox.warning(self, APP_SECTION, "Cookie 文件无法读取或格式不正确，请选择 Netscape 格式的 cookies.txt。")
            return
        self._ig_cookie_path = path
        self.cookie_edit.setText(os.path.basename(path))

    def choose_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择视频下载目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def open_output_folder(self):
        path = self.output_edit.text().strip()
        if not path:
            QMessageBox.information(self, APP_SECTION, "请先展开 ⚙ 设置选择下载目录。")
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
        display_title = item.title
        if item.video_id and f"[{item.video_id}]" not in display_title and is_generic_video_title(display_title):
            display_title = f"{display_title} [{item.video_id}]"
        return [
            item.index,
            item.platform,
            item.source or "",
            display_title,
            item.duration,
            item.status,
            item.url,
        ]

    def update_tiktok_warning(self):
        self.tiktok_vpn_warning.setVisible(any(
            detect_platform(url) == "TikTok"
            for url in URL_RE.findall(self.links_edit.toPlainText())
        ))

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
            elif item.platform == "Instagram":
                cell.setForeground(QColor("#f472b6"))
            elif item.platform == "TikTok":
                cell.setForeground(QColor("#38bdf8"))
        if col == 5:
            status = str(item.status or "")
            if status.startswith("成功") or status == "待下载":
                cell.setForeground(QColor("#22c55e"))
            elif "下载中" in status or "处理中" in status:
                cell.setForeground(QColor("#38bdf8"))
            elif "失败" in status or "解析失败" in status:
                cell.setForeground(QColor("#ef4444"))
            elif "跳过" in status or "续传" in status or "停止" in status:
                cell.setForeground(QColor("#eab308"))

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
                self.table.scrollToItem(self.table.item(row, 0))
                return
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, value in enumerate(self._row_values(item)):
            cell = QTableWidgetItem("" if value is None else str(value))
            self._style_cell(cell, col, item)
            self.table.setItem(row, col, cell)
        self.table.scrollToBottom()

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
        self._stats = {"success": 0, "skipped": 0, "failed": 0, "resumed": 0}
        self.table.setRowCount(0)
        self.set_running_state(True)
        self.status_row.setText(f"正在解析 {len(urls)} 个链接（{mode}）...")
        self._update_summary_ui(current_pct=0.0, speed_eta="解析中…")
        self.log(f"开始解析 {len(urls)} 个链接，模式：{mode}" + (f"，列表上限 {limit}" if limit else ""))

        worker = VideoPreviewWorker(urls, mode, limit, cookie_file=self.active_cookie_file())
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.item_ready.connect(self.on_item_update)
        worker.done.connect(self.on_preview_done)
        worker.finished.connect(self.on_worker_finished)
        self.worker = worker
        worker.start()

    def on_preview_done(self, items: list[VideoItem]):
        self.preview_items = list(items)
        self.fill_table(self.preview_items)
        ok = sum(1 for i in items if i.status == "待下载")
        playlists = len({i.playlist_title for i in items if i.playlist_title})
        self.status_row.setText(f"预览完成：{ok}/{len(items)} 项可下载" + (f"（含 {playlists} 个列表）" if playlists else ""))
        self._update_summary_ui(current_pct=0.0, speed_eta="待开始")
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
            self.more_settings_btn.setChecked(True)
            QMessageBox.warning(self, APP_SECTION, "请先选择下载目录。")
            return

        urls = self.collect_urls()
        if not urls:
            QMessageBox.information(self, APP_SECTION, "请先在左侧粘贴视频或播放列表链接。")
            return

        env = self.refresh_env_status(silent=True)
        if not env.ready_for_hd:
            missing = "、".join(c.name for c in env.missing_required) or "ffmpeg"
            ask = QMessageBox.question(
                self,
                APP_SECTION,
                f"检测到缺少必需组件：{missing}\n\n"
                "没有 ffmpeg 时高清视频会合并失败或掉画质。\n\n"
                "是否现在一键安装组件？\n"
                "（选「No」将仍尝试下载，但不推荐）",
            )
            if ask == QMessageBox.StandardButton.Yes:
                self.install_env_components()
                return

        mode = self.current_mode()
        limit = self.playlist_limit_spin.value()
        quality = self.quality_combo.currentText()

        self.preview_items = []
        self._stats = {"success": 0, "skipped": 0, "failed": 0, "resumed": 0}
        self.table.setRowCount(0)
        self.set_running_state(True)
        self.status_row.setText(f"正在自动加载 {len(urls)} 个链接（画质：{quality}）...")
        self._update_summary_ui(current_pct=0.0, speed_eta="准备中…")
        self.log(f"开始：自动加载 {len(urls)} 个链接后下载，画质：{quality}，模式：{mode}")

        worker = VideoDownloadWorker(
            source_urls=urls,
            output_dir=output_dir,
            quality_key=quality,
            output_format=self.format_combo.currentText().lower(),
            cookie_file=self.active_cookie_file(),
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

        status = str(item.status or "")
        total = len(self.preview_items)
        if status.startswith("下载中"):
            pct_match = re.search(r"(\d+(?:\.\d+)?)%", status)
            pct = float(pct_match.group(1)) if pct_match else 0.0
            extra = status.replace("下载中", "").strip()
            if pct_match:
                extra = extra.replace(pct_match.group(0), "").strip()
            self.status_row.setText(f"正在下载 ({item.index}/{total})：{item.title[:36]}")
            self._update_summary_ui(current_pct=pct, speed_eta=extra)
        elif status == "处理中/合并":
            self.status_row.setText(f"正在无损合并高清音视频 ({item.index}/{total})：{item.title[:32]}")
            self._update_summary_ui(current_pct=99.0, speed_eta="FFmpeg 合并中…")
        else:
            self._update_summary_ui()

    def on_progress(self, stats: dict):
        self._stats = dict(stats)
        self._update_summary_ui()

    def on_download_done(self):
        done = self._stats.get("success", 0)
        skipped = self._stats.get("skipped", 0)
        failed = self._stats.get("failed", 0)
        self.status_row.setText(f"任务全部完成 · 成功 {done}，跳过 {skipped}，失败 {failed}")
        self.live_speed_label.setText("已完成")
        if len(self.preview_items) > 0 and failed == 0:
            self.overall_progress_bar.setValue(100)
            self.live_percent_label.setText("100%")

    def stop_task(self):
        if self.worker and hasattr(self.worker, "stop"):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.status_row.setText("正在停止（已下载分片将保留以便断点续传）...")
            self.log("已请求停止。未完成文件会保留，再次开始可断点续传。")
        else:
            self.set_running_state(False)

    def on_worker_finished(self):
        if self.sender() is self.worker:
            self.worker = None
        self.set_running_state(False)

    def show_error(self, message: str):
        self.log(message)
        self.status_row.setText("出现错误（可点右上角 📋 查看总日志）")
        QMessageBox.warning(self, APP_SECTION, message)

    def request_close(self) -> bool:
        if self.ig_login_worker and self.ig_login_worker.isRunning():
            self.ig_login_worker.stop()
            self.ig_login_worker.wait(2000)
        if not self.has_running_worker():
            return True
        if hasattr(self.worker, "stop"):
            self.stop_task()
        if not self.worker.wait(3000):
            return False
        return True


