"""批量上传到 Google Drive + 写入入库表。

- 左：可折叠设置 + 分类选择 + 拖拽文件夹
- 右：任务列表 / 进度 / 回执
- 分类全部来自表格；拖入本地分类文件夹可自动匹配
- 凭据与主界面共用；token 固定路径，授权一次后自动刷新
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QThread, Qt, QUrl, Signal, QEvent, QTimer
from PySide6.QtGui import QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QStyle,
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
    QScrollArea,
    QSpinBox,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from transfer_widgets import drawer, compact_field
from column_mapping import MappingEditor, default_mappings, validate_mappings, mapped_cells, mappings_from_headers
from secure_storage import user_data_directory
from transfer_widgets import SafeEditTable as QTableWidget
from focus_widgets import FocusComboBox as QComboBox, FocusSpinBox as QSpinBox
from sheets_batch_downloader import (
    GoogleClient,
    column_to_number,
    default_token_path,
    number_to_column,
)


APP_SECTION = "批量上传云端"
DEFAULT_PARENT_FOLDER_ID = ""
DATA_SHEET_NAME = ""
CATEGORY_SHEET_NAME = ""
LOG_SHEET_NAME = ""
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff"}

# 入库表默认列（可在设置里改）
DEFAULT_INBOUND_COLS = {
    "date": "A",
    "cat1": "B",
    "cat2": "C",
    "cat3": "D",
    "type": "E",
    "clean_meta": "F",       # 是否清理元数据
    "reviewer": "G",         # 审核人员
    "uploader": "H",         # 上传者
    "copyright": "I",
    "file_url": "J",
    "status": "K",
    "done_date": "L",
}


def normalize_col(letter: str, fallback: str) -> str:
    text = str(letter or "").strip().upper()
    if not text:
        return fallback
    try:
        column_to_number(text)
        return text
    except Exception:
        return fallback


def build_inbound_values(cells: dict) -> list:
    """cells: {'A': v, 'F': v, ...} → 从 A 到最大列的连续列表。"""
    if not cells:
        return []
    max_n = 1
    mapped = {}
    for col, val in cells.items():
        c = normalize_col(col, "")
        if not c:
            continue
        n = column_to_number(c)
        mapped[n] = val
        max_n = max(max_n, n)
    return [mapped.get(i, "") for i in range(1, max_n + 1)]


def is_upload_success_status(status: str) -> bool:
    st = str(status or "")
    return st in ("成功", "已存在，跳过") or st.startswith("已存在")


def is_retryable_upload_error(exc) -> bool:
    """空间满/无权限等不可靠重试；网络抖动/5xx/限流可重试。"""
    msg = str(exc or "").lower()
    non_retry = (
        "storagequotaexceeded",
        "quota has been exceeded",
        "storage quota",
        "insufficient authentication scopes",
        "the caller does not have permission",
        "forbidden",
        "file not found",
        "notfound",
        "invalid",
    )
    if any(x in msg for x in non_retry):
        return False
    retry = (
        "connection",
        "timeout",
        "timed out",
        "aborted",
        "reset",
        "ssl",
        "503",
        "500",
        "502",
        "429",
        "rate limit",
        "ratelimit",
        "backend error",
        "internal error",
        "temporarily",
        "unavailable",
        "broken pipe",
        "max retries",
    )
    return any(x in msg for x in retry)


@dataclass
class UploadFileItem:
    path: str
    name: str
    status: str = "待上传"
    file_url: str = ""


@dataclass
class UploadTask:
    index: int
    is_image_mode: bool
    cat1: str = ""
    cat2: str = ""
    cat3: str = ""
    video_type: str = ""
    uploader: str = ""
    reviewer: str = ""
    clean_meta: str = ""  # 写入 F 列的文案，如「是」/「否」
    files: list[UploadFileItem] = field(default_factory=list)
    folder_url: str = ""
    status: str = "待处理"
    source_folder: str = ""


def app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def settings_path() -> str:
    return os.path.join(user_data_directory(), "upload_settings.json")


def task_queue_path() -> str:
    """上传任务清单持久化路径（重启可恢复失败项）。"""
    return os.path.join(user_data_directory(), "upload_task_queue.json")


def task_to_dict(task: UploadTask) -> dict:
    return {
        "index": task.index,
        "is_image_mode": bool(task.is_image_mode),
        "cat1": task.cat1,
        "cat2": task.cat2,
        "cat3": task.cat3,
        "video_type": task.video_type,
        "uploader": task.uploader,
        "reviewer": task.reviewer,
        "clean_meta": task.clean_meta,
        "folder_url": task.folder_url,
        "status": task.status,
        "source_folder": task.source_folder,
        "files": [
            {
                "path": f.path,
                "name": f.name,
                "status": f.status,
                "file_url": f.file_url,
            }
            for f in task.files
        ],
    }


def task_from_dict(data: dict) -> UploadTask | None:
    if not isinstance(data, dict):
        return None
    files = []
    for fd in data.get("files") or []:
        if not isinstance(fd, dict):
            continue
        path = str(fd.get("path") or "")
        name = str(fd.get("name") or os.path.basename(path) or "file")
        files.append(
            UploadFileItem(
                path=path,
                name=name,
                status=str(fd.get("status") or "待上传"),
                file_url=str(fd.get("file_url") or ""),
            )
        )
    if not files:
        return None
    return UploadTask(
        index=int(data.get("index") or 0),
        is_image_mode=bool(data.get("is_image_mode")),
        cat1=str(data.get("cat1") or ""),
        cat2=str(data.get("cat2") or ""),
        cat3=str(data.get("cat3") or ""),
        video_type=str(data.get("video_type") or ""),
        uploader=str(data.get("uploader") or ""),
        reviewer=str(data.get("reviewer") or ""),
        clean_meta=str(data.get("clean_meta") or ""),
        files=files,
        folder_url=str(data.get("folder_url") or ""),
        status=str(data.get("status") or "待处理"),
        source_folder=str(data.get("source_folder") or ""),
    )


def default_credentials_path() -> str:
    preferred = os.path.join(app_base_dir(), "谷歌服务账号.json")
    if os.path.exists(preferred):
        return preferred
    return os.path.join(app_base_dir(), "credentials.json")


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def list_files_recursive(folder: str) -> list[str]:
    out = []
    for root, _, files in os.walk(folder):
        for name in files:
            # 跳过隐藏/系统
            if name.startswith("."):
                continue
            out.append(os.path.join(root, name))
    return out


def match_path_to_category(rel_parts: list[str], category_rows: list[list[str]]) -> tuple[str, str, str, str]:
    """
    用相对路径段匹配表格分类。
    例：自拍素材/动物植物/xxx.mp4 -> cat1=自拍素材, cat2=动物植物
    优先最长前缀匹配 (c1,c2,c3)。
    """
    parts = [p for p in rel_parts if p and p not in (".", "..")]
    if not parts or not category_rows:
        return "", "", "", ""

    best = ("", "", "", "")
    best_score = -1
    for row in category_rows:
        c1 = str(row[0] if row else "").strip()
        c2 = str(row[1] if len(row) > 1 else "").strip()
        c3 = str(row[2] if len(row) > 2 else "").strip()
        c4 = str(row[3] if len(row) > 3 else "").strip()
        chain = [x for x in (c1, c2, c3) if x]
        if not chain:
            continue
        if len(parts) < len(chain):
            continue
        ok = True
        for i, name in enumerate(chain):
            if parts[i] != name:
                ok = False
                break
        if not ok:
            continue
        score = len(chain)
        # 若路径下一段正好等于 D 列类型，也算加分
        if c4 and len(parts) > len(chain) and parts[len(chain)] == c4:
            score += 0.5
        if score > best_score:
            best_score = score
            best = (c1, c2, c3, c4)
    return best


class DropZone(QFrame):
    """支持拖入文件/文件夹。"""
    paths_dropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadDropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = QLabel("将文件或「分类文件夹」拖到这里\n也可点下方按钮选择\n文件夹会按目录名自动匹配一/二/三级分类")
        self.hint.setObjectName("dropHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("QFrame#uploadDropZone { border-color: #38bdf8; }")
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p:
                paths.append(p)
        if paths:
            self.paths_dropped.emit(paths)
        event.acceptProposedAction()


class Card(QFrame):
    def __init__(self, title: str, object_name: str = "workCard"):
        super().__init__()
        self.setObjectName(object_name)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 14, 16, 14)
        self.layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        self.layout.addWidget(title_label)


class CategoryLoadWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    loaded = Signal(list)
    headers_loaded = Signal(list)
    columns_resolved = Signal(list)

    def __init__(self, credentials_path: str, token_path: str, spreadsheet_id: str, sheet_name: str, columns=None, start_row=2, inherit=True):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.columns = columns
        self.start_row = start_row
        self.inherit = inherit

    def run(self):
        try:
            client = GoogleClient(self.credentials_path, self.token_path)
            self.log.emit(f"已使用凭据：{client.account_label}（已授权则不会重复弹窗）")
            columns = list(self.columns or ["A", "B", "C", "D", "", ""])
            if len(columns) == 6 and self.start_row > 1 and (not columns[4] or not columns[5]):
                all_headers = client.read_header_row(self.spreadsheet_id, self.sheet_name, self.start_row - 1)
                for index, aliases in ((4, {"上传者", "上传人员", "上传人"}),
                                       (5, {"审核人员", "审核人", "审核者"})):
                    if not columns[index]:
                        matches = [number_to_column(i + 1) for i, h in enumerate(all_headers) if h.strip() in aliases]
                        if len(matches) == 1:
                            columns[index] = matches[0]
                        else:
                            self.log.emit(f"{'上传者' if index == 4 else '审核人员'}：未能唯一识别姓名列，请在读取列填写实际列字母。")
            self.columns = columns
            self.columns_resolved.emit(columns)
            rows = client.read_category_rows(self.spreadsheet_id, self.sheet_name, columns, self.start_row, self.inherit)
            headers = client.read_mapped_headers(self.spreadsheet_id, self.sheet_name,
                                                 self.columns or ["A", "B", "C", "D"],
                                                 self.start_row - 1)
            self.headers_loaded.emit(headers)
            self.loaded.emit(rows)
            self.log.emit(f"分类目录已加载 {len(rows)} 行。")
        except Exception as exc:
            self.failed.emit(f"加载分类失败：{exc}")


class InboundHeadersWorker(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, credentials, token, spreadsheet, sheet, header_row):
        super().__init__()
        self.args = (credentials, token, spreadsheet, sheet, header_row)

    def run(self):
        try:
            credentials, token, spreadsheet, sheet, header_row = self.args
            client = GoogleClient(credentials, token)
            self.loaded.emit(client.read_header_row(spreadsheet, sheet, header_row))
        except Exception as exc:
            self.failed.emit(f"读取入库表头失败：{exc}")


class UploadWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    item_update = Signal(int, int, str, str)
    task_update = Signal(int, str, str)
    progress = Signal(int, int)
    done = Signal()

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        spreadsheet_id: str,
        parent_folder_id: str,
        data_sheet: str,
        log_sheet: str,
        tasks: list[UploadTask],
        copyright_agreed: bool,
        skip_existing: bool,
        inbound_cols: dict | None = None,
        only_failed: bool = False,
        max_retries: int = 3,
        max_threads: int = 4,
        inbound_mappings=None,
        header_row=1,
        data_row=7,
        sync_headers=False,
    ):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.spreadsheet_id = spreadsheet_id
        self.parent_folder_id = parent_folder_id
        self.data_sheet = data_sheet
        self.log_sheet = log_sheet
        self.tasks = tasks
        self.copyright_agreed = copyright_agreed
        self.skip_existing = skip_existing
        self.only_failed = only_failed
        self.max_retries = max(1, int(max_retries or 1))
        self.max_threads = max(1, min(16, int(max_threads or 4)))
        cols = dict(DEFAULT_INBOUND_COLS)
        if inbound_cols:
            cols.update({k: normalize_col(v, cols.get(k, "A")) for k, v in inbound_cols.items() if k in cols})
        self.inbound_cols = cols
        self.inbound_mappings = validate_mappings(default_mappings(cols) if inbound_mappings is None else inbound_mappings)
        self.header_row = header_row
        self.data_row = data_row
        self.sync_headers = sync_headers
        self.stop_event = threading.Event()
        self._counter_lock = threading.Lock()
        self._existing_lock = threading.Lock()
        self._thread_local = threading.local()

    def _get_thread_client(self) -> GoogleClient:
        """后台线程客户端：禁止弹浏览器，且构造时走 token 全局锁。"""
        if not hasattr(self._thread_local, "client"):
            self._thread_local.client = GoogleClient(
                self.credentials_path,
                self.token_path,
                allow_browser=False,
            )
        return self._thread_local.client

    def stop(self):
        self.stop_event.set()

    def _upload_one_file(self, client: GoogleClient, folder_id: str, file_item: UploadFileItem, existing_map: dict | None = None):
        existing = None
        if existing_map is not None:
            with self._existing_lock:
                existing = existing_map.get(file_item.name)
        else:
            existing = client.find_file_in_folder(folder_id, file_item.name)

        if existing and self.skip_existing:
            return "skipped", existing.get("webViewLink") or ""
        if existing and not self.skip_existing:
            created = existing
        else:
            # 不把 existing_map 传给 upload（避免无锁写共享 dict）；上传成功后由外层登记
            created = client.upload_local_file(
                file_item.path,
                folder_id,
                file_name=file_item.name,
                mime_type=guess_mime(file_item.path),
                existing_map=None,
            )
            if existing_map is not None and created:
                with self._existing_lock:
                    existing_map[file_item.name] = created
        return "ok", created.get("webViewLink") or ""

    def run(self):
        import concurrent.futures

        success = failed = skipped = 0
        pending_files = []
        for t in self.tasks:
            for fi, f in enumerate(t.files):
                if self.only_failed and is_upload_success_status(f.status):
                    continue
                pending_files.append((t, fi, f))
        total_files = len(pending_files) if self.only_failed else sum(len(t.files) for t in self.tasks)
        if total_files == 0:
            self.log.emit("没有需要上传的文件（失败重试时若全成功会提示此项）。")
            self.done.emit()
            return
        done_files = 0
        try:
            client = GoogleClient(self.credentials_path, self.token_path)
            self.log.emit(
                f"已使用凭据：{client.account_label}；"
                f"{'仅失败重试' if self.only_failed else '全量上传'}；"
                f"并发线程数：{self.max_threads}；8MB 极速分块"
            )
            if not self.parent_folder_id.strip():
                raise RuntimeError("请填写 Google Drive 父文件夹 ID")
            if not self.spreadsheet_id.strip():
                raise RuntimeError("请填写表格 ID")

            if self.sync_headers:
                client.ensure_sheet(self.spreadsheet_id, self.data_sheet)
                client.write_row_fields(self.spreadsheet_id, self.data_sheet, self.header_row,
                                        {m['column']: m['header'] for m in self.inbound_mappings})

            for task in self.tasks:
                if self.stop_event.is_set():
                    self.log.emit("任务已停止。")
                    break

                task_files = list(enumerate(task.files))
                if self.only_failed:
                    task_files = [
                        (fi, f) for fi, f in task_files
                        if not is_upload_success_status(f.status)
                    ]
                    if not task_files:
                        continue

                self.task_update.emit(task.index, "上传中", "")
                date_str = datetime.now().strftime("%Y-%m-%d")
                if task.is_image_mode:
                    path_parts = ["图片素材", date_str, task.uploader]
                else:
                    path_parts = [task.cat1, task.cat2, task.cat3, date_str, task.uploader]

                folder_id = ""
                path_label = ""
                folder_ok = False
                for attempt in range(1, self.max_retries + 1):
                    if self.stop_event.is_set():
                        break
                    try:
                        folder_meta = client.get_or_create_folder_path(self.parent_folder_id, path_parts)
                        folder_id = folder_meta["id"]
                        folder_url = folder_meta.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"
                        task.folder_url = folder_url
                        path_label = "/".join([p for p in path_parts if p])
                        self.log.emit(f"任务#{task.index} 目标目录：{path_label}")
                        folder_ok = True
                        break
                    except Exception as exc:
                        if is_retryable_upload_error(exc) and attempt < self.max_retries:
                            self.log.emit(
                                f"任务#{task.index} 创建目录失败，{attempt}/{self.max_retries} 次，稍后重试：{exc}"
                            )
                            time.sleep(min(1.5 * attempt, 6.0))
                            continue
                        self.task_update.emit(task.index, f"失败：{exc}", "")
                        self.log.emit(f"任务#{task.index} 创建目录失败：{exc}")
                        with self._counter_lock:
                            failed += len(task_files)
                            done_files += len(task_files)
                            self.progress.emit(done_files, total_files)
                        break
                if not folder_ok:
                    continue

                # 一次性获取文件夹现有文件清单，做内存极速比对
                existing_map = {}
                if self.skip_existing:
                    try:
                        existing_map = client.list_folder_existing_files(folder_id)
                    except Exception as e:
                        self.log.emit(f"预读云端目录文件失败（将退回单文件查询）：{e}")
                        existing_map = {}

                def _process_one_file(item_tuple):
                    nonlocal success, failed, skipped, done_files
                    if self.stop_event.is_set():
                        return
                    fi, file_item = item_tuple
                    self.item_update.emit(task.index, fi, "上传中", "")
                    last_exc = None
                    uploaded = False
                    thread_client = self._get_thread_client()
                    for attempt in range(1, self.max_retries + 1):
                        if self.stop_event.is_set():
                            break
                        try:
                            kind, file_url = self._upload_one_file(thread_client, folder_id, file_item, existing_map)
                            # 云端上传成功后，入库/日志失败不重传文件，只记警告
                            try:
                                self._write_inbound(thread_client, task, file_url, file_item.name)
                            except Exception as sheet_exc:
                                self.log.emit(f"入库表写入失败（文件已上传）：{file_item.name} -> {sheet_exc}")
                            if kind == "skipped":
                                self.item_update.emit(task.index, fi, "已存在，跳过", file_url)
                                with self._counter_lock:
                                    skipped += 1
                            else:
                                try:
                                    thread_client.append_upload_log(
                                        self.spreadsheet_id, self.log_sheet, "INFO",
                                        f"文件 [{file_item.name}] 上传成功，路径: {path_label}",
                                    )
                                except Exception:
                                    pass
                                self.item_update.emit(task.index, fi, "成功", file_url)
                                with self._counter_lock:
                                    success += 1
                                self.log.emit(
                                    f"成功：{file_item.name}"
                                    + (f"（第 {attempt} 次）" if attempt > 1 else "")
                                )
                            uploaded = True
                            break
                        except Exception as exc:
                            last_exc = exc
                            if self.stop_event.is_set():
                                self.item_update.emit(task.index, fi, "已停止", "")
                                break
                            if is_retryable_upload_error(exc) and attempt < self.max_retries:
                                wait = min(1.5 * attempt, 8.0)
                                self.log.emit(
                                    f"失败重试 {attempt}/{self.max_retries}：{file_item.name} -> {exc}；{wait:.1f}s 后重试"
                                )
                                self.item_update.emit(
                                    task.index, fi, f"重试中 {attempt}/{self.max_retries}", ""
                                )
                                time.sleep(wait)
                                continue
                            break

                    if not uploaded and not self.stop_event.is_set():
                        with self._counter_lock:
                            failed += 1
                        msg = str(last_exc or "未知错误")
                        self.item_update.emit(task.index, fi, f"失败：{msg}", "")
                        self.log.emit(f"失败：{file_item.name} -> {msg}")

                    with self._counter_lock:
                        done_files += 1
                        self.progress.emit(done_files, total_files)

                # 并发传输：单文件失败不中断整批；线程异常必须 result() 捞出来记日志
                self.log.emit(f"任务#{task.index} 开始上传 {len(task_files)} 个文件…")
                if self.max_threads > 1 and len(task_files) > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                        futures = {
                            executor.submit(_process_one_file, item): item
                            for item in task_files
                        }
                        for future in concurrent.futures.as_completed(futures):
                            if self.stop_event.is_set():
                                # 不 cancel 其它任务，尽量让已在跑的传完；循环继续收割结果
                                pass
                            try:
                                future.result()
                            except Exception as exc:
                                item = futures.get(future)
                                name = ""
                                try:
                                    name = item[1].name if item else ""
                                except Exception:
                                    pass
                                with self._counter_lock:
                                    failed += 1
                                    done_files += 1
                                    self.progress.emit(done_files, total_files)
                                self.log.emit(f"线程异常（已继续后续文件）{name}: {exc}")
                else:
                    for item in task_files:
                        if self.stop_event.is_set():
                            self.log.emit("任务已停止（后续文件未传）。")
                            break
                        try:
                            _process_one_file(item)
                        except Exception as exc:
                            with self._counter_lock:
                                failed += 1
                                done_files += 1
                                self.progress.emit(done_files, total_files)
                            self.log.emit(f"单文件异常（已继续）：{exc}")

                if self.stop_event.is_set():
                    self.task_update.emit(task.index, "已停止", task.folder_url)
                    break
                self.task_update.emit(task.index, "完成", task.folder_url)
                self.log.emit(
                    f"任务#{task.index} 结束：累计成功 {success}，跳过 {skipped}，失败 {failed}。"
                )

            self.log.emit(
                f"全部上传结束：成功 {success}，跳过 {skipped}，失败 {failed}，"
                f"进度 {done_files}/{total_files}。"
            )
        except Exception as exc:
            self.log.emit(f"上传主流程异常（已尽量传完当前批次前的文件）：{exc}")
            self.failed.emit(str(exc))
        finally:
            self.done.emit()

    def _write_inbound(self, client: GoogleClient, task: UploadTask, file_url: str, file_name: str = ""):
        formatted = datetime.now().strftime("%Y-%m-%d")
        copyright_status = "保证版权没有问题" if self.copyright_agreed else "未勾选"
        if task.is_image_mode:
            display_cat1, display_cat2, display_cat3, display_type = "图片素材", "", "", "图片"
        else:
            display_cat1, display_cat2, display_cat3 = task.cat1, task.cat2 or "", task.cat3 or ""
            display_type = task.video_type or ""
        values = dict(date=formatted, cat1=display_cat1, cat2=display_cat2,
                      cat3=display_cat3, type=display_type, clean_meta=task.clean_meta,
                      reviewer=task.reviewer, uploader=task.uploader, copyright=copyright_status,
                      file_url=file_url, status="已上传", done_date=formatted,
                      file_name=file_name, folder_url=task.folder_url)
        cells = mapped_cells(self.inbound_mappings, values)
        if cells:
            client.insert_inbound_row(self.spreadsheet_id, self.data_sheet,
                                      build_inbound_values(cells), insert_before_row=self.data_row)



class DriveBatchUploadPage(QWidget):
    def __init__(
        self,
        parent=None,
        credentials_supplier=None,
        spreadsheet_supplier=None,  # 保留参数兼容旧调用；上传表独立，不再回落
        token_path: str = "",
    ):
        super().__init__(parent)
        self.credentials_supplier = credentials_supplier
        self.token_path = token_path or default_token_path()
        self.worker = None
        self.category_rows: list[list[str]] = []
        self.tasks: list[UploadTask] = []
        self.pending_files: list[str] = []
        self.is_image_mode = False
        self.task_seq = 0
        self.settings_collapsed = False
        self.build_ui()
        self.connect_signals()
        self.load_saved_settings()
        self.load_task_queue()
        if self.spreadsheet_edit.text().strip():
            self.config_summary.setText(f"{self.data_sheet_edit.text()} · {self.category_sheet_edit.text()} · 列映射已配置")

    def credentials_path(self) -> str:
        if callable(self.credentials_supplier):
            try:
                value = str(self.credentials_supplier() or "").strip()
                if value:
                    return value
            except Exception:
                pass
        return default_credentials_path()

    def spreadsheet_id(self) -> str:
        """上传专用表格 ID，用于入库、分类和日志。"""
        return self.spreadsheet_edit.text().strip()

    @staticmethod
    def _solid_bg(widget: QWidget, color: str = "#0b1120"):
        """Solid fill so leftover white Base never shows through."""
        if widget is None:
            return
        c = QColor(color)
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        widget.setAutoFillBackground(True)
        pal = widget.palette()
        pal.setColor(QPalette.ColorRole.Window, c)
        pal.setColor(QPalette.ColorRole.Base, c)
        widget.setPalette(pal)

    def apply_page_fill(self, bg: str = "#0b1120"):
        """Called from main window theme switch; keeps left column free of white bands."""
        self._solid_bg(self, bg)
        scroll = getattr(self, "_left_scroll", None)
        inner = getattr(self, "_left_inner", None)
        if scroll is not None:
            scroll.setStyleSheet("")  # drop any transparent override
            self._solid_bg(scroll, bg)
            vp = scroll.viewport()
            if vp is not None:
                vp.setStyleSheet("")
                self._solid_bg(vp, bg)
        if inner is not None:
            self._solid_bg(inner, bg)

    def build_ui(self):
        self.setObjectName("pageFill")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._solid_bg(self, "#0b1120")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        tip = QLabel(
            "凭据请在顶部「⚙ 全局设置」配置。本页表格 ID 独立（成品入库表 / 分类目录 / 日志）。\n"
            "入库默认：F=是否清理元数据，G=审核人员，H=上传者（列与表名均可改）。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        root.addWidget(tip)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        # ----- 左侧（加宽，避免设置项挤在一行显示不全）-----
        # 不用 transparent：Windows 上 QScrollArea viewport 默认 Base=白，会透出白底
        left_scroll = QScrollArea()
        left_scroll.setObjectName("pageFill")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_scroll.setMinimumWidth(300)
        left_scroll.setMaximumWidth(390)
        left_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        left_inner = QWidget()
        left_inner.setObjectName("scrollInner")
        left_inner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        left_inner.setMinimumWidth(280)
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 8, 0)
        left.setSpacing(10)
        left_scroll.setWidget(left_inner)
        body.addWidget(left_scroll, 3)  # 左侧占更多宽度比例
        self._left_scroll = left_scroll
        self._left_inner = left_inner
        self.apply_page_fill("#0b1120")

        # 可折叠设置
        self.settings_toggle = QToolButton()
        self.settings_toggle.setObjectName("collapseBtn")
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setChecked(True)
        self.settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_toggle.setText("▾  设置（点此折叠/展开）")
        self.settings_toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        left.addWidget(self.settings_toggle)

        self.settings_body = QFrame()
        self.settings_body.setObjectName("settingsCard")
        sg = QVBoxLayout(self.settings_body)
        sg.setContentsMargins(14, 12, 14, 12)
        sg.setSpacing(8)
        left.addWidget(self.settings_body)

        self.credentials_label = QLabel("")
        self.credentials_label.setObjectName("subtitle")
        self.credentials_label.setWordWrap(True)
        sg.addWidget(self.credentials_label)

        self.spreadsheet_edit = QLineEdit()
        self.spreadsheet_edit.setPlaceholderText("上传专用表格 ID（成品入库表 / 分类目录所在表格，必填）")
        self.parent_folder_edit = QLineEdit(DEFAULT_PARENT_FOLDER_ID)
        self.data_sheet_edit = QLineEdit(DATA_SHEET_NAME)
        self.category_sheet_edit = QLineEdit(CATEGORY_SHEET_NAME)
        self.log_sheet_edit = QLineEdit(LOG_SHEET_NAME)
        self.uploader_edit = QComboBox()
        self.uploader_edit.setEditable(True)
        self.uploader_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.uploader_edit.lineEdit().setPlaceholderText("选择或输入上传者姓名 *")
        self.reviewer_edit = QComboBox()
        self.reviewer_edit.setEditable(True)
        self.reviewer_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.reviewer_edit.lineEdit().setPlaceholderText("选择或输入审核人员")

        for label, widget in [
            ("上传专用表格 ID *", self.spreadsheet_edit),
            ("Drive 父文件夹 ID", self.parent_folder_edit),
            ("成品入库表名称（工作表名，可改）", self.data_sheet_edit),
            ("分类目录名称（工作表名，可改）", self.category_sheet_edit),
            ("成品入库表日志名称（工作表名，可改）", self.log_sheet_edit),
        ]:
            sg.addWidget(self._labeled(label, widget))

        self.mapping_toggle = QToolButton()
        self.mapping_toggle.setObjectName("collapseBtn")
        self.mapping_toggle.setCheckable(True)
        self.mapping_toggle.setChecked(True)
        self.mapping_toggle.setText("▾  入库列映射（点此折叠）")
        self.mapping_toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sg.addWidget(self.mapping_toggle)
        self.mapping_body = QFrame()
        mapping_layout = QVBoxLayout(self.mapping_body)
        mapping_layout.setContentsMargins(0, 0, 0, 0)
        sg.addWidget(self.mapping_body)
        self.mapping_toggle.toggled.connect(self._on_mapping_toggled)
        mapping_layout.addWidget(QLabel("写入目标：入库工作表（双击单元格编辑，分类表不影响这里）"))
        self.load_inbound_headers_btn = QPushButton("读取整行表头（按表格顺序生成映射）")
        self.load_inbound_headers_btn.clicked.connect(self.load_inbound_headers)
        mapping_layout.addWidget(self.load_inbound_headers_btn)
        self.mapping_editor = MappingEditor()
        mapping_layout.addWidget(self.mapping_editor)
        self.inbound_header_hint = QLabel("首次读取整行表头 → 校对名称和目标列 → 保存。之后按保存的列回填。")
        self.inbound_header_hint.setWordWrap(True)
        mapping_layout.addWidget(self.inbound_header_hint)
        self.header_row_spin = QSpinBox()
        self.header_row_spin.setRange(1, 1000000)
        self.data_row_spin = QSpinBox()
        self.data_row_spin.setRange(2, 1000000)
        self.data_row_spin.setValue(7)
        mapping_layout.addWidget(self._labeled("入库表表头行", self.header_row_spin))
        mapping_layout.addWidget(self._labeled("插入数据行（在该行前插入）", self.data_row_spin))
        self.sync_headers_check = QCheckBox("上传时将映射中的表头名称写入表格（仅指定列）")
        mapping_layout.addWidget(self.sync_headers_check)
        sg.addWidget(QLabel("分类和人员读取列（名称自动读取表头；列留空不读取）"))
        self.category_mapping = QTableWidget(6, 3)
        self.category_mapping.setHorizontalHeaderLabels(["下拉字段", "分类表读取列", "写入入库表的哪一列"])
        self.category_mapping.setVerticalHeaderLabels(["分类1", "分类2", "分类3", "类型", "人员", "人员"])
        self.category_mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.category_mapping.setFixedHeight(220)
        for i, label in enumerate(("一级分类", "二级分类", "三级分类", "类型", "上传者", "审核人员")):
            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.category_mapping.setItem(i, 0, item)
            self.category_mapping.setItem(i, 1, QTableWidgetItem("ABCD"[i] if i < 4 else ""))
        self.category_targets = []
        for i in range(6):
            target = QComboBox()
            target.addItem("不写入", "")
            target.activated.connect(lambda index, row=i: self.assign_category_target(row))
            self.category_targets.append(target)
            self.category_mapping.setCellWidget(i, 2, target)
        sg.addWidget(self.category_mapping)
        refresh_targets = QPushButton("更新可选写入目标（使用上方入库列映射）")
        refresh_targets.clicked.connect(self.refresh_category_targets)
        sg.addWidget(refresh_targets)
        people_tip = QLabel("每行左边指定姓名/分类从哪列读，右边选择写入目标。人员读取列留空时按“上传者/审核人员”表头自动识别；也可手填列字母。")
        people_tip.setWordWrap(True)
        sg.addWidget(people_tip)
        self.category_start_spin = QSpinBox()
        self.category_start_spin.setRange(1, 1000000)
        self.category_start_spin.setValue(2)
        sg.addWidget(self._labeled("分类/人员读取起始行（上一行为表头）", self.category_start_spin))
        self.category_inherit_check = QCheckBox("空白一级/二级沿用上一行（适用于合并单元格）")
        self.category_inherit_check.setChecked(True)
        sg.addWidget(self.category_inherit_check)

        self.copyright_check = QCheckBox("我保证版权没有问题")
        self.copyright_check.setChecked(True)
        self.clean_meta_check = QCheckBox("是否清理元数据（按列映射回填）")
        self.clean_meta_check.setChecked(False)
        self.skip_existing_check = QCheckBox("云端同名则跳过（仍写入成品入库表）")
        self.skip_existing_check.setChecked(True)
        sg.addWidget(self.copyright_check)
        sg.addWidget(self.clean_meta_check)
        sg.addWidget(self.skip_existing_check)

        set_btns = QHBoxLayout()
        self.save_settings_btn = QPushButton("保存设置并折叠")
        self.save_settings_btn.setObjectName("primaryButton")
        self.load_cat_btn = QPushButton("从分类工作表读取分类与人员")
        self.load_cat_btn.setObjectName("secondaryButton")
        self.sync_cred_btn = QPushButton("刷新凭据")
        self.sync_cred_btn.setObjectName("ghostButton")
        set_btns.addWidget(self.save_settings_btn)
        set_btns.addWidget(self.load_cat_btn)
        set_btns.addWidget(self.sync_cred_btn)
        sg.addLayout(set_btns)

        # 日常操作区（常驻显示，设置折叠后依然可见）
        work = Card("常规操作 / 上传设置", "workCard")
        left.addWidget(work)

        # 上传者与审核人员常驻在常驻操作区
        user_row = QVBoxLayout()
        user_row.setSpacing(10)
        user_row.addWidget(compact_field("上传者 *", self.uploader_edit))
        user_row.addWidget(compact_field("审核人员", self.reviewer_edit))
        work.layout.addLayout(user_row)

        mode_row = QHBoxLayout()
        self.mode_standard_btn = QPushButton("分类上传")
        self.mode_standard_btn.setObjectName("primaryButton")
        self.mode_image_btn = QPushButton("图片直传")
        self.mode_image_btn.setObjectName("secondaryButton")
        mode_row.addWidget(self.mode_standard_btn)
        mode_row.addWidget(self.mode_image_btn)
        work.layout.addLayout(mode_row)

        self.cat_panel = QFrame()
        cat_box = QVBoxLayout(self.cat_panel)
        cat_box.setContentsMargins(0, 0, 0, 0)
        cat_box.setSpacing(6)
        work.layout.addWidget(self.cat_panel)

        self.cat1_combo = QComboBox()
        self.cat2_combo = QComboBox()
        self.cat3_combo = QComboBox()
        self.video_type_combo = QComboBox()
        self.video_type_combo.setEditable(True)
        self.video_type_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for c in (self.cat1_combo, self.cat2_combo, self.cat3_combo, self.video_type_combo):
            c.clear()
            c.addItem("")
        self.category_labels = []
        for label, w in [
            ("一级分类（可留空）", self.cat1_combo),
            ("二级分类（可独立选择）", self.cat2_combo),
            ("三级分类（可独立选择）", self.cat3_combo),
            ("类型（可独立选择）", self.video_type_combo),
        ]:
            field = compact_field(label.split("（")[0], w)
            self.category_labels.append(field.findChild(QLabel))
            cat_box.addWidget(field)

        self.drop_zone = DropZone()
        work.layout.addWidget(self.drop_zone)

        file_btns = QHBoxLayout()
        self.pick_files_btn = QPushButton("选文件")
        self.pick_files_btn.setObjectName("secondaryButton")
        self.pick_folder_btn = QPushButton("选分类文件夹")
        self.pick_folder_btn.setObjectName("secondaryButton")
        self.pick_folder_btn.setToolTip("选择已按「一级/二级/三级」建好的本地文件夹，自动匹配分类并拆成任务")
        file_btns.addWidget(self.pick_files_btn)
        file_btns.addWidget(self.pick_folder_btn)
        work.layout.addLayout(file_btns)

        self.pending_label = QLabel("待添加：0 个文件")
        self.pending_label.setObjectName("status")
        work.layout.addWidget(self.pending_label)

        self.add_task_btn = QPushButton("加入清单 →")
        self.add_task_btn.setObjectName("primaryButton")
        self.start_btn = QPushButton("开始批量上传")
        self.start_btn.setObjectName("primaryButton")
        self.retry_failed_btn = QPushButton("重试失败项")
        self.retry_failed_btn.setObjectName("secondaryButton")
        self.retry_failed_btn.setToolTip(
            "只重新上传失败/未完成文件。清单会保存到本机，下次启动软件仍可继续重试。"
        )
        self.clear_done_btn = QPushButton("清除已完成")
        self.clear_done_btn.setObjectName("secondaryButton")
        self.clear_done_btn.setToolTip("从清单移除已成功/已跳过的文件，只保留失败与待上传项，便于下次继续。")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.clear_tasks_btn = QPushButton("清空清单")
        self.clear_tasks_btn.setObjectName("ghostButton")
        for b in (
            self.add_task_btn,
            self.start_btn,
            self.retry_failed_btn,
            self.clear_done_btn,
            self.stop_btn,
            self.clear_tasks_btn,
        ):
            work.layout.addWidget(b)


        # ----- 右侧 -----
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 7)  # 右侧任务区更大

        task_card = Card("任务列表")
        right.addWidget(task_card, 3)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "模式", "分类", "文件", "状态", "文件链接", "文件夹"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for i, w in enumerate([36, 72, 150, 160, 100, 160]):
            self.table.setColumnWidth(i, w)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        task_card.layout.addWidget(self.table)

        prog_card = Card("上传进度")
        right.addWidget(prog_card, 0)
        self.progress_label = QLabel("等待开始")
        self.progress_label.setObjectName("status")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        prog_card.layout.addWidget(self.progress_label)
        prog_card.layout.addWidget(self.progress_bar)

        receipt_card = Card("回执链接")
        right.addWidget(receipt_card, 2)
        self.receipt_box = QTextEdit()
        self.receipt_box.setReadOnly(True)
        self.receipt_box.setObjectName("pasteTextBox")
        self.receipt_box.setPlaceholderText("上传成功后显示文件夹/文件链接，可复制。")
        receipt_card.layout.addWidget(self.receipt_box)
        rb = QHBoxLayout()
        self.copy_receipt_btn = QPushButton("复制全部回执")
        self.copy_receipt_btn.setObjectName("secondaryButton")
        self.clear_receipt_btn = QPushButton("清空回执")
        self.clear_receipt_btn.setObjectName("ghostButton")
        self.open_folder_btn = QPushButton("打开父文件夹")
        self.open_folder_btn.setObjectName("secondaryButton")
        rb.addWidget(self.copy_receipt_btn)
        rb.addWidget(self.clear_receipt_btn)
        rb.addWidget(self.open_folder_btn)
        rb.addStretch()
        receipt_card.layout.addLayout(rb)

        log_card = Card("日志")
        right.addWidget(log_card, 1)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        log_card.layout.addWidget(self.log_box)

        # Configuration lives in a reusable modal; widget identity and saved values are retained.
        self.config_dialog = QDialog(self)
        self.config_dialog.setWindowTitle("表格与列映射设置")
        self.config_dialog.resize(760, 720)
        dialog_layout = QVBoxLayout(self.config_dialog)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        left.removeWidget(self.settings_body)
        settings_scroll.setWidget(self.settings_body)
        dialog_layout.addWidget(settings_scroll)
        close_config = QPushButton("返回主页面")
        close_config.clicked.connect(self.config_dialog.reject)
        dialog_layout.addWidget(close_config)
        left.removeWidget(self.settings_toggle)
        self.settings_toggle.hide()
        self.config_button = QPushButton("⚙ 表格与列映射设置")
        self.config_button.setObjectName("secondaryButton")
        self.config_button.clicked.connect(self.open_config_dialog)
        top = QHBoxLayout()
        self.config_summary = QLabel("表格与列映射 · 点击设置")
        self.config_summary.setWordWrap(True)
        top.addWidget(self.config_summary, 1)
        top.addWidget(self.config_button)
        root.insertLayout(0, top)
        tip.hide()
        # Give personnel and categories their own compact visual groups.
        work.layout.itemAt(0).widget().setText("人员")
        people = QFrame()
        people.setObjectName("peoplePanel")
        work.layout.removeItem(user_row)
        people.setLayout(user_row)
        work.layout.insertWidget(1, people)
        people.setStyleSheet("QFrame#peoplePanel { background: rgba(70,130,230,24); border-radius: 8px; }")
        self.cat_panel.setObjectName("categoryPanel")
        self.cat_panel.setStyleSheet("QFrame#categoryPanel { background: rgba(40,170,130,16); border-radius: 6px; }")
        cat_box.insertWidget(0, QLabel("分类 · 各级可独立选择"))
        self.drop_zone.hint.setText("拖拽文件或文件夹到这里\n也可点击选择")
        icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp).pixmap(32, 32))
        self.drop_zone.layout().insertWidget(0, icon)
        self.drop_zone.setCursor(Qt.CursorShape.PointingHandCursor)
        self.drop_zone.clicked.connect(self.pick_files)
        self.pick_files_btn.setText("选择文件")
        self.pick_folder_btn.setText("选择文件夹")
        self.add_task_btn.setText("加入上传队列")
        for button in (self.start_btn, self.stop_btn, self.retry_failed_btn, self.clear_done_btn, self.clear_tasks_btn):
            work.layout.removeWidget(button)
        controls = QHBoxLayout()
        controls.addWidget(self.start_btn, 1)
        controls.addWidget(self.stop_btn)
        task_card.layout.insertLayout(1, controls)
        tools_row = QHBoxLayout()
        for button in (self.retry_failed_btn, self.clear_done_btn, self.clear_tasks_btn):
            tools_row.addWidget(button)
        task_card.layout.addLayout(tools_row)
        right.removeWidget(prog_card)
        prog_card.layout.removeWidget(self.progress_label)
        prog_card.layout.removeWidget(self.progress_bar)
        task_card.layout.insertWidget(2, self.progress_label)
        task_card.layout.insertWidget(3, self.progress_bar)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        prog_card.hide()
        self._unused_progress_card = prog_card
        right.removeWidget(receipt_card)
        right.removeWidget(log_card)
        # Receipts are already available in the table; keep the backing widget for compatibility.
        receipt_card.hide()
        self._receipt_card = receipt_card
        self.log_toggle = drawer("运行日志", log_card, left, 16777215)
        self._upload_log_card = log_card
        self._upload_work_card = work
        self._upload_left_layout = left
        work.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        log_card.setMinimumHeight(120)
        log_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_box.setMinimumHeight(60)
        left.setStretchFactor(log_card, 1)
        left.addStretch(0)
        self._log_spacer_index = left.count() - 1
        self.log_toggle.toggled.connect(self._log_visibility_changed)
        self._auto_log_expanded = None
        self._left_scroll.viewport().installEventFilter(self)
        QTimer.singleShot(0, self._adapt_upload_log)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellClicked.connect(self.copy_table_link)
        right.setStretchFactor(task_card, 1)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setColumnHidden(1, True)
        header.setStretchLastSection(False)
        for col in (2, 3, 5, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 35)
        self.table.setColumnWidth(4, 100)
        self.table.verticalHeader().setDefaultSectionSize(32)

        work.layout.setContentsMargins(10, 8, 10, 8)
        work.layout.setSpacing(6)
        self.drop_zone.setMinimumHeight(90)
        icon.setText("⇧")
        icon.setStyleSheet("color: #5489ff; font-size: 30px;")
        for control in (self.uploader_edit, self.reviewer_edit, self.cat1_combo,
                        self.cat2_combo, self.cat3_combo, self.video_type_combo):
            control.setStyleSheet("QComboBox, QSpinBox, QLineEdit { min-height: 22px; max-height: 26px; padding: 3px 6px; } QComboBox { combobox-popup: 0; }")
        for button in (self.mode_standard_btn, self.mode_image_btn, self.pick_files_btn,
                       self.pick_folder_btn, self.add_task_btn):
            button.setStyleSheet("min-height: 22px; max-height: 28px; padding: 4px 6px;")
        self.progress_bar.setStyleSheet("min-height: 6px; max-height: 6px; border: none;")

        self.status_row = QLabel("点击顶部配置表格；日常选择人员、分类，再添加文件或文件夹")
        self.status_row.setObjectName("status")
        root.addWidget(self.status_row)

        self.refresh_credentials_label()
        self.log("上传页就绪。设置保存到本机；分类来自表格；支持拖拽文件夹自动匹配。")

    def eventFilter(self, watched, event):
        if hasattr(self, "_left_scroll") and watched is self._left_scroll.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._adapt_upload_log)
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._adapt_upload_log)

    def _log_visibility_changed(self, expanded):
        self._upload_left_layout.setStretch(self._log_spacer_index, 0 if expanded else 1)

    def _adapt_upload_log(self):
        if not hasattr(self, "_upload_log_card"):
            return
        layout = self._upload_left_layout
        margins = layout.contentsMargins()
        needed = (self._upload_work_card.sizeHint().height() + self.log_toggle.sizeHint().height()
                  + margins.top() + margins.bottom() + layout.spacing() * 3)
        expanded = self._left_scroll.viewport().height() - needed >= 140
        if expanded != self._auto_log_expanded:
            self._auto_log_expanded = expanded
            self.log_toggle.setChecked(expanded)
        self._log_visibility_changed(self.log_toggle.isChecked())

    def copy_table_link(self, row, column):
        if column not in (5, 6):
            return
        item = self.table.item(row, column)
        if item and item.text().strip():
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(item.text())
            self.status_row.setText("文件链接已复制" if column == 5 else "文件夹链接已复制")

    def _labeled(self, label: str, widget: QWidget) -> QFrame:
        field = QFrame()
        field.setObjectName("fieldBox")
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        caption.setWordWrap(True)
        widget.setMinimumWidth(200)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        box.addWidget(caption)
        box.addWidget(widget)
        return field

    def connect_signals(self):
        self.settings_toggle.toggled.connect(self._on_settings_toggled)
        self.save_settings_btn.clicked.connect(self.save_settings_and_collapse)
        self.load_cat_btn.clicked.connect(self.load_categories)
        self.sync_cred_btn.clicked.connect(self.refresh_credentials_label)
        self.mode_standard_btn.clicked.connect(lambda: self.set_image_mode(False))
        self.mode_image_btn.clicked.connect(lambda: self.set_image_mode(True))
        self.drop_zone.paths_dropped.connect(self.on_paths_dropped)
        self.pick_files_btn.clicked.connect(self.pick_files)
        self.pick_folder_btn.clicked.connect(self.pick_category_folder)
        self.add_task_btn.clicked.connect(self.add_task)
        self.start_btn.clicked.connect(self.start_upload)
        self.retry_failed_btn.clicked.connect(self.retry_failed_uploads)
        self.clear_done_btn.clicked.connect(self.clear_completed_tasks)
        self.stop_btn.clicked.connect(self.stop_upload)
        self.clear_tasks_btn.clicked.connect(self.clear_tasks)
        self.copy_receipt_btn.clicked.connect(self.copy_receipts)
        self.clear_receipt_btn.clicked.connect(self.receipt_box.clear)
        self.open_folder_btn.clicked.connect(self.open_parent_folder)

    def open_config_dialog(self):
        self.refresh_category_targets()
        self.settings_toggle.setChecked(True)
        self.settings_body.show()
        self.config_dialog.exec()

    def _on_settings_toggled(self, expanded: bool):
        self.settings_body.setVisible(expanded)
        self.settings_toggle.setText("▾  设置（点此折叠）" if expanded else "▸  设置（已折叠，点此展开）")
        self.settings_collapsed = not expanded

    def _on_mapping_toggled(self, expanded):
        self.mapping_body.setVisible(expanded)
        self.mapping_toggle.setText("▾  入库列映射（点此折叠）" if expanded
                                    else "▸  入库列映射（已折叠，点此展开）")

    def refresh_category_labels(self):
        for i, label in enumerate(self.category_labels):
            label.setText((self.category_mapping.item(i, 0).text().strip() or "分类"))

    def refresh_category_targets(self):
        sources = ("cat1", "cat2", "cat3", "type", "uploader", "reviewer")
        mappings = self.mapping_editor.rows()
        for i, combo in enumerate(self.category_targets):
            selected = next((str(r["column"]).strip().upper() for r in mappings
                             if r["source"] == sources[i] and str(r["column"]).strip()), "")
            combo.blockSignals(True)
            combo.clear(); combo.addItem("不写入", "")
            for r in mappings:
                col = str(r["column"]).strip().upper()
                if col:
                    combo.addItem(f"{r['header']} → {col} 列", col)
            combo.setCurrentIndex(max(0, combo.findData(selected)))
            combo.blockSignals(False)
        for i, control in enumerate((self.cat1_combo, self.cat2_combo, self.cat3_combo,
                                     self.video_type_combo, self.uploader_edit, self.reviewer_edit)):
            control.setToolTip("写入目标：" + self.category_targets[i].currentText())

    def assign_category_target(self, row):
        sources = ("cat1", "cat2", "cat3", "type", "uploader", "reviewer")
        source = sources[row]
        column = self.category_targets[row].currentData()
        mappings = self.mapping_editor.rows()
        target = next((m for m in mappings if m["column"].strip().upper() == column), None) if column else None
        if column and target is None:
            self.refresh_category_targets()
            return
        if target and target["source"] in sources and target["source"] != source:
            QMessageBox.warning(self, APP_SECTION, "该目标已分配给另一个下拉字段。请先将另一项改为不写入，避免互相覆盖。")
            self.refresh_category_targets()
            return
        for mapping in mappings:
            if mapping["source"] == source:
                mapping["source"] = "unassigned"
        if target is not None:
            target["source"] = source
            target["value"] = ""
        self.mapping_editor.set_rows(mappings)
        self.refresh_category_targets()

    def on_category_columns_resolved(self, columns):
        for i, column in enumerate(columns[:6]):
            self.category_mapping.item(i, 1).setText(column)

    def category_mapping_from_ui(self):
        rows = [dict(header=self.category_mapping.item(i, 0).text(),
                     column=self.category_mapping.item(i, 1).text(), source="custom", value="")
                for i in range(6)]
        for row in rows:
            validate_mappings([row])
        return rows

    def validate_settings(self):
        validate_mappings(self.mapping_editor.rows())
        self.category_mapping_from_ui()
        if self.data_row_spin.value() <= self.header_row_spin.value():
            raise ValueError("插入数据行必须位于表头行之后")

    def clean_meta_text(self) -> str:
        return "是" if self.clean_meta_check.isChecked() else "否"

    def save_settings_and_collapse(self):
        try:
            self.validate_settings()
        except ValueError as exc:
            QMessageBox.warning(self, APP_SECTION, str(exc))
            return
        data = {
            "spreadsheet_id": self.spreadsheet_edit.text().strip(),
            "parent_folder_id": self.parent_folder_edit.text().strip(),
            "data_sheet": self.data_sheet_edit.text().strip(),
            "category_sheet": self.category_sheet_edit.text().strip(),
            "log_sheet": self.log_sheet_edit.text().strip(),
            "uploader": self.uploader_edit.currentText().strip(),
            "reviewer": self.reviewer_edit.currentText().strip(),
            "clean_meta": self.clean_meta_check.isChecked(),
            "copyright": self.copyright_check.isChecked(),
            "skip_existing": self.skip_existing_check.isChecked(),
            "inbound_mappings": self.mapping_editor.rows(),
            "header_row": self.header_row_spin.value(),
            "data_row": self.data_row_spin.value(),
            "sync_headers": self.sync_headers_check.isChecked(),
            "category_mapping": self.category_mapping_from_ui(),
            "category_start_row": self.category_start_spin.value(),
            "category_inherit": self.category_inherit_check.isChecked(),
            "collapsed": True,
            "mapping_collapsed": True,
        }
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.log(f"设置已保存：{settings_path()}")
        except Exception as exc:
            QMessageBox.warning(self, APP_SECTION, f"保存失败：{exc}")
            return
        self.refresh_category_labels()
        self.mapping_toggle.setChecked(False)
        self.settings_toggle.setChecked(False)
        self.config_summary.setText(f"{self.data_sheet_edit.text()} · {self.category_sheet_edit.text()} · 列映射已配置")
        self.config_dialog.accept()
        self.status_row.setText("设置已保存 · 可选择分类、人员和上传文件")

    def load_saved_settings(self):
        path = settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        self.spreadsheet_edit.setText(data.get("spreadsheet_id", "") or self.spreadsheet_edit.text())
        if data.get("parent_folder_id"):
            self.parent_folder_edit.setText(data["parent_folder_id"])
        if data.get("data_sheet"):
            self.data_sheet_edit.setText(data["data_sheet"])
        if data.get("category_sheet"):
            self.category_sheet_edit.setText(data["category_sheet"])
        if data.get("log_sheet"):
            self.log_sheet_edit.setText(data["log_sheet"])
        if data.get("uploader"):
            self.uploader_edit.setCurrentText(data["uploader"])
        if data.get("reviewer"):
            self.reviewer_edit.setCurrentText(data["reviewer"])
        self.clean_meta_check.setChecked(bool(data.get("clean_meta", False)))
        self.copyright_check.setChecked(bool(data.get("copyright", True)))
        self.skip_existing_check.setChecked(bool(data.get("skip_existing", True)))
        self.mapping_editor.set_rows(data.get("inbound_mappings", default_mappings(data["inbound_cols"]) if data.get("inbound_cols") else []))
        self.header_row_spin.setValue(int(data.get("header_row", 1)))
        self.data_row_spin.setValue(int(data.get("data_row", 7)))
        self.sync_headers_check.setChecked(bool(data.get("sync_headers", False)))
        for i, row in enumerate(data.get("category_mapping", [])[:6]):
            self.category_mapping.item(i, 0).setText(row.get("header", ""))
            self.category_mapping.item(i, 1).setText(row.get("column", ""))
        self.category_start_spin.setValue(int(data.get("category_start_row", 2)))
        self.category_inherit_check.setChecked(bool(data.get("category_inherit", True)))
        self.mapping_toggle.setChecked(not data.get("mapping_collapsed", True))
        if data.get("collapsed"):
            self.settings_toggle.setChecked(False)
        self.refresh_category_labels()
        self.refresh_category_targets()
        self.log("已恢复本机上传设置。")

    def refresh_credentials_label(self):
        path = self.credentials_path()
        token = self.token_path
        token_ok = os.path.exists(token)
        self.credentials_label.setText(
            f"凭据（来自顶部「全局设置」）：{path or '未设置'}\n"
            f"授权缓存：{token}"
            + ("（已有，一般无需重新登录）" if token_ok else "（首次使用会打开浏览器授权一次）")
        )

    def log(self, message: str):
        self.log_box.append(f"[{time.strftime('%H:%M:%S')}] {message}")

    def set_image_mode(self, enabled: bool):
        self.is_image_mode = enabled
        if enabled:
            self.mode_image_btn.setObjectName("primaryButton")
            self.mode_standard_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(False)
        else:
            self.mode_standard_btn.setObjectName("primaryButton")
            self.mode_image_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(True)
        parent = self.window()
        if parent and hasattr(parent, "apply_style"):
            parent.apply_style()
        else:
            for b in (self.mode_image_btn, self.mode_standard_btn):
                b.style().unpolish(b)
                b.style().polish(b)

    def has_running_worker(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def load_inbound_headers(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "请等待当前读取或上传任务结束。")
            return
        sid = self.spreadsheet_id()
        sheet = self.data_sheet_edit.text().strip()
        if not sid or not sheet:
            QMessageBox.warning(self, APP_SECTION, "请先填写上传专用表格 ID 和入库工作表名称。")
            return
        rows = self.mapping_editor.rows()
        if rows:
            answer = QMessageBox.question(self, APP_SECTION,
                "重新读取会按当前入库表的表头顺序重建映射，替换尚未保存的修改。继续读取？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.mapping_editor.setEnabled(False)
        self.load_inbound_headers_btn.setEnabled(False)
        self.load_cat_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        worker = InboundHeadersWorker(self.credentials_path(), self.token_path, sid, sheet,
                                      self.header_row_spin.value())
        context = (sid, sheet, self.header_row_spin.value())
        def apply(headers):
            if context != (self.spreadsheet_id(), self.data_sheet_edit.text().strip(), self.header_row_spin.value()):
                self.log("入库表配置已改变，忽略旧表头结果，请重新读取。")
                return
            new_rows = mappings_from_headers(headers, rows)
            if not new_rows:
                self.log("所选表头行没有非空表头，保留当前映射，请检查工作表名称和表头行。")
                self.inbound_header_hint.setText("未读到表头，当前映射未改变。请检查表头行。")
                return
            self.mapping_editor.set_rows(new_rows)
            self.refresh_category_targets()
            self.mapping_toggle.setChecked(True)
            unknown = sum(r["source"] == "unassigned" for r in new_rows)
            message = f"已按表格顺序读取 {len(new_rows)} 项，可修改名称和目标列，校对后保存。"
            if unknown:
                message += f" {unknown} 项未识别回填内容，暂不回填；需要填写时可右键指定。"
            self.inbound_header_hint.setText(message)
            self.log(f"入库工作表「{sheet}」第 {context[2]} 行：" + message)
            self.status_row.setText("入库表头已读取，请校对映射列并保存")
        def finished():
            self.mapping_editor.setEnabled(True)
            self.load_inbound_headers_btn.setEnabled(True)
            self.load_cat_btn.setEnabled(True)
            self.start_btn.setEnabled(True)
        worker.loaded.connect(apply)
        worker.failed.connect(self.show_error)
        worker.finished.connect(finished)
        self.worker = worker
        worker.start()

    def load_categories(self):
        if self.has_running_worker():
            return
        self.refresh_credentials_label()
        sid = self.spreadsheet_id()
        if not sid:
            QMessageBox.warning(
                self,
                APP_SECTION,
                "请填写本页「上传专用表格 ID」（入库表、分类目录所在的 Google 表格）。",
            )
            self.settings_toggle.setChecked(True)
            return
        if not self.category_sheet_edit.text().strip():
            QMessageBox.warning(self, APP_SECTION, "请填写分类工作表名称。")
            self.settings_toggle.setChecked(True)
            return
        try:
            mapping = self.category_mapping_from_ui()
        except ValueError as exc:
            QMessageBox.warning(self, APP_SECTION, str(exc))
            return
        self.start_btn.setEnabled(False)
        worker = CategoryLoadWorker(
            self.credentials_path(),
            self.token_path,
            sid,
            self.category_sheet_edit.text().strip() or CATEGORY_SHEET_NAME,
            columns=[m["column"] for m in mapping],
            start_row=self.category_start_spin.value(),
            inherit=self.category_inherit_check.isChecked(),
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.columns_resolved.connect(self.on_category_columns_resolved)
        worker.headers_loaded.connect(self.on_category_headers_loaded)
        worker.loaded.connect(self.on_categories_loaded)
        worker.finished.connect(lambda: self.start_btn.setEnabled(True))
        self.worker = worker
        worker.start()

    def on_category_headers_loaded(self, headers):
        defaults = ("一级分类", "二级分类", "三级分类", "类型", "上传者", "审核人员")
        for i, fallback in enumerate(defaults):
            header = str(headers[i] or "").strip() if i < len(headers) else ""
            self.category_mapping.item(i, 0).setText(header or fallback)
        self.refresh_category_labels()

    def on_categories_loaded(self, rows: list):
        self.refresh_category_labels()
        self.category_rows = list(rows or [])
        for i, combo in enumerate((self.cat1_combo, self.cat2_combo, self.cat3_combo, self.video_type_combo,
                                   self.uploader_edit, self.reviewer_edit)):
            current = combo.currentText()
            values = list(dict.fromkeys(str(r[i]).strip() for r in self.category_rows
                                        if len(r) > i and str(r[i]).strip()))
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([""] + values)
            combo.setCurrentText(current if current in values or combo.isEditable() else "")
            combo.blockSignals(False)
        self.refresh_category_targets()
        counts = (self.uploader_edit.count() - 1, self.reviewer_edit.count() - 1)
        self.status_row.setText(f"分类 {len(self.category_rows)} 行 · 上传者 {counts[0]} 人 · 审核人员 {counts[1]} 人")
        for i, count in enumerate(counts, 4):
            if count == 0:
                self.log(f"{'上传者' if i == 4 else '审核人员'}没有读取到姓名：请检查分类工作表、读取列及起始行；仍可手动输入。")
        self.log(self.status_row.text())

    def on_paths_dropped(self, paths: list):
        files, folders = [], []
        for p in paths:
            if not p:
                continue
            norm = os.path.normpath(os.path.abspath(p))
            if os.path.isfile(norm):
                files.append(norm)
            elif os.path.isdir(norm):
                folders.append(norm)
        if folders:
            for folder in folders:
                self._ingest_category_folder(folder)
        if files:
            self.pending_files.extend(files)
            self.pending_files = list(dict.fromkeys(self.pending_files))
            self.pending_label.setText(f"待添加：{len(self.pending_files)} 个文件")
            self.log(f"拖入文件 {len(files)} 个。")
            uploader = self.uploader_edit.currentText().strip()
            cat1 = self.cat1_combo.currentText().strip()
            if uploader and (cat1 or self.is_image_mode):
                self.add_task()

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", "所有文件 (*.*)")
        if not paths:
            return
        self.pending_files.extend(paths)
        self.pending_files = list(dict.fromkeys(self.pending_files))
        self.pending_label.setText(f"待添加：{len(self.pending_files)} 个文件")

    def pick_category_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择已按分类建好的文件夹")
        if folder:
            self._ingest_category_folder(folder)

    def _ingest_category_folder(self, folder: str):
        """递归扫描文件夹，按相对路径匹配分类，自动生成多个任务。"""
        uploader = self.uploader_edit.currentText().strip() or "默认上传者"
        reviewer = self.reviewer_edit.currentText().strip()
        clean_meta = self.clean_meta_text()

        all_files = list_files_recursive(folder)
        if not all_files:
            QMessageBox.information(self, APP_SECTION, f"文件夹内没有文件：{os.path.basename(folder)}")
            return

        groups: dict[tuple, list[str]] = {}
        unmatched = []
        root = os.path.abspath(folder)
        root_name = os.path.basename(root.rstrip("\\/"))

        for fpath in all_files:
            rel = os.path.relpath(fpath, root)
            parts = rel.replace("\\", "/").split("/")
            dir_parts = parts[:-1]
            chain = [root_name] + dir_parts if root_name else dir_parts

            c1, c2, c3, c4 = "", "", "", ""
            if self.category_rows:
                c1, c2, c3, c4 = match_path_to_category(chain, self.category_rows)
                if not c1:
                    c1, c2, c3, c4 = match_path_to_category(dir_parts, self.category_rows)
            ext = os.path.splitext(fpath)[1].lower()
            is_img = ext in IMAGE_EXTS and self.is_image_mode
            if not c1 and not is_img:
                unmatched.append(fpath)
                c1 = self.cat1_combo.currentText().strip() or root_name or "未分类素材"
                c2 = self.cat2_combo.currentText().strip()
                c3 = self.cat3_combo.currentText().strip()
                c4 = self.video_type_combo.currentText().strip()
            key = (c1, c2, c3, c4, is_img)
            groups.setdefault(key, []).append(fpath)

        added = 0
        for (c1, c2, c3, c4, is_img), files in groups.items():
            if not files:
                continue
            self.task_seq += 1
            task = UploadTask(
                index=self.task_seq,
                is_image_mode=is_img,
                cat1=c1 if not is_img else "图片素材",
                cat2="" if is_img else c2,
                cat3="" if is_img else c3,
                video_type="" if is_img else c4,
                uploader=uploader,
                reviewer=reviewer,
                clean_meta=clean_meta,
                files=[UploadFileItem(path=p, name=os.path.basename(p)) for p in files],
                source_folder=folder,
            )
            self.tasks.append(task)
            added += 1
            self.log(
                f"自动任务#{task.index}（[{uploader}] {'/'.join([x for x in [c1, c2, c3, c4] if x]) or '图片'}）：{len(files)} 文件"
            )

        self.rebuild_table()
        if unmatched and self.category_rows:
            self.log(f"提示：有 {len(unmatched)} 个文件未全匹配表格分类，已归入指定分类。")
        self.status_row.setText(f"从文件夹自动加入 {added} 个任务（上传者：{uploader}）")
        self.log(f"文件夹扫描完成：{folder}")

    def add_task(self):
        if not self.pending_files:
            QMessageBox.information(self, APP_SECTION, "请先选择或拖入文件。")
            return
        uploader = self.uploader_edit.currentText().strip()
        if not uploader:
            QMessageBox.warning(self, APP_SECTION, "请填写上传者。")
            return
        if not self.is_image_mode:
            cat1 = self.cat1_combo.currentText().strip()
            cat2 = self.cat2_combo.currentText().strip()
            cat3 = self.cat3_combo.currentText().strip()
            vtype = self.video_type_combo.currentText().strip()
        else:
            cat1 = cat2 = cat3 = vtype = ""

        self.task_seq += 1
        files = [UploadFileItem(path=p, name=os.path.basename(p)) for p in self.pending_files if os.path.isfile(p)]
        task = UploadTask(
            index=self.task_seq,
            is_image_mode=self.is_image_mode,
            cat1=cat1 if not self.is_image_mode else "图片素材",
            cat2=cat2, cat3=cat3, video_type=vtype,
            uploader=uploader,
            reviewer=self.reviewer_edit.currentText().strip(),
            clean_meta=self.clean_meta_text(),
            files=files,
        )
        self.tasks.append(task)
        self.pending_files = []
        self.pending_label.setText("待添加：0 个文件")
        self.rebuild_table()
        self.log(f"已加入任务#{task.index}，{len(files)} 个文件。")

    def clear_tasks(self):
        if self.has_running_worker():
            return
        if self.tasks:
            reply = QMessageBox.question(
                self,
                APP_SECTION,
                "确定清空全部上传清单吗？\n（本机已保存的失败记录也会一并删除）",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.tasks = []
        self.task_seq = 0
        self.rebuild_table()
        self.save_task_queue()
        self.progress_bar.setValue(0)
        self.progress_label.setText("等待开始")
        self.log("已清空上传清单。")

    def clear_completed_tasks(self):
        """清除已成功/已跳过，只保留失败与待上传，方便下次继续。"""
        if self.has_running_worker():
            return
        before_files = sum(len(t.files) for t in self.tasks)
        kept_tasks = []
        removed = 0
        for task in self.tasks:
            remain = [f for f in task.files if not is_upload_success_status(f.status)]
            removed += len(task.files) - len(remain)
            if remain:
                task.files = remain
                # 任务状态：若还有失败则标待重试
                if any(str(f.status or "").startswith("失败") or f.status in ("已停止", "待上传") for f in remain):
                    task.status = "待重试"
                kept_tasks.append(task)
        self.tasks = kept_tasks
        self.rebuild_table()
        self.save_task_queue()
        self.log(f"已清除完成项 {removed} 个文件，保留 {sum(len(t.files) for t in self.tasks)} 个待处理/失败。")
        self.status_row.setText(
            f"已清除完成 {removed}，剩余 {sum(len(t.files) for t in self.tasks)} 文件待处理"
        )
        if before_files and removed == 0:
            QMessageBox.information(self, APP_SECTION, "当前没有「已完成」的文件可清除。")

    def save_task_queue(self):
        """把当前清单写到本机，重启软件可恢复。"""
        path = task_queue_path()
        payload = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task_seq": self.task_seq,
            "tasks": [task_to_dict(t) for t in self.tasks],
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.log(f"保存上传清单失败：{exc}")

    def load_task_queue(self):
        """启动时恢复上次未完成/失败的上传清单。"""
        path = task_queue_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            self.log(f"读取上传清单失败：{exc}")
            return
        raw_tasks = data.get("tasks") or []
        restored = []
        missing = 0
        for item in raw_tasks:
            task = task_from_dict(item)
            if not task:
                continue
            # 丢弃本地已不存在的文件
            keep_files = []
            for fi in task.files:
                if is_upload_success_status(fi.status):
                    keep_files.append(fi)  # 成功也先恢复，用户可点「清除已完成」
                elif fi.path and os.path.isfile(fi.path):
                    keep_files.append(fi)
                else:
                    missing += 1
            if not keep_files:
                continue
            task.files = keep_files
            restored.append(task)
        if not restored:
            return
        self.tasks = restored
        self.task_seq = max(
            int(data.get("task_seq") or 0),
            max((t.index for t in restored), default=0),
        )
        failed_n = sum(
            1 for t in self.tasks for f in t.files if not is_upload_success_status(f.status)
        )
        done_n = sum(1 for t in self.tasks for f in t.files if is_upload_success_status(f.status))
        self.rebuild_table()
        self.log(
            f"已恢复上次上传清单：{len(self.tasks)} 任务，"
            f"完成 {done_n}，未完成/失败 {failed_n}"
            + (f"，丢失本地文件 {missing}" if missing else "")
            + f"。\n保存位置：{path}"
        )
        if failed_n:
            self.status_row.setText(f"已恢复清单 · 可点「清除已完成」后「重试失败项」（{failed_n} 个）")

    def rebuild_table(self):
        rows = []
        for task in self.tasks:
            mode = "图片" if task.is_image_mode else "分类"
            cats = " / ".join([x for x in [task.cat1, task.cat2, task.cat3, task.video_type] if x])
            uploader_tag = f"[{task.uploader}] " if task.uploader else ""
            path_label = f"{uploader_tag}{cats or '图片素材'}"
            for f in task.files:
                rows.append((task, f, mode, path_label))
        self.table.setRowCount(len(rows))
        for i, (task, f, mode, path_label) in enumerate(rows):
            vals = [task.index, mode, path_label, f.name, f.status, f.file_url, task.folder_url]
            for col, val in enumerate(vals):
                cell = QTableWidgetItem("" if val is None else str(val))
                if col == 4:
                    st = str(f.status or "")
                    if "成功" in st or st == "待上传":
                        cell.setForeground(QColor("#34d399"))
                    elif "失败" in st:
                        cell.setForeground(QColor("#f87171"))
                    elif "跳过" in st:
                        cell.setForeground(QColor("#fbbf24"))
                if col in (5, 6) and cell.text():
                    cell.setForeground(QColor("#5489ff"))
                    cell.setToolTip("点击复制完整链接\n" + cell.text())
                else:
                    cell.setToolTip(cell.text())
                self.table.setItem(i, col, cell)
        failed_n = sum(
            1 for t in self.tasks for f in t.files if not is_upload_success_status(f.status)
        )
        done_n = sum(1 for t in self.tasks for f in t.files if is_upload_success_status(f.status))
        total = sum(len(t.files) for t in self.tasks)
        self.status_row.setText(
            f"清单 {len(self.tasks)} 任务 / {total} 文件（完成 {done_n} · 待处理/失败 {failed_n}）· 已自动保存"
        )
        self.refresh_receipt_box()
        # 状态变化时落盘（上传中也会刷新）
        if not self.has_running_worker():
            self.save_task_queue()

    def refresh_receipt_box(self):
        lines = []
        for task in self.tasks:
            title = " / ".join([x for x in [task.cat1, task.cat2, task.cat3, task.video_type] if x])
            uploader_tag = f"[{task.uploader}] " if task.uploader else ""
            lines.append(f"【任务#{task.index}】{uploader_tag}{title or '图片直传'}")
            if task.folder_url:
                lines.append(f"  文件夹：{task.folder_url}")
            for f in task.files:
                if f.file_url:
                    lines.append(f"  · {f.name}\n    {f.file_url}")
            lines.append("")
        text = "\n".join(lines).strip()
        if text:
            self.receipt_box.setPlainText(text)

    def copy_receipts(self):
        text = self.receipt_box.toPlainText().strip()
        if not text:
            QMessageBox.information(self, APP_SECTION, "暂无回执。")
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.log("回执已复制。")

    def start_upload(self):
        self._start_upload_worker(only_failed=False)

    def retry_failed_uploads(self):
        """只重试失败/停止的文件。"""
        if self.has_running_worker():
            return
        n = sum(
            1
            for t in self.tasks
            for f in t.files
            if not is_upload_success_status(f.status)
        )
        if n == 0:
            QMessageBox.information(self, APP_SECTION, "当前没有失败项可重试。")
            return
        self.log(f"准备重试 {n} 个未成功文件…")
        self._start_upload_worker(only_failed=True)

    def _start_upload_worker(self, only_failed: bool = False):
        if self.has_running_worker():
            return
        if not self.tasks:
            QMessageBox.information(self, APP_SECTION, "请先加入任务。")
            return
        self.refresh_credentials_label()
        sid = self.spreadsheet_id()
        parent = self.parent_folder_edit.text().strip()
        if not sid or not parent:
            QMessageBox.warning(
                self,
                APP_SECTION,
                "请填写本页「上传专用表格 ID」与「Drive 父文件夹 ID」。\n"
                "请配置入库、分类与日志工作表后点「保存设置并折叠」。",
            )
            self.settings_toggle.setChecked(True)
            return

        if not self.data_sheet_edit.text().strip() or not self.log_sheet_edit.text().strip():
            QMessageBox.warning(self, APP_SECTION, "请填写入库工作表和日志工作表名称。")
            self.settings_toggle.setChecked(True)
            return
        uploader = self.uploader_edit.currentText().strip()
        reviewer = self.reviewer_edit.currentText().strip()
        clean_meta = self.clean_meta_text()
        if not uploader:
            QMessageBox.warning(self, APP_SECTION, "请填写默认上传者。")
            self.settings_toggle.setChecked(True)
            return
        for t in self.tasks:
            if not t.uploader:
                t.uploader = uploader
            if not t.reviewer and reviewer:
                t.reviewer = reviewer
            if not t.clean_meta and clean_meta:
                t.clean_meta = clean_meta

        try:
            self.validate_settings()
        except ValueError as exc:
            QMessageBox.warning(self, APP_SECTION, str(exc))
            return
        self.set_running(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("重试失败项…" if only_failed else "上传中…")

        worker = UploadWorker(
            credentials_path=self.credentials_path(),
            token_path=self.token_path,
            spreadsheet_id=sid,
            parent_folder_id=parent,
            data_sheet=self.data_sheet_edit.text().strip() or DATA_SHEET_NAME,
            log_sheet=self.log_sheet_edit.text().strip() or LOG_SHEET_NAME,
            tasks=self.tasks,
            copyright_agreed=self.copyright_check.isChecked(),
            skip_existing=self.skip_existing_check.isChecked(),
            inbound_mappings=self.mapping_editor.rows(),
            header_row=self.header_row_spin.value(),
            data_row=self.data_row_spin.value(),
            sync_headers=self.sync_headers_check.isChecked(),
            only_failed=only_failed,
            max_retries=3,
            max_threads=getattr(self, "max_threads", 4),
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.item_update.connect(self.on_item_update)
        worker.task_update.connect(self.on_task_update)
        worker.progress.connect(self.on_progress)
        worker.done.connect(self.on_upload_done)
        worker.finished.connect(self.on_worker_finished)
        self.worker = worker
        worker.start()

    def stop_upload(self):
        if self.worker and hasattr(self.worker, "stop"):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.log("已请求停止。")

    def on_item_update(self, task_index, file_index, status, file_url):
        for task in self.tasks:
            if task.index == task_index and 0 <= file_index < len(task.files):
                task.files[file_index].status = status
                if file_url:
                    task.files[file_index].file_url = file_url
                break
        self.rebuild_table()
        # 上传过程中也定期落盘，避免中途崩溃丢失败记录
        self.save_task_queue()

    def on_task_update(self, task_index, status, folder_url):
        for task in self.tasks:
            if task.index == task_index:
                task.status = status
                if folder_url:
                    task.folder_url = folder_url
                break
        self.rebuild_table()
        self.save_task_queue()

    def on_progress(self, done, total):
        pct = int(done * 100 / total) if total else 0
        self.progress_bar.setValue(pct)
        self.progress_label.setText(f"{done}/{total}（{pct}%）")

    def on_upload_done(self):
        self.progress_label.setText("上传完成")
        self.refresh_receipt_box()
        self.save_task_queue()
        failed_n = sum(
            1 for t in self.tasks for f in t.files if not is_upload_success_status(f.status)
        )
        if failed_n:
            self.log(
                f"上传流程结束。仍有 {failed_n} 个未成功，清单已保存到本机；"
                f"下次启动可继续，或点「清除已完成」后「重试失败项」。\n"
                f"清单文件：{task_queue_path()}"
            )
        else:
            self.log("上传流程结束。全部成功；可点「清除已完成」整理清单。")

    def on_worker_finished(self):
        if self.sender() is self.worker:
            self.worker = None
        self.set_running(False)
        self.save_task_queue()

    def set_running(self, running: bool):
        self.config_button.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.retry_failed_btn.setEnabled(not running)
        self.clear_done_btn.setEnabled(not running)
        self.add_task_btn.setEnabled(not running)
        self.load_cat_btn.setEnabled(not running)
        self.clear_tasks_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def open_parent_folder(self):
        fid = self.parent_folder_edit.text().strip()
        if not fid:
            return
        QDesktopServices.openUrl(QUrl(f"https://drive.google.com/drive/folders/{fid}"))

    def show_error(self, message: str):
        self.log(message)
        if "scope" in message.lower() or "insufficient" in message.lower() or "权限" in message:
            message += (
                f"\n\n授权文件位置：\n{self.token_path}\n"
                "仅当权限不足时删除该文件再登录一次即可。"
            )
        QMessageBox.warning(self, APP_SECTION, message)

    def request_close(self) -> bool:
        if not self.has_running_worker():
            return True
        self.stop_upload()
        if self.worker and not self.worker.wait(3000):
            return False
        return True
