"""Shared filesystem and public HTTP boundaries for downloaded content."""
from __future__ import annotations

import errno
import http.client
import ipaddress
import ntpath
import os
import re
import shutil
import socket
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

CHUNK_SIZE = 256 * 1024
MAX_DOWNLOAD_BYTES = 100 * 1024**3
MAX_DOWNLOAD_SECONDS = 24 * 60 * 60


def sanitize_component(value: str, *, limit: int = 150) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", str(value or "未命名"))
    text = re.sub(r"\s+", " ", text).strip().rstrip(" .")[:limit].rstrip(" .")
    if not text:
        text = "未命名"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", text.split(".")[0]):
        text = "_" + text
    return text


def _is_link(path: str) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def confined_path(root: str, *parts: str) -> str:
    """Reject traversal and existing links, including Windows junctions."""
    root = os.path.abspath(root)
    if _is_link(root):
        raise ValueError("下载根目录不能是符号链接或目录联接。")
    components = []
    for part in parts:
        part = str(part)
        if not part or ntpath.splitdrive(part)[0] or part.startswith(("/", "\\")):
            raise ValueError("下载路径必须位于所选目录内。")
        for component in re.split(r"[/\\]", part):
            if (not component or component in {".", ".."}
                    or component != sanitize_component(component, limit=max(len(component), 150))):
                raise ValueError("下载路径包含不安全的文件名。")
            components.append(component)
    current = root
    for component in components:
        current = os.path.join(current, component)
        if _is_link(current):
            raise ValueError("下载路径不能经过符号链接或目录联接。")
    real_root = os.path.realpath(root)
    if os.path.commonpath([real_root, os.path.realpath(current)]) != real_root:
        raise ValueError("下载路径超出所选目录。")
    return current


def checked_target(root: str, target: str) -> str:
    # Preserve raw dot components for validation; abspath would erase them.
    raw = str(target)
    if ".." in re.split(r"[/\\]", raw):
        raise ValueError("下载路径不能包含上级目录。")
    absolute = os.path.abspath(raw)
    relative = os.path.relpath(absolute, os.path.abspath(root))
    return confined_path(root, relative)


class DownloadFile:
    """Private staging file; publish without ever replacing another file."""
    def __init__(self, root: str, target: str, *, skip_existing: bool = False):
        self.root = os.path.abspath(root)
        self.target = checked_target(self.root, target)
        self.skip_existing = skip_existing
        self.path = self.target
        self.temp = ""
        self.file = None

    def __enter__(self):
        parent = os.path.dirname(self.target)
        os.makedirs(parent, exist_ok=True)
        checked_target(self.root, self.target)
        fd, self.temp = tempfile.mkstemp(prefix=".diy-", suffix=".part", dir=parent)
        self.file = os.fdopen(fd, "wb")
        return self

    def _publish(self):
        stem, ext = os.path.splitext(self.target)
        for index in range(1, 10001):
            destination = self.target if index == 1 else f"{stem}_{index}{ext}"
            checked_target(self.root, destination)
            try:
                # Atomic no-replace on NTFS, APFS and usual Unix filesystems.
                os.link(self.temp, destination)
                self.path = destination
                return
            except FileExistsError:
                if self.skip_existing and os.path.isfile(destination):
                    self.path = destination
                    return
                continue
            except OSError as exc:
                if exc.errno not in {errno.EPERM, errno.EACCES, errno.ENOTSUP,
                                     errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS}:
                    raise
            # Filesystems without hardlinks: reserve the final name exclusively.
            try:
                out = open(destination, "xb")
            except FileExistsError:
                if self.skip_existing and os.path.isfile(destination):
                    self.path = destination
                    return
                continue
            try:
                with out, open(self.temp, "rb") as source:
                    shutil.copyfileobj(source, out, CHUNK_SIZE)
            except BaseException:
                os.unlink(destination)
                raise
            self.path = destination
            return
        raise RuntimeError("同名文件过多，请更换下载目录。")

    def __exit__(self, kind, value, traceback):
        try:
            self.file.close()
            if kind is None:
                self._publish()
        finally:
            if self.temp and os.path.exists(self.temp):
                os.unlink(self.temp)


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("普通下载仅允许不含登录信息的 HTTP(S) 链接。")
    if "%" in parsed.hostname:
        raise ValueError("下载地址不允许 IPv6 区域标识。")
    # Also validate malformed port syntax before starting a request.
    _ = parsed.port


def _public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                       source_address=None, *, all_errors=False):
    host, port = address
    resolved = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not resolved:
        raise OSError("下载地址没有可用的网络地址。")
    for family, kind, proto, canonname, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b:1::/48"):
                raise ValueError("已阻止可能通往非公网地址的 IPv6 转换地址。")
            if ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            elif ip in ipaddress.ip_network("64:ff9b::/96"):
                ip = ipaddress.IPv4Address(int(ip) & 0xffffffff)
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("为保护本机和局域网，已阻止访问非公网下载地址。")
    last_error = None
    # Connect directly to the validated sockaddr, without a second DNS lookup.
    for family, kind, proto, canonname, sockaddr in resolved:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError("下载连接失败。")


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        validate_public_url(request.full_url)
        return self.do_open(_PublicHTTPConnection, request)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        validate_public_url(request.full_url)
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, request, fp, code, message, headers, newurl):
        validate_public_url(newurl)
        if urlsplit(request.full_url).scheme == "https" and urlsplit(newurl).scheme != "https":
            raise ValueError("已阻止 HTTPS 下载降级为明文连接。")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def public_opener(*handlers):
    # A proxy would resolve/connect independently and bypass the address policy.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _PublicHTTPHandler(),
        _PublicHTTPSHandler(), _PublicRedirect(), *handlers,
    )


def open_public(url: str, *, timeout: int = 60, opener=None):
    validate_public_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "DIYDownloader"})
    return (opener or public_opener()).open(request, timeout=timeout)


def stream_copy(response, out, *, stop_event=None, pause_event=None,
                max_bytes=MAX_DOWNLOAD_BYTES, max_seconds=MAX_DOWNLOAD_SECONDS):
    total = int(response.headers.get("Content-Length") or 0)
    if total < 0 or total > max_bytes:
        raise ValueError("下载文件超过允许的大小。")
    started = time.monotonic()
    received = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("任务已停止")
        if time.monotonic() - started > max_seconds:
            raise TimeoutError("下载超过允许的总时长。")
        if pause_event is not None and pause_event.is_set():
            time.sleep(0.2)
            continue
        chunk = response.read(CHUNK_SIZE)
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise ValueError("下载文件超过允许的大小。")
        out.write(chunk)
    if total and received != total:
        raise OSError("下载未完成，未保留不完整文件。")

