import http.cookiejar
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request
from safe_downloads import (sanitize_component, confined_path, DownloadFile,
                            open_public, public_opener, stream_copy)


APP_TITLE = "表格批量分组下载器"


def sanitize_path_part(value: str) -> str:
    return sanitize_component(value)


def parse_title(title: str, fallback_number: int, group_mode: str):
    text = str(title or "").strip()
    number = str(fallback_number)
    group_name = text or "未命名"
    prefix = text or "未命名"

    match = re.match(r"^(\d+)\s*-\s*([^-]+?)\s*-\s*([^\s]+)(?:\s+(.*))?$", text)
    if match:
        number = match.group(1)
        prefix = f"{match.group(1)}-{sanitize_path_part(match.group(2))}-{sanitize_path_part(match.group(3))}"
        group_name = match.group(3)
    else:
        number_match = re.match(r"^(\d+)", text)
        if number_match:
            number = number_match.group(1)

        first_part = text.split()[0] if text.split() else ""
        prefix = first_part or text or "未命名"
        group_name = first_part or text or "未命名"

    if group_mode == "prefix":
        group_name = prefix
    elif group_mode == "full":
        group_name = text or "未命名"

    return sanitize_path_part(group_name), sanitize_path_part(number)


def extract_drive_file_info(url: str):
    text = str(url or "").strip()
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    resource_key = query.get("resourcekey", [""])[0]

    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", text)
    if match:
        return match.group(1), resource_key

    file_id = query.get("id", [""])[0]
    if file_id:
        return file_id, resource_key

    return "", resource_key


def to_drive_download_url(url: str):
    file_id, resource_key = extract_drive_file_info(url)
    if not file_id:
        return url

    direct = f"https://drive.google.com/uc?export=download&id={quote(file_id)}"
    if resource_key:
        direct += f"&resourcekey={quote(resource_key)}"
    return direct


def extension_from_name(name: str) -> str:
    base = os.path.basename(name or "")
    _, ext = os.path.splitext(base)
    if ext and 2 <= len(ext) <= 6:
        return ext
    return ""


def filename_from_content_disposition(header: str) -> str:
    if not header:
        return ""

    match = re.search(r"filename\*=UTF-8''([^;]+)", header, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))

    match = re.search(r'filename="?([^";]+)"?', header, re.I)
    if match:
        return match.group(1).strip()

    return ""


class Downloader:
    def __init__(self):
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = public_opener(HTTPCookieProcessor(self.cookie_jar))

    def _request(self, url: str):
        return Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 batch-group-downloader",
                "Accept": "*/*",
            },
        )

    def _find_drive_confirm_url(self, html: bytes, current_url: str):
        text = html.decode("utf-8", errors="ignore")
        match = re.search(r'href="([^"]*?confirm=[^"]*?)"', text)
        if match:
            return urljoin("https://drive.google.com", match.group(1).replace("&amp;", "&"))

        for cookie in self.cookie_jar:
            if cookie.name.startswith("download_warning"):
                parsed = urlparse(current_url)
                query = parse_qs(parsed.query)
                query["confirm"] = [cookie.value]
                parts = []
                for key, values in query.items():
                    for value in values:
                        parts.append(f"{quote(key)}={quote(value)}")
                return parsed._replace(query="&".join(parts)).geturl()

        return ""

    def download(self, url: str, target_path_without_ext: str, default_ext: str = ".jpg",
                 *, output_root: str):
        download_url = to_drive_download_url(url)
        with open_public(download_url, opener=self.opener) as response:
            content_type = response.headers.get("Content-Type", "")
            host = (urlparse(download_url).hostname or "").lower()
            is_drive = host == "drive.google.com" or host.endswith(".drive.google.com")
            if is_drive and "text/html" in content_type.lower():
                # Drive confirmation pages are small; never buffer the file body.
                page = response.read(256 * 1024 + 1)
                if len(page) > 256 * 1024:
                    raise ValueError("Drive 确认页面过大。")
                confirm = self._find_drive_confirm_url(page, download_url)
                if not confirm:
                    raise RuntimeError("Drive 返回网页，请确认链接公开可下载。")
                with open_public(confirm, opener=self.opener) as confirmed:
                    if "text/html" in confirmed.headers.get("Content-Type", "").lower():
                        raise RuntimeError("Drive 返回网页，请使用授权下载入口。")
                    return self._save_response(confirmed, url, target_path_without_ext,
                                               default_ext, output_root)
            return self._save_response(response, url, target_path_without_ext,
                                       default_ext, output_root)

    def _save_response(self, response, url, target, default_ext, output_root):
        remote_name = filename_from_content_disposition(response.headers.get("Content-Disposition", ""))
        ext = extension_from_name(remote_name) or extension_from_name(urlparse(url).path) or default_ext
        target += ext
        with DownloadFile(output_root, target) as download:
            stream_copy(response, download.file)
        return download.path


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path

    root, ext = os.path.splitext(path)
    index = 2
    while True:
        candidate = f"{root}_{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


