"""运行环境检测与一键安装（ffmpeg 等高清合并必需组件）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse


USER_AGENT = "DIYDownloader-EnvSetup/1.1 (+https://github.com/christiancagfr-alt/DIY-download)"

# 只接受 GitHub Release API 返回且带 SHA-256 digest 的 FFmpeg 资产。
FFMPEG_RELEASE_APIS = [
    "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest",
    "https://api.github.com/repos/yt-dlp/FFmpeg-Builds/releases/latest",
]


def app_data_dir() -> str:
    if sys.platform.startswith("win"):
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(root, "DIYDownloader")
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~/Library/Application Support"), "DIYDownloader")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        path = os.path.join(root, "DIYDownloader")
    os.makedirs(path, mode=0o700, exist_ok=True)
    if not sys.platform.startswith("win"):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def bundled_bin_dir() -> str:
    """程序自带/安装目录下的 bin。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "bin")
    return path


def tools_bin_dir() -> str:
    """用户数据目录下的 tools/bin（一键安装目标）。"""
    path = os.path.join(app_data_dir(), "tools", "bin")
    os.makedirs(path, exist_ok=True)
    return path


def _candidate_ffmpeg_paths() -> list[str]:
    paths = []
    which = shutil.which("ffmpeg")
    if which:
        paths.append(which)
    which_p = shutil.which("ffprobe")
    # 常见位置 + 本应用安装位置
    if sys.platform.startswith("win"):
        extras = [
            os.path.join(tools_bin_dir(), "ffmpeg.exe"),
            os.path.join(bundled_bin_dir(), "ffmpeg.exe"),
            os.path.join(app_data_dir(), "tools", "ffmpeg", "bin", "ffmpeg.exe"),
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            os.path.join(os.path.expanduser("~"), "ffmpeg", "bin", "ffmpeg.exe"),
        ]
    elif sys.platform == "darwin":
        extras = [
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/opt/local/bin/ffmpeg",
            os.path.join(bundled_bin_dir(), "ffmpeg"),
            os.path.join(tools_bin_dir(), "ffmpeg"),
        ]
    else:
        extras = [
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            os.path.join(bundled_bin_dir(), "ffmpeg"),
            os.path.join(tools_bin_dir(), "ffmpeg"),
        ]
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        extras.extend([
            os.path.join(base, "ffmpeg.exe"),
            os.path.join(base, "bin", "ffmpeg.exe"),
            os.path.join(getattr(sys, "_MEIPASS", base), "ffmpeg.exe"),
        ])
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        extras.extend([
            os.path.join(base, "ffmpeg.exe"),
            os.path.join(base, "bin", "ffmpeg.exe"),
        ])
    paths.extend(extras)
    # 去重保序
    seen = set()
    out = []
    for p in paths:
        if not p:
            continue
        key = os.path.normcase(os.path.abspath(p)) if os.path.isabs(p) else p
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def resolve_ffmpeg_path() -> str:
    for path in _candidate_ffmpeg_paths():
        if path and os.path.isfile(path):
            return path
    try:
        import imageio_ffmpeg  # type: ignore
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return ""


def resolve_ffprobe_path() -> str:
    which = shutil.which("ffprobe")
    if which and os.path.isfile(which):
        return which
    ffmpeg = resolve_ffmpeg_path()
    if ffmpeg:
        probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe")
        if os.path.isfile(probe):
            return probe
    return ""


