"""GitHub Release 自动检查更新、下载与安装提示。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from version import APP_VERSION, GITHUB_REPO_SLUG, RELEASES_API, RELEASES_PAGE


USER_AGENT = f"DIYDownloader/{APP_VERSION} (+https://github.com/{GITHUB_REPO_SLUG})"


@dataclass
class ReleaseInfo:
    tag: str
    version: str
    name: str
    body: str
    html_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    is_installer: bool


def parse_version(text: str) -> tuple:
    raw = str(text or "").strip().lstrip("vV")
    parts = re.findall(r"\d+", raw)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def _http_get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _pick_asset(assets: list) -> Optional[dict]:
    if not assets:
        return None
    names = [(a, str(a.get("name") or "")) for a in assets]
    # 优先 Windows 安装包（onedir，稳定）
    for a, name in names:
        lower = name.lower()
        if lower.endswith("-setup.exe") or lower.endswith("setup.exe") or "installer" in lower:
            if lower.endswith(".exe"):
                return a
    # 其次 Windows 便携 zip（onedir 压缩包）
    for a, name in names:
        lower = name.lower()
        if "windows" in lower and lower.endswith(".zip"):
            return a
    # 兼容旧版 onefile exe
    for a, name in names:
        lower = name.lower()
        if "windows" in lower and lower.endswith(".exe"):
            return a
    for a, name in names:
        if name.lower().endswith(".exe"):
            return a
    return None


def fetch_latest_release() -> Optional[ReleaseInfo]:
    try:
        data = _http_get_json(RELEASES_API)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    tag = str(data.get("tag_name") or "")
    if not tag:
        return None
    asset = _pick_asset(data.get("assets") or [])
    if not asset:
        return None
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if not url:
        return None
    is_installer = bool(
        re.search(r"(setup|installer|install)", name, re.I)
        or name.lower().endswith("-setup.exe")
        or name.lower().endswith(".zip")  # 便携 zip：下载后打开目录由用户解压覆盖
    )
    return ReleaseInfo(
        tag=tag,
        version=tag.lstrip("vV"),
        name=str(data.get("name") or tag),
        body=str(data.get("body") or ""),
        html_url=str(data.get("html_url") or RELEASES_PAGE),
        asset_name=name,
        asset_url=url,
        asset_size=int(asset.get("size") or 0),
        is_installer=is_installer,
    )


def check_for_update() -> Optional[ReleaseInfo]:
    info = fetch_latest_release()
    if not info:
        return None
    if not is_newer(info.version, APP_VERSION):
        return None
    return info


def download_file(url: str, target_path: str, progress_callback=None) -> str:
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/octet-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk = 1024 * 256
        with open(target_path, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                if progress_callback and total:
                    progress_callback(downloaded, total)
    return target_path


def default_download_dir() -> str:
    base = os.path.join(tempfile.gettempdir(), "DIYDownloader-updates")
    os.makedirs(base, exist_ok=True)
    return base


def download_release(info: ReleaseInfo, progress_callback=None) -> str:
    target = os.path.join(default_download_dir(), info.asset_name)
    return download_file(info.asset_url, target, progress_callback=progress_callback)


def current_executable() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0] or __file__)


def launch_installer_or_replace(downloaded_path: str, is_installer: bool) -> None:
    """
    安装包：直接启动安装程序。
    便携 exe：写 bat 替换当前程序并重启。
    """
    downloaded_path = os.path.abspath(downloaded_path)
    if not os.path.isfile(downloaded_path):
        raise FileNotFoundError(downloaded_path)

    lower = downloaded_path.lower()
    if is_installer or lower.endswith("setup.exe"):
        if sys.platform.startswith("win"):
            os.startfile(downloaded_path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen([downloaded_path], close_fds=True)
        return

    # zip 便携包：打开所在目录，提示用户解压覆盖安装目录
    if lower.endswith(".zip"):
        folder = os.path.dirname(downloaded_path)
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        return

    # 便携版：替换自身
    current = current_executable()
    if not getattr(sys, "frozen", False):
        # 开发模式：只打开下载目录/文件
        folder = os.path.dirname(downloaded_path)
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        return

    bat_path = os.path.join(default_download_dir(), "apply_update.bat")
    # 等进程退出后覆盖并启动
    content = f"""@echo off
chcp 65001 >nul
setlocal
set "SRC={downloaded_path}"
set "DST={current}"
echo 正在安装更新，请稍候...
ping 127.0.0.1 -n 3 >nul
:retry
copy /Y "%SRC%" "%DST%" >nul 2>&1
if errorlevel 1 (
  ping 127.0.0.1 -n 2 >nul
  goto retry
)
start "" "%DST%"
del "%~f0" >nul 2>&1
"""
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.Popen(
        ["cmd", "/c", bat_path],
        cwd=os.path.dirname(bat_path),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        close_fds=True,
    )


def format_size(num: int) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
