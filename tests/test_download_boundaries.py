import errno
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import safe_downloads as sd
import secure_storage as storage
import sheets_batch_downloader as sheets
import batch_group_downloader as legacy
import updater


class Response(io.BytesIO):
    def __init__(self, data=b"new contents", headers=None):
        super().__init__(data)
        self.headers = headers or {}
        self.read_sizes = []

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("Response must be read in bounded chunks")
        self.read_sizes.append(size)
        return super().read(size)


class LocalFiles(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="diy-tests-")
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_normal_and_special_names(self):
        for name in ["..", ".", "CON", "nul.txt", "COM1", "a:b", "x\0y", "last. "]:
            with self.subTest(name=name):
                clean = sd.sanitize_component(name)
                result = sd.confined_path(str(self.root), clean)
                self.assertEqual(Path(result).parent, self.root)
                self.assertNotIn(clean, {".", "..", ""})
        self.assertEqual(sd.sanitize_component("照片 01.jpg"), "照片 01.jpg")
        self.assertEqual(Path(sd.confined_path(str(self.root), ".diy_folder_done.json")).parent, self.root)

    def test_escape_inputs_rejected(self):
        for name in ["../x", r"..\x", "/tmp/x", r"C:\x", r"\\server\share",
                     "x/../../y", "CON.txt", "x:stream", "x. "]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                sd.confined_path(str(self.root), name)

    def test_sink_rejects_escape_before_creating_file(self):
        with self.assertRaises(ValueError):
            with sd.DownloadFile(str(self.root), str(self.root/".."/"outside.txt")):
                self.fail("Should reject before opening")

    def test_symbolic_link_directory_rejected(self):
        outside = self.root/"outside"
        outside.mkdir()
        link = self.root/"chosen"/"link"
        link.parent.mkdir()
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        with self.assertRaises(ValueError):
            sd.confined_path(str(link.parent), "link/file.txt")
        with self.assertRaises(ValueError):
            sd.confined_path(str(link), "file.txt")

    def test_concurrent_downloads_preserve_existing_files(self):
        target = self.root/"same.txt"
        target.write_bytes(b"original")
        def save(i):
            with sd.DownloadFile(str(self.root), str(target)) as download:
                download.file.write(str(i).encode())
            return Path(download.path)
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(save, range(12)))
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(len(set(paths)), 12)
        self.assertEqual({p.read_bytes() for p in paths}, {str(i).encode() for i in range(12)})
        self.assertFalse(list(self.root.glob("*.part")))

    def test_long_name_collision_keeps_both_files(self):
        target = self.root/("a"*150 + ".jpg")
        target.write_bytes(b"original")
        with sd.DownloadFile(str(self.root), str(target)) as download:
            download.file.write(b"new")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(Path(download.path).read_bytes(), b"new")

    def test_no_hardlink_filesystem_fallback_is_exclusive(self):
        target = self.root/"same.txt"
        target.write_bytes(b"original")
        with patch.object(sd.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with sd.DownloadFile(str(self.root), str(target)) as download:
                download.file.write(b"second")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(Path(download.path).read_bytes(), b"second")

    def test_failed_download_removes_only_staging_file(self):
        target = self.root/"same.txt"
        target.write_bytes(b"original")
        with self.assertRaises(RuntimeError):
            with sd.DownloadFile(str(self.root), str(target)) as download:
                download.file.write(b"partial")
                raise RuntimeError("simulated network failure")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_http_response_name_cannot_overwrite(self):
        target = self.root/"existing.txt"
        target.write_bytes(b"original")
        response = Response(headers={"Content-Disposition": 'attachment; filename="existing.txt"'})
        with patch.object(sheets, "open_public", return_value=response):
            result = sheets.PublicDownloader().download(
                "https://public.example/different.txt", str(self.root/"different.txt"),
                output_root=str(self.root))
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(Path(result).read_bytes(), b"new contents")
        self.assertNotEqual(Path(result), target)

    def test_skip_existing_uses_final_response_name(self):
        target = self.root/"existing.txt"
        target.write_bytes(b"original")
        response = Response(headers={"Content-Disposition": 'attachment; filename="existing.txt"'})
        with patch.object(sheets, "open_public", return_value=response):
            result = sheets.PublicDownloader().download(
                "https://public.example/different.txt", str(self.root/"different.txt"),
                output_root=str(self.root), skip_existing=True)
        self.assertEqual(Path(result), target)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(response.read_sizes, [])

    def test_actual_ui_path_builders_remain_inside_output_root(self):
        from types import SimpleNamespace
        from sheets_batch_downloader_modern import build_target_path, folder_local_dir
        from paste_link_download_page import _build_target_path
        item = SimpleNamespace(group_name="..", row_number=2)
        for target in (build_target_path(str(self.root), item, "sample.jpg"),
                       folder_local_dir(str(self.root), item),
                       _build_target_path(str(self.root), "..", "sample.jpg")):
            self.assertTrue(Path(target).is_relative_to(self.root))
            self.assertNotEqual(Path(target), self.root)

    def test_drive_recursive_names_and_completed_write(self):
        client = object.__new__(sheets.GoogleClient)
        class Reply:
            def __init__(self, data):
                self.data = data
            def execute(self):
                return self.data
        class Files:
            def list(self, **kwargs):
                fid = kwargs["q"].split("'")[1]
                entries = {
                    "root": [{"id": "child", "name": "..", "mimeType": "application/vnd.google-apps.folder"}],
                    "child": [{"id": "file", "name": "sample.txt", "mimeType": "text/plain"}],
                }
                return Reply({"files": entries[fid]})
            def get_media(self, **kwargs):
                return b"sample"
        class Drive:
            def files(self):
                return Files()
        class Media:
            def __init__(self, fp, request, **kwargs):
                self.fp, self.request = fp, request
            def next_chunk(self):
                self.fp.write(self.request)
                return None, True
        client.drive = Drive()
        client.MediaIoBaseDownload = Media
        remote = client.list_folder_files("root")[0]
        target = sd.confined_path(str(self.root), remote["relative_path"])
        saved = client.download_drive_file("file", target, threading.Event(), output_root=str(self.root))
        self.assertEqual(Path(saved).read_bytes(), b"sample")
        self.assertTrue(Path(saved).is_relative_to(self.root))

    def test_failed_source_deletion_keeps_verified_copy_and_original(self):
        source, destination = self.root/"old.json", self.root/"new.json"
        self.token_fixture(source)
        unlink = storage.os.unlink
        def deny_old(path, *args, **kwargs):
            if os.fspath(path) == str(source):
                raise PermissionError("simulated denied deletion")
            return unlink(path, *args, **kwargs)
        with patch.object(storage.os, "unlink", side_effect=deny_old):
            with self.assertRaises(PermissionError):
                storage.migrate_legacy_token(str(source), str(destination))
        self.assertTrue(source.exists())
        self.assertEqual(json.loads(source.read_text()), json.loads(destination.read_text()))

    def test_drive_path_sink_rejects_escape(self):
        client = object.__new__(sheets.GoogleClient)
        client.drive = Mock()
        with self.assertRaises(ValueError):
            client.download_drive_file("fixture", str(self.root/".."/"escape.txt"),
                                       threading.Event(), output_root=str(self.root))

    def test_sheet_group_name_is_not_parent_directory(self):
        self.assertNotEqual(sheets.parse_title("..", 2, "full")[0], "..")

    def test_legacy_download_is_streamed(self):
        response = Response(b"x"*(sd.CHUNK_SIZE*2+3),
                            {"Content-Disposition": 'attachment; filename="photo.jpg"'})
        with patch.object(legacy, "open_public", return_value=response):
            result = legacy.Downloader().download(
                "https://public.example/photo.jpg", str(self.root/"001"),
                output_root=str(self.root))
        self.assertEqual(Path(result).stat().st_size, sd.CHUNK_SIZE*2+3)
        self.assertTrue(all(size == sd.CHUNK_SIZE for size in response.read_sizes))

    def test_stream_size_limit_cancel_and_short_body(self):
        with self.assertRaises(ValueError):
            sd.stream_copy(Response(b"123456"), io.BytesIO(), max_bytes=5)
        with self.assertRaises(ValueError):
            sd.stream_copy(Response(headers={"Content-Length": "10"}), io.BytesIO(), max_bytes=5)
        with self.assertRaises(OSError):
            sd.stream_copy(Response(b"short", {"Content-Length": "20"}), io.BytesIO())
        stop = threading.Event()
        stop.set()
        with self.assertRaises(RuntimeError):
            sd.stream_copy(Response(), io.BytesIO(), stop_event=stop)

    def test_update_digest_mismatch_deletes_new_file(self):
        target = self.root/"fixture-setup.exe"
        with patch.object(updater, "open_public", return_value=Response(b"wrong")):
            with self.assertRaises(RuntimeError):
                updater.download_file("https://github.com/fixture", str(target), "0"*64)
        self.assertFalse(target.exists())

    def test_update_never_truncates_existing_file(self):
        target = self.root/"fixture-setup.exe"
        target.write_bytes(b"existing")
        with patch.object(updater, "open_public", return_value=Response()):
            with self.assertRaises(FileExistsError):
                updater.download_file("https://github.com/fixture", str(target), "0"*64)
        self.assertEqual(target.read_bytes(), b"existing")

    def test_changed_update_is_not_started(self):
        target = self.root/"fixture-setup.exe"
        original = hashlib.sha256(b"verified").hexdigest()
        target.write_bytes(b"changed")
        with patch.object(updater.os, "startfile", create=True) as start:
            with self.assertRaises(ValueError):
                updater.launch_installer_or_replace(str(target), True, expected_sha256=original)
        start.assert_not_called()

    def test_verified_update_launch_and_lock(self):
        target = self.root/"fixture-setup.exe"
        target.write_bytes(b"harmless fixture")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        def fake_start(path):
            self.assertEqual(path, str(target))
            if os.name == "nt":
                with self.assertRaises(OSError):
                    target.write_bytes(b"cannot replace while locked")
                with self.assertRaises(OSError):
                    target.unlink()
        with patch.object(updater.sys, "platform", "win32"), patch.object(
                updater.os, "startfile", side_effect=fake_start, create=True) as start:
            updater.launch_installer_or_replace(str(target), True, expected_sha256=digest)
        start.assert_called_once()
        self.assertEqual(target.read_bytes(), b"harmless fixture")

    def test_updates_use_separate_directories(self):
        with patch.object(updater, "user_data_directory", return_value=str(self.root)):
            first, second = updater.default_download_dir(), updater.default_download_dir()
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).is_relative_to(self.root))
        if os.name != "nt":
            self.assertEqual(os.stat(first).st_mode & 0o777, 0o700)

    def token_fixture(self, path, token="local-test-refresh"):
        path.write_text(json.dumps({"client_id": "local-test-client", "refresh_token": token}))

    def test_token_migration_moves_verified_copy(self):
        source, destination = self.root/"old.json", self.root/"private"/"new.json"
        self.token_fixture(source)
        self.assertTrue(storage.migrate_legacy_token(str(source), str(destination)))
        self.assertFalse(source.exists())
        self.assertEqual(json.loads(destination.read_text())["client_id"], "local-test-client")
        if os.name != "nt":
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o700)

    def test_different_credentials_are_not_deleted(self):
        source, destination = self.root/"old.json", self.root/"new.json"
        self.token_fixture(source)
        self.token_fixture(destination, "other-local-test-refresh")
        with self.assertRaises(ValueError):
            storage.migrate_legacy_token(str(source), str(destination))
        self.assertTrue(source.exists())
        self.assertEqual(json.loads(destination.read_text())["refresh_token"], "other-local-test-refresh")

    def test_invalid_legacy_token_is_preserved(self):
        source, destination = self.root/"old.json", self.root/"new.json"
        source.write_text("{}")
        with self.assertRaises(ValueError):
            storage.migrate_legacy_token(str(source), str(destination))
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())

    def test_failed_credential_write_preserves_original(self):
        source, destination = self.root/"old.json", self.root/"new.json"
        self.token_fixture(source)
        with patch.object(storage.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                storage.migrate_legacy_token(str(source), str(destination))
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".credential-*")))


