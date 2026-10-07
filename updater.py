"""GitHub Release auto-update with asset digest verification and macOS support."""

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
_ALLOWED_RELEASE_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
}


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
    asset_digest: str = ""


def parse_version(text: str) -> tuple:
    raw = str(text or "").strip().lstrip("vV")
    parts = re.findall(r"\d+", raw)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def _is_allowed_release_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return parsed.scheme.lower() == "https" and host in _ALLOWED_RELEASE_HOSTS


def _http_get_json(url: str, timeout: int = 20) -> dict:
    if not _is_allowed_release_url(url):
        raise RuntimeError(f"Refusing non-GitHub HTTPS update URL: {url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        if not _is_allowed_release_url(final_url):
            raise RuntimeError(f"Update API redirected to untrusted host: {final_url}")
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _pick_asset(assets: list) -> Optional[dict]:
    if not assets:
        return None
    names = [(a, str(a.get("name") or "")) for a in assets]

    if sys.platform.startswith("win"):
        for a, name in names:
            lower = name.lower()
            if (
                lower.endswith("-setup.exe")
                or lower.endswith("setup.exe")
                or "installer" in lower
            ) and lower.endswith(".exe"):
                return a
        for a, name in names:
            lower = name.lower()
            if "windows" in lower and lower.endswith(".zip"):
                return a
        return None

    if sys.platform == "darwin":
        machine = platform.machine().lower()
        wants_arm = machine in ("arm64", "aarch64")
        mac_assets = []
        for a, name in names:
            lower = name.lower()
            if (
                "macos" in lower or "darwin" in lower or "mac-" in lower
            ) and lower.endswith((".zip", ".dmg")):
                mac_assets.append((a, lower))
        preferred = (
            ("arm64", "aarch64", "apple")
            if wants_arm
            else ("x64", "x86_64", "intel")
        )
        for a, lower in mac_assets:
            if any(token in lower for token in preferred):
                return a
        return mac_assets[0][0] if mac_assets else None

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
    digest = str(asset.get("digest") or "")
    if not url or not _is_allowed_release_url(url):
        return None
    if os.path.basename(name) != name or name in (".", ".."):
        raise RuntimeError("Unsafe GitHub Release asset name; auto-update aborted.")
    lower = name.lower()
    is_installer = lower.endswith((".exe", ".dmg")) and not lower.endswith(".zip")
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
        asset_digest=digest,
    )


def check_for_update() -> Optional[ReleaseInfo]:
    info = fetch_latest_release()
    if not info or not is_newer(info.version, APP_VERSION):
        return None
    return info


def _parse_sha256_digest(value: str) -> str:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", text)
    if not match:
        raise RuntimeError(
            "Release asset has no verifiable GitHub SHA-256 digest; "
            "refusing automatic installation."
        )
    return match.group(1)


def download_file(
    url: str,
    target_path: str,
    progress_callback=None,
    expected_digest: str = "",
    expected_size: int = 0,
) -> str:
    if not _is_allowed_release_url(url):
        raise RuntimeError(f"Refusing non-GitHub HTTPS update URL: {url}")
    expected_sha256 = _parse_sha256_digest(expected_digest)
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    part_path = target_path + ".part"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
    )
    hasher = hashlib.sha256()
    downloaded = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            final_url = resp.geturl()
            if not _is_allowed_release_url(final_url):
                raise RuntimeError(
                    f"Update download redirected to untrusted host: {final_url}"
                )
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            with open(part_path, "wb") as out:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        progress_callback(downloaded, total)
        if expected_size and downloaded != int(expected_size):
            raise RuntimeError(
                f"Update size mismatch: expected {expected_size}, got {downloaded}"
            )
        actual_sha256 = hasher.hexdigest().lower()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"Update SHA-256 mismatch: expected {expected_sha256}, "
                f"got {actual_sha256}"
            )
        os.replace(part_path, target_path)
        return target_path
    except Exception:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except OSError:
            pass
        raise


def default_download_dir() -> str:
    base = os.path.join(tempfile.gettempdir(), "DIYDownloader-updates")
    os.makedirs(base, exist_ok=True)
    return base


def download_release(info: ReleaseInfo, progress_callback=None) -> str:
    safe_name = os.path.basename(str(info.asset_name or ""))
    if not safe_name or safe_name != info.asset_name or safe_name in (".", ".."):
        raise RuntimeError("Unsafe update asset filename.")
    target = os.path.join(default_download_dir(), safe_name)
    return download_file(
        info.asset_url,
        target,
        progress_callback=progress_callback,
        expected_digest=info.asset_digest,
        expected_size=info.asset_size,
    )


def current_executable() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0] or __file__)


def _open_path(path: str, reveal: bool = False) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        cmd = ["/usr/bin/open"]
        if reveal:
            cmd.append("-R")
        cmd.append(path)
        subprocess.Popen(cmd, close_fds=True)
        return
    subprocess.Popen(["xdg-open", path], close_fds=True)


def launch_installer_or_replace(downloaded_path: str, is_installer: bool) -> None:
    """Windows installer/legacy replacement; macOS only opens verified packages."""
    downloaded_path = os.path.abspath(downloaded_path)
    if not os.path.isfile(downloaded_path):
        raise FileNotFoundError(downloaded_path)
    lower = downloaded_path.lower()

    if sys.platform == "darwin":
        if lower.endswith(".dmg"):
            _open_path(downloaded_path)
            return
        if lower.endswith(".zip"):
            _open_path(downloaded_path, reveal=True)
            return
        raise RuntimeError("macOS auto-update accepts only .zip or .dmg assets.")

    if not sys.platform.startswith("win"):
        _open_path(os.path.dirname(downloaded_path))
        return

    if is_installer or lower.endswith("setup.exe"):
        if not lower.endswith(".exe"):
            raise RuntimeError("Windows installer must be an .exe file.")
        os.startfile(downloaded_path)  # type: ignore[attr-defined]
        return

    if lower.endswith(".zip"):
        _open_path(os.path.dirname(downloaded_path))
        return

    current = current_executable()
    if not getattr(sys, "frozen", False):
        _open_path(os.path.dirname(downloaded_path))
        return
    if not lower.endswith(".exe"):
        raise RuntimeError("Windows portable self-replace accepts only .exe files.")

    bat_path = os.path.join(default_download_dir(), "apply_update.bat")
    # ASCII-only body avoids cmd.exe code-page corruption.
    content = f"""@echo off
setlocal
set "SRC={downloaded_path}"
set "DST={current}"
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
    with open(bat_path, "w", encoding="ascii", errors="strict") as f:
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
