"""Private local credential and update-file storage."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager

from safe_downloads import _is_link


def private_directory(path: str) -> str:
    path = os.path.abspath(path)
    if _is_link(path):
        raise ValueError("私有数据目录不能是符号链接或目录联接。")
    os.makedirs(path, mode=0o700, exist_ok=True)
    if _is_link(path):
        raise ValueError("私有数据目录不能是符号链接或目录联接。")
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def user_data_directory() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return private_directory(os.path.join(base, "DIYDownloader"))


def atomic_private_text(path: str, content: str) -> None:
    path = os.path.abspath(path)
    parent = private_directory(os.path.dirname(path))
    if _is_link(path):
        raise ValueError("凭据文件不能是符号链接。")
    fd, temporary = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_token(path: str) -> dict:
    if _is_link(path):
        raise ValueError("旧授权缓存是链接，未执行迁移。")
    with open(path, "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) and data[key] for key in ("client_id", "refresh_token")
    ):
        raise ValueError("授权缓存不完整，保留原文件，请重新登录或手动检查。")
    return data


def migrate_legacy_token(source: str, destination: str) -> bool:
    source, destination = os.path.abspath(source), os.path.abspath(destination)
    if source == destination or not os.path.lexists(source):
        return False
    data = _read_token(source)
    if os.path.exists(destination):
        stored = _read_token(destination)
        if any(stored.get(key) != data.get(key) for key in ("client_id", "refresh_token")):
            raise ValueError("新旧授权缓存不一致，已保留旧文件，请核实账号后清理。")
    else:
        atomic_private_text(destination, json.dumps(data))
    # Verify the saved credential before removing the only original copy.
    stored = _read_token(destination)
    if any(stored.get(key) != data.get(key) for key in ("client_id", "refresh_token")):
        raise ValueError("授权迁移验证失败，已保留旧文件。")
    if os.name != "nt":
        os.chmod(destination, 0o600)
    os.unlink(source)
    return True


@contextmanager
def verified_update(path: str, expected_sha256: str):
    """On Windows deny writes/deletion while hashing and starting the installer."""
    if _is_link(path):
        raise ValueError("更新文件不能是链接。")
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = create(path, 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
        file = os.fdopen(fd, "rb")
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        file = os.fdopen(fd, "rb")
    with file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("更新目标不是普通文件。")
        hasher = hashlib.sha256()
        while chunk := file.read(256 * 1024):
            hasher.update(chunk)
        if hasher.hexdigest() != expected_sha256:
            raise ValueError("更新文件已改变或损坏，请重新下载。")
        yield