class PublicNetwork(unittest.TestCase):
    def addresses(self, *ips):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in ips]

    def test_non_public_addresses_blocked_before_connection(self):
        for ip in ["127.0.0.1", "192.168.1.10", "10.0.0.1", "169.254.169.254",
                   "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::"]:
            with self.subTest(ip=ip), patch.object(sd.socket, "getaddrinfo", return_value=self.addresses(ip)), patch.object(
                    sd.socket, "socket") as socket_factory:
                with self.assertRaises(ValueError):
                    sd._public_connection(("example.test", 443), timeout=1)
                socket_factory.assert_not_called()

    def test_mixed_dns_answer_blocked(self):
        with patch.object(sd.socket, "getaddrinfo", return_value=self.addresses("1.1.1.1", "127.0.0.1")):
            with self.assertRaises(ValueError):
                sd._public_connection(("example.test", 443))

    def test_connection_uses_validated_ip_without_second_lookup(self):
        with patch.object(sd.socket, "getaddrinfo", return_value=self.addresses("1.1.1.1")) as dns, patch.object(
                sd.socket, "socket") as factory:
            connection = sd._public_connection(("example.test", 443), timeout=3)
        dns.assert_called_once()
        factory.return_value.connect.assert_called_once_with(("1.1.1.1", 443))
        self.assertIs(connection, factory.return_value)

    def test_only_http_s_without_credentials(self):
        for url in ["file:///tmp/test", "ftp://example.com/f", "http://name:pass@example.com/f",
                    "http://[fe80::1%25eth0]/f", "https:///missing-host"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                sd.validate_public_url(url)

    def test_redirect_cannot_downgrade_tls(self):
        request = sd.urllib.request.Request("https://example.com/a")
        with self.assertRaises(ValueError):
            sd._PublicRedirect().redirect_request(request, None, 302, "Found", {},
                                                  "http://example.com/b")

    def test_redirect_to_loopback_is_blocked(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/private")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = sd._public_connection
        def route_only_initial_fixture(address, *args, **kwargs):
            if address[0] == "public.example":
                return socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
            return original(address, *args, **kwargs)
        try:
            with patch.object(sd, "_public_connection", side_effect=route_only_initial_fixture):
                with self.assertRaises(ValueError):
                    sd.open_public("http://public.example/start", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