def _run_version(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        # ffmpeg 版本一般在第一行
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line[:160]
    except Exception as exc:
        return f"检测失败：{exc}"
    return ""


@dataclass
class EnvComponent:
    key: str
    name: str
    ok: bool
    required: bool
    detail: str
    path: str = ""
    installable: bool = False


@dataclass
class EnvReport:
    components: list[EnvComponent] = field(default_factory=list)

    @property
    def ready_for_hd(self) -> bool:
        return all(c.ok for c in self.components if c.required)

    @property
    def missing_required(self) -> list[EnvComponent]:
        return [c for c in self.components if c.required and not c.ok]

    def summary_line(self) -> str:
        parts = []
        for c in self.components:
            mark = "✓" if c.ok else "✗"
            parts.append(f"{mark}{c.name}")
        status = "高清环境就绪" if self.ready_for_hd else "缺少必需组件"
        return f"{status}  |  " + "  ".join(parts)


def detect_yt_dlp() -> EnvComponent:
    try:
        import yt_dlp  # noqa: F401
        try:
            from yt_dlp.version import __version__ as ver
        except Exception:
            ver = "已安装"
        return EnvComponent(
            key="yt_dlp",
            name="yt-dlp",
            ok=True,
            required=True,
            detail=f"版本 {ver}",
            path="",
            installable=not getattr(sys, "frozen", False),
        )
    except Exception as exc:
        return EnvComponent(
            key="yt_dlp",
            name="yt-dlp",
            ok=False,
            required=True,
            detail=f"未安装（{exc}）",
            installable=not getattr(sys, "frozen", False),
        )


def detect_ffmpeg() -> EnvComponent:
    path = resolve_ffmpeg_path()
    if path:
        ver = _run_version([path, "-version"])
        return EnvComponent(
            key="ffmpeg",
            name="ffmpeg",
            ok=True,
            required=True,
            detail=ver or path,
            path=path,
            installable=True,
        )
    return EnvComponent(
        key="ffmpeg",
        name="ffmpeg",
        ok=False,
        required=True,
        detail="未找到。高清合并（480p/720p/1080p/最佳）需要 ffmpeg。",
        installable=True,
    )


def detect_ffprobe() -> EnvComponent:
    path = resolve_ffprobe_path()
    if path:
        ver = _run_version([path, "-version"])
        return EnvComponent(
            key="ffprobe",
            name="ffprobe",
            ok=True,
            required=False,
            detail=ver or path,
            path=path,
            installable=True,
        )
    return EnvComponent(
        key="ffprobe",
        name="ffprobe",
        ok=False,
        required=False,
        detail="可选，随 ffmpeg 一键安装一并提供。",
        installable=True,
    )


def scan_environment() -> EnvReport:
    return EnvReport(components=[
        detect_yt_dlp(),
        detect_ffmpeg(),
        detect_ffprobe(),
    ])


def _normalize_sha256(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1].strip()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _http_download(
    url: str,
    target: str,
    expected_sha256: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    expected = _normalize_sha256(expected_sha256)
    if not expected:
        raise RuntimeError("下载资产缺少有效 SHA-256，已拒绝安装。")
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
        raise RuntimeError("FFmpeg 下载地址必须来自 github.com Release。")

    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    hasher = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        chunk = 256 * 1024
        with open(target, "wb") as out:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                hasher.update(buf)
                done += len(buf)
                if progress and total:
                    progress(done, total)

    actual = hasher.hexdigest().lower()
    if actual != expected:
        try:
            os.remove(target)
        except OSError:
            pass
        raise RuntimeError(f"FFmpeg SHA-256 校验失败：期望 {expected}，实际 {actual}")
    return target


def _pick_ffmpeg_asset() -> tuple[str, str, str]:
    """从 GitHub Release API 选择带 sha256 digest 的 win64 ZIP。"""
    if not sys.platform.startswith("win"):
        raise RuntimeError("便携 FFmpeg 自动下载仅用于 Windows。")

    for api in FFMPEG_RELEASE_APIS:
        try:
            req = urllib.request.Request(
                api,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            scored = []
            for asset in data.get("assets") or []:
                name = str(asset.get("name") or "")
                lower = name.lower()
                url = str(asset.get("browser_download_url") or "")
                digest = _normalize_sha256(asset.get("digest") or "")
                if not digest or not url.startswith("https://github.com/"):
                    continue
                if not lower.endswith(".zip") or ("win64" not in lower and "windows" not in lower):
                    continue
                if "shared" in lower:
                    continue
                score = 0
                if "gpl" in lower:
                    score += 2
                if "essentials" in lower or "release" in lower:
                    score += 3
                if "master" in lower or "latest" in lower:
                    score += 1
                if "lgpl" in lower:
                    score -= 1
                scored.append((score, url, digest, name))
            if scored:
                scored.sort(key=lambda x: -x[0])
                _, url, digest, name = scored[0]
                return url, digest, name
        except Exception:
            continue
    raise RuntimeError("没有找到带 SHA-256 digest 的可信 FFmpeg Windows Release 资产。")


def _extract_ffmpeg_from_zip(zip_path: str, dest_bin: str, log: Optional[Callable[[str], None]] = None) -> str:
    def _log(msg: str):
        if log:
            log(msg)

    os.makedirs(dest_bin, exist_ok=True)
    wanted = {"ffmpeg.exe", "ffprobe.exe", "ffplay.exe"}
    found_ffmpeg = ""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        # 找出 bin 目录下的 exe
        targets = []
        for name in names:
            base = os.path.basename(name).lower()
            if base in wanted and not name.endswith("/"):
                targets.append(name)
        if not targets:
            # 有的包直接在根目录
            for name in names:
                base = os.path.basename(name).lower()
                if base.startswith("ffmpeg") and base.endswith(".exe"):
                    targets.append(name)
        if not targets:
            raise RuntimeError("压缩包中未找到 ffmpeg.exe")

        for name in targets:
            base = os.path.basename(name)
            out = os.path.join(dest_bin, base)
            _log(f"解压 {base} …")
            with zf.open(name) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if base.lower() == "ffmpeg.exe":
                found_ffmpeg = out
    if not found_ffmpeg:
        # 再扫一遍目录
        for root, _, files in os.walk(dest_bin):
            for f in files:
                if f.lower() == "ffmpeg.exe":
                    found_ffmpeg = os.path.join(root, f)
                    break
    if not found_ffmpeg or not os.path.isfile(found_ffmpeg):
        raise RuntimeError("解压后仍未找到 ffmpeg.exe")
    return found_ffmpeg


def _install_ffmpeg_macos_homebrew(log: Optional[Callable[[str], None]] = None) -> str:
    def _log(msg: str):
        if log:
            log(msg)

    brew = shutil.which("brew")
    if not brew:
        for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if os.path.isfile(candidate):
                brew = candidate
                break
    if not brew:
        raise RuntimeError(
            "macOS 未检测到 Homebrew。请先从 https://brew.sh 安装 Homebrew，"
            "再重新点击安装组件。"
        )

    _log("macOS：通过 Homebrew 安装 ffmpeg…")
    proc = subprocess.run([brew, "install", "ffmpeg"], capture_output=True, text=True, timeout=1200)
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if output:
        _log(output[-1200:])
    if proc.returncode != 0:
        raise RuntimeError(f"Homebrew 安装 ffmpeg 失败（code={proc.returncode}）")

    path = resolve_ffmpeg_path()
    if not path:
        raise RuntimeError("Homebrew 安装完成，但仍未检测到 ffmpeg。")
    return path


def install_ffmpeg_portable(
    progress: Optional[Callable[[int, int], None]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """安装 FFmpeg。

    Windows：仅下载 GitHub Release API 返回且带 SHA-256 digest 的 ZIP。
    macOS：调用 Homebrew，不下载/执行未知二进制。
    """
    def _log(msg: str):
        if log:
            log(msg)

    if sys.platform == "darwin":
        return _install_ffmpeg_macos_homebrew(log=log)
    if not sys.platform.startswith("win"):
        raise RuntimeError("当前系统不支持一键安装 ffmpeg，请使用系统包管理器安装。")

    dest_bin = tools_bin_dir()
    _log(f"安装目录：{dest_bin}")
    url, digest, asset_name = _pick_ffmpeg_asset()
    _log(f"可信资产：{asset_name}")
    _log(f"SHA-256：{digest}")

    tmp_dir = tempfile.mkdtemp(prefix="diy_ffmpeg_")
    zip_path = os.path.join(tmp_dir, "ffmpeg.zip")
    try:
        _log("正在下载并校验 ffmpeg 组件…")
        _http_download(url, zip_path, expected_sha256=digest, progress=progress)
        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        _log(f"校验通过（{size_mb:.1f} MB），开始解压…")
        ffmpeg_path = _extract_ffmpeg_from_zip(zip_path, dest_bin, log=log)
        ver = _run_version([ffmpeg_path, "-version"])
        _log(f"安装成功：{ffmpeg_path}")
        if ver:
            _log(ver)
        return ffmpeg_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def install_ytdlp_pip(log: Optional[Callable[[str], None]] = None) -> bool:
    """仅非打包环境：pip 安装/升级 yt-dlp。"""
    def _log(msg: str):
        if log:
            log(msg)

    if getattr(sys, "frozen", False):
        _log("当前为打包版，yt-dlp 已内置，无需 pip 安装。")
        return True
    _log("正在通过 pip 安装/升级 yt-dlp …")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "yt-dlp==2026.8.19"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if out:
        _log(out[-500:])
    if proc.returncode != 0:
        raise RuntimeError(f"pip 安装 yt-dlp 失败（code={proc.returncode}）")
    _log("yt-dlp 安装完成。")
    return True


def install_missing_components(
    progress: Optional[Callable[[int, int], None]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> EnvReport:
    """检测并安装缺失的必需组件。"""
    def _log(msg: str):
        if log:
            log(msg)

    report = scan_environment()
    for comp in report.components:
        if comp.ok:
            _log(f"已就绪：{comp.name} — {comp.detail}")
            continue
        if not comp.installable:
            _log(f"无法自动安装：{comp.name} — {comp.detail}")
            continue
        if comp.key == "ffmpeg" or comp.key == "ffprobe":
            if not detect_ffmpeg().ok:
                install_ffmpeg_portable(progress=progress, log=log)
        elif comp.key == "yt_dlp":
            install_ytdlp_pip(log=log)

    # 装完再扫一次
    return scan_environment()
