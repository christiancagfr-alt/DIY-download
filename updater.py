"""GitHub Release 自动检查更新、下载与安装提示。

安全边界：
- 只从 version.py 指定的个人 GitHub 仓库读取 Release。
- 自动下载必须带 GitHub Release asset 的 sha256 digest，并在本地校验。
- macOS/Linux 不自动执行下载产物；ZIP 只打开所在目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from version import APP_VERSION, GITHUB_REPO_SLUG, RELEASES_API, RELEASES_PAGE


USER_AGENT = f"DIYDownloader/{APP_VERSION} (+https://github.com/{GITHUB_REPO_SLUG})"
_ALLOWED_DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}


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
    asset_digest: str
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
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _mac_arch_tokens() -> tuple[str, ...]:
    machine = (platform.machine() or "").lower()
    if machine in {"arm64", "aarch64"}:
        return ("arm64", "aarch64", "apple-silicon")
    return ("x86_64", "x64", "intel", "amd64")


def _pick_asset(assets: list) -> Optional[dict]:
    if not assets:
        return None

    named = [(a, str(a.get("name") or "")) for a in assets]

    if sys.platform.startswith("win"):
        for a, name in named:
            lower = name.lower()
            if lower.endswith("-setup.exe") or (lower.endswith(".exe") and ("setup" in lower or "installer" in lower)):
                return a
        for a, name in named:
            lower = name.lower()
            if "windows" in lower and lower.endswith(".zip"):
                return a
        return None

    if sys.platform == "darwin":
        tokens = _mac_arch_tokens()
        for a, name in named:
            lower = name.lower()
            if "macos" in lower and lower.endswith(".zip") and any(token in lower for token in tokens):
                return a
        # Backward compatibility with older single-arch/universal releases.
        for a, name in named:
            lower = name.lower()
            if "macos" in lower and lower.endswith(".zip"):
                return a
        return None

    return None


def _normalized_sha256(digest: str) -> str:
    value = str(digest or "").strip().lower()
    if value.startswith("sha256:"):
        value = value.split(":", 1)[1].strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        return ""
    return value


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

    name = str(asset.get("name") or "").strip()
    url = str(asset.get("browser_download_url") or "").strip()
    digest = _normalized_sha256(asset.get("digest") or "")
    if not name or name != os.path.basename(name):
        raise RuntimeError("Release 资产文件名不安全，已拒绝自动更新。")
    if not url or urlparse(url).scheme != "https":
        raise RuntimeError("Release 资产下载地址不是 HTTPS，已拒绝自动更新。")
    if (urlparse(url).hostname or "").lower() not in _ALLOWED_DOWNLOAD_HOSTS:
        raise RuntimeError("Release 资产下载地址不属于 GitHub，已拒绝自动更新。")
    if not digest:
        raise RuntimeError("Release 资产缺少 SHA-256 digest，已拒绝自动更新。")

    lower = name.lower()
    is_installer = bool(sys.platform.startswith("win") and lower.endswith(".exe") and ("setup" in lower or "installer" in lower))
    return ReleaseInfo(
        tag=tag,
        version=tag.lstrip("vV"),
        name=str(data.get("name") or tag),
        body=str(data.get("body") or ""),
        html_url=str(data.get("html_url") or RELEASES_PAGE),
        asset_name=name,
        asset_url=url,
        asset_size=int(asset.get("size") or 0),
        asset_digest=digest,
        is_installer=is_installer,
    )


def check_for_update() -> Optional[ReleaseInfo]:
    info = fetch_latest_release()
    if not info:
        return None
    if not is_newer(info.version, APP_VERSION):
        return None
    return info


def download_file(url: str, target_path: str, expected_sha256: str, progress_callback=None) -> str:
    expected = _normalized_sha256(expected_sha256)
    if not expected:
        raise RuntimeError("缺少有效的 SHA-256，禁止自动下载更新。")

    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_DOWNLOAD_HOSTS:
        raise RuntimeError("更新下载地址不安全。")

    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/octet-stream",
        },
    )

    hasher = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk = 1024 * 256
        with open(target_path, "wb") as out:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                hasher.update(buf)
                downloaded += len(buf)
                if progress_callback and total:
                    progress_callback(downloaded, total)

    actual = hasher.hexdigest().lower()
    if actual != expected:
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise RuntimeError(
            "更新文件 SHA-256 校验失败，文件已删除。\n"
            f"期望：{expected}\n实际：{actual}"
        )
    return target_path


def default_download_dir() -> str:
    base = os.path.join(tempfile.gettempdir(), "DIYDownloader-updates")
    os.makedirs(base, exist_ok=True)
    return base


def download_release(info: ReleaseInfo, progress_callback=None) -> str:
    target = os.path.join(default_download_dir(), os.path.basename(info.asset_name))
    return download_file(
        info.asset_url,
        target,
        expected_sha256=info.asset_digest,
        progress_callback=progress_callback,
    )


def current_executable() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0] or __file__)


def _open_folder(folder: str) -> None:
    folder = os.path.abspath(folder)
    if sys.platform.startswith("win"):
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder], close_fds=True)
    else:
        subprocess.Popen(["xdg-open", folder], close_fds=True)


def launch_installer_or_replace(downloaded_path: str, is_installer: bool) -> None:
    """安全启动更新。

    Windows 仅允许显式 setup/installer EXE 自动启动。
    ZIP（包括 macOS 包）只打开下载目录，由用户手动替换/安装。
    """
    downloaded_path = os.path.abspath(downloaded_path)
    if not os.path.isfile(downloaded_path):
        raise FileNotFoundError(downloaded_path)

    lower = downloaded_path.lower()

    if lower.endswith(".zip"):
        _open_folder(os.path.dirname(downloaded_path))
        return

    if sys.platform.startswith("win") and is_installer and lower.endswith(".exe"):
        base = os.path.basename(lower)
        if "setup" not in base and "installer" not in base:
            raise RuntimeError("拒绝启动未识别的可执行更新文件。")
        os.startfile(downloaded_path)  # type: ignore[attr-defined]
        return

    # 非 Windows 或未知类型一律不执行下载文件。
    _open_folder(os.path.dirname(downloaded_path))


def format_size(num: int) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