@dataclass
class DownloadItem:
    row_number: int
    title: str
    url: str
    group_name: str
    file_number: str


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x680")
        self.minsize(720, 620)

        self.log_queue = queue.Queue()
        self.worker = None

        self.output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "批量下载"))
        self.name_col = tk.IntVar(value=1)
        self.url_col = tk.IntVar(value=2)
        self.group_mode = tk.StringVar(value="person")

        self._build_ui()
        self.after(100, self._drain_log_queue)

    def _build_ui(self):
        main = ttk.Frame(self, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text=APP_TITLE, font=("", 16, "bold")).pack(anchor="w")

        path_row = ttk.Frame(main)
        path_row.pack(fill=tk.X, pady=(12, 4))
        ttk.Label(path_row, text="本地下载目录").pack(side=tk.LEFT)
        ttk.Entry(path_row, textvariable=self.output_dir).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(path_row, text="选择", command=self.choose_dir).pack(side=tk.LEFT)

        config = ttk.Frame(main)
        config.pack(fill=tk.X, pady=8)
        ttk.Label(config, text="名称列序号").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(config, from_=1, to=99, textvariable=self.name_col, width=8).grid(row=1, column=0, sticky="w", padx=(0, 18))
        ttk.Label(config, text="链接列序号").grid(row=0, column=1, sticky="w")
        ttk.Spinbox(config, from_=1, to=99, textvariable=self.url_col, width=8).grid(row=1, column=1, sticky="w", padx=(0, 18))
        ttk.Label(config, text="分组规则").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            config,
            textvariable=self.group_mode,
            width=26,
            state="readonly",
            values=[
                "person",
                "prefix",
                "full",
            ],
        ).grid(row=1, column=2, sticky="w")

        ttk.Label(
            main,
            text="分组规则说明：person = 张三；prefix = 485-ZB-张三；full = 整行名称。",
            foreground="#5f6368",
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(main, text="从表格复制后粘贴到这里（默认第 1 列名称，第 2 列链接）").pack(anchor="w")
        self.text = tk.Text(main, height=14, wrap="none")
        self.text.pack(fill=tk.BOTH, expand=True, pady=(4, 10))
        self.text.insert(
            "1.0",
            "485-ZB-张三 老太太1\thttps://drive.google.com/file/d/...\n"
            "486-ZB-张三 老太太2\thttps://drive.google.com/file/d/...\n",
        )

        button_row = ttk.Frame(main)
        button_row.pack(fill=tk.X)
        self.start_button = ttk.Button(button_row, text="开始批量下载", command=self.start)
        self.start_button.pack(side=tk.LEFT)
        ttk.Button(button_row, text="清空日志", command=lambda: self.log.delete("1.0", tk.END)).pack(side=tk.LEFT, padx=8)

        ttk.Label(main, text="日志").pack(anchor="w", pady=(12, 4))
        self.log = tk.Text(main, height=10, wrap="word", bg="#1f1f1f", fg="#7cff7c", insertbackground="#7cff7c")
        self.log.pack(fill=tk.BOTH)

    def choose_dir(self):
        path = filedialog.askdirectory(initialdir=self.output_dir.get() or os.path.expanduser("~"))
        if path:
            self.output_dir.set(path)

    def parse_items(self):
        raw = self.text.get("1.0", tk.END)
        lines = [line for line in raw.splitlines() if line.strip()]
        name_index = max(1, int(self.name_col.get())) - 1
        url_index = max(1, int(self.url_col.get())) - 1
        group_mode = self.group_mode.get()

        items = []
        for i, line in enumerate(lines, start=1):
            cells = line.split("\t")
            title = cells[name_index].strip() if name_index < len(cells) else ""
            url = cells[url_index].strip() if url_index < len(cells) else ""
            group_name, file_number = parse_title(title, i, group_mode)
            items.append(DownloadItem(i, title, url, group_name, file_number))
        return items

    def start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "任务正在运行。")
            return

        items = self.parse_items()
        if not items:
            messagebox.showwarning(APP_TITLE, "请先粘贴表格数据。")
            return

        output_dir = self.output_dir.get().strip()
        if not output_dir:
            messagebox.showwarning(APP_TITLE, "请先选择下载目录。")
            return

        self.start_button.config(state=tk.DISABLED)
        self.worker = threading.Thread(target=self.run_downloads, args=(items, output_dir), daemon=True)
        self.worker.start()

    def run_downloads(self, items, output_dir):
        downloader = Downloader()
        success = 0
        skipped = 0
        failed = 0

        self.log_queue.put(f"准备下载 {len(items)} 行。")
        for item in items:
            if not item.url or re.search(r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders|folderview)", item.url, re.I):
                skipped += 1
                self.log_queue.put(f"第 {item.row_number} 行跳过：空链接或文件夹链接")
                continue

            target_without_ext = confined_path(output_dir, item.group_name, item.file_number)
            try:
                saved_path = downloader.download(item.url, target_without_ext, output_root=output_dir)
                success += 1
                self.log_queue.put(f"成功：{saved_path}")
                time.sleep(0.2)
            except Exception as exc:
                failed += 1
                self.log_queue.put(f"第 {item.row_number} 行失败：{exc}")

        self.log_queue.put(f"完成：成功 {success}，跳过 {skipped}，失败 {failed}。")
        self.log_queue.put("__DONE__")

    def _drain_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                if message == "__DONE__":
                    self.start_button.config(state=tk.NORMAL)
                else:
                    now = time.strftime("%H:%M:%S")
                    self.log.insert(tk.END, f"[{now}] {message}\n")
                    self.log.see(tk.END)
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)


if __name__ == "__main__":
    App().mainloop()
