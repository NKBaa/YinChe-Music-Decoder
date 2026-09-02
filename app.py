from __future__ import annotations

import sys
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PySide6.QtCore import QEasingCurve, QObject, Property, QPropertyAnimation, QSize, QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QPainter
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QFileDialog,
    QGraphicsOpacityEffect, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QRadioButton, QStyle, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from decoders import Cancelled, SUPPORTED_SUFFIXES, decode_file, service_for

APP_NAME = "音澈"


def format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


@dataclass
class QueueItem:
    path: Path
    root: Path | None = None


class ThreadStepper(QWidget):
    def __init__(self, value=4, minimum=1, maximum=32, parent=None):
        super().__init__(parent)
        self.minimum, self.maximum, self._value = minimum, maximum, value
        self.setObjectName("threadStepper")
        layout = QHBoxLayout(self); layout.setContentsMargins(3, 3, 3, 3); layout.setSpacing(0)
        self.minus = QPushButton("−"); self.minus.setObjectName("stepButton"); self.minus.setToolTip("减少线程")
        self.number = QLabel(); self.number.setObjectName("stepValue"); self.number.setAlignment(Qt.AlignmentFlag.AlignCenter); self.number.setFixedWidth(62)
        self.plus = QPushButton("+"); self.plus.setObjectName("stepButton"); self.plus.setToolTip("增加线程")
        self.minus.clicked.connect(lambda: self.set_value(self._value - 1)); self.plus.clicked.connect(lambda: self.set_value(self._value + 1))
        layout.addWidget(self.minus); layout.addWidget(self.number); layout.addWidget(self.plus); self.set_value(value)

    def value(self):
        return self._value

    def set_value(self, value):
        self._value = max(self.minimum, min(self.maximum, value))
        self.number.setText(str(self._value))
        self.minus.setEnabled(self._value > self.minimum); self.plus.setEnabled(self._value < self.maximum)


class ToggleSwitch(QAbstractButton):
    def __init__(self, checked=True, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._position = 1.0 if checked else 0.0
        self.animation = QPropertyAnimation(self, b"position", self)
        self.animation.setDuration(170)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self.animate_toggle)

    def sizeHint(self):
        return QSize(42, 24)

    def get_position(self):
        return self._position

    def set_position(self, value):
        self._position = value
        self.update()

    position = Property(float, get_position, set_position)

    def animate_toggle(self, checked):
        self.animation.stop()
        self.animation.setStartValue(self._position)
        self.animation.setEndValue(1.0 if checked else 0.0)
        self.animation.start()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = self.rect().adjusted(1, 2, -1, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#176b58") if self.isChecked() else QColor("#cbd4d2"))
        painter.drawRoundedRect(track, 10, 10)
        diameter = 16
        left = 4 + self._position * (self.width() - diameter - 8)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(int(left), 4, diameter, diameter)


class BatchWorker(QObject):
    row_status = Signal(int, str, str)
    progress = Signal(int)
    status = Signal(str)
    summary = Signal(dict)
    finished = Signal(dict, bool)

    def __init__(self, items, output, mode, overwrite, preserve_tree, workers, cancel):
        super().__init__()
        self.items, self.output, self.mode = items, output, mode
        self.overwrite, self.preserve_tree, self.cancel = overwrite, preserve_tree, cancel
        self.workers = workers
        self._fractions = [0.0] * len(items)
        self._progress_lock = threading.Lock()

    def output_for(self, item):
        out = self.output
        if self.preserve_tree and item.root:
            try:
                out /= item.path.parent.relative_to(item.root)
            except ValueError:
                pass
        return out

    def decode_one(self, index, item):
        if self.cancel.is_set():
            raise Cancelled("任务已取消")
        self.row_status.emit(index, "", "解码中")
        def update(done, size):
            with self._progress_lock:
                self._fractions[index] = min(1.0, done / max(1, size))
                value = int(sum(self._fractions) / len(self._fractions) * 100)
            self.progress.emit(value)
        result = decode_file(item.path, self.output_for(item), self.mode, self.overwrite, self.cancel, update)
        with self._progress_lock:
            self._fractions[index] = 1.0
            value = int(sum(self._fractions) / len(self._fractions) * 100)
        self.progress.emit(value)
        return result

    def run(self):
        counts = {"done": 0, "skipped": 0, "filtered": 0, "failed": 0}
        total = len(self.items)
        completed = 0
        largest = max((item.path.stat().st_size for item in self.items), default=1)
        memory_workers = max(1, (1024 * 1024 * 1024) // max(1, largest))
        actual_workers = max(1, min(self.workers, memory_workers, total))
        suffix = "" if actual_workers == self.workers else f"（内存保护限制为 {actual_workers}）"
        self.status.emit(f"正在使用 {actual_workers} 个线程处理 {total} 个文件{suffix}")
        with ThreadPoolExecutor(max_workers=actual_workers, thread_name_prefix="yinche") as pool:
            futures = {pool.submit(self.decode_one, index, item): index for index, item in enumerate(self.items)}
            for future in as_completed(futures):
                index = futures[future]
                if future.cancelled():
                    continue
                try:
                    result = future.result()
                    counts[result.status] += 1
                    label = {"done": "完成", "skipped": "已跳过", "filtered": "格式不匹配"}[result.status]
                    if result.status == "done":
                        written = [name for name, present in (("歌词", result.lyrics_written), ("封面", result.cover_written)) if present]
                        if written:
                            label += " · " + " · ".join(written)
                    self.row_status.emit(index, (result.audio_format or "").upper(), label)
                except Cancelled:
                    self.row_status.emit(index, "", "已取消")
                except Exception as error:
                    counts["failed"] += 1
                    self.row_status.emit(index, "", f"失败：{error}")
                completed += 1
                self.status.emit(f"并行处理中  {completed}/{total} 已完成")
                self.summary.emit(dict(counts))
                if self.cancel.is_set():
                    for pending in futures:
                        pending.cancel()
        self.finished.emit(counts, self.cancel.is_set())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  音乐解码")
        self.resize(1120, 720)
        self.setMinimumSize(900, 620)
        self.setAcceptDrops(True)
        self.items, self.cancel, self.thread, self.worker = [], Event(), None, None
        self.build_ui()
        self.apply_style()

    def build_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        page = QVBoxLayout(central); page.setContentsMargins(28, 22, 28, 20); page.setSpacing(14)
        header = QHBoxLayout(); titles = QVBoxLayout()
        title = QLabel(APP_NAME); title.setObjectName("title"); titles.addWidget(title)
        subtitle = QLabel("网易云 · 酷狗 · QQ 音乐本地批量解码"); subtitle.setObjectName("muted"); titles.addWidget(subtitle)
        header.addLayout(titles); header.addStretch()
        self.add_files_btn = QPushButton("添加文件"); self.add_folder_btn = QPushButton("添加文件夹"); self.clear_btn = QPushButton("清空队列")
        self.add_files_btn.clicked.connect(self.add_files); self.add_folder_btn.clicked.connect(self.add_folder); self.clear_btn.clicked.connect(self.clear_queue)
        header.addWidget(self.add_files_btn); header.addWidget(self.add_folder_btn); header.addWidget(self.clear_btn); page.addLayout(header)

        panel = QWidget(); panel.setObjectName("panel"); settings = QVBoxLayout(panel); settings.setContentsMargins(16, 14, 16, 14)
        output_line = QHBoxLayout(); output_line.addWidget(QLabel("输出位置"))
        self.output = QLineEdit(str(Path.home() / "Music" / "Decoded")); output_line.addWidget(self.output, 1)
        browse = QPushButton("浏览"); browse.clicked.connect(self.choose_output); output_line.addWidget(browse)
        self.open_output_btn = QPushButton("打开"); self.open_output_btn.setObjectName("openFolder")
        self.open_output_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.open_output_btn.setToolTip("在资源管理器中打开当前输出目录")
        self.open_output_btn.clicked.connect(self.open_output_folder); output_line.addWidget(self.open_output_btn); settings.addLayout(output_line)
        options = QHBoxLayout(); options.setSpacing(10); options.addWidget(QLabel("输出格式"))
        self.format_group = QButtonGroup(self); self.format_group.setExclusive(True)
        format_segment = QWidget(); format_segment.setObjectName("segmentShell"); format_layout = QHBoxLayout(format_segment)
        format_layout.setContentsMargins(3, 3, 3, 3); format_layout.setSpacing(3)
        self.auto = QPushButton("自动保留原格式"); self.mp3 = QPushButton("仅 MP3"); self.flac = QPushButton("仅 FLAC")
        for index, button in enumerate((self.auto, self.mp3, self.flac)):
            button.setCheckable(True); button.setObjectName("segmentButton"); self.format_group.addButton(button, index); format_layout.addWidget(button)
        self.auto.setChecked(True); options.addWidget(format_segment)
        options.addSpacing(18); options.addWidget(QLabel("同名文件"))
        self.overwrite_group = QButtonGroup(self); self.overwrite_group.setExclusive(True)
        overwrite_segment = QWidget(); overwrite_segment.setObjectName("segmentShell"); overwrite_layout = QHBoxLayout(overwrite_segment)
        overwrite_layout.setContentsMargins(3, 3, 3, 3); overwrite_layout.setSpacing(3)
        for index, label in enumerate(("自动重命名", "跳过", "覆盖")):
            button = QPushButton(label); button.setCheckable(True); button.setObjectName("segmentButton")
            self.overwrite_group.addButton(button, index); overwrite_layout.addWidget(button)
            if index == 0: button.setChecked(True)
        options.addWidget(overwrite_segment); options.addStretch(); settings.addLayout(options)
        scan_options = QHBoxLayout(); scan_options.setSpacing(28)
        self.preserve = ToggleSwitch(True)
        self.preserve.setToolTip("输出时复刻导入文件夹内的子目录层级")
        preserve_wrap = QVBoxLayout(); preserve_wrap.setSpacing(2); preserve_title = QHBoxLayout(); preserve_title.setSpacing(9)
        preserve_title.addWidget(self.preserve); preserve_title.addWidget(QLabel("保留目录结构")); preserve_title.addStretch(); preserve_wrap.addLayout(preserve_title)
        preserve_help = QLabel("输出时复刻导入文件夹内的子目录层级"); preserve_help.setObjectName("helper"); preserve_wrap.addWidget(preserve_help)
        self.recursive = ToggleSwitch(True)
        self.recursive.setToolTip("导入文件夹时，同时搜索其中的所有子文件夹")
        recursive_wrap = QVBoxLayout(); recursive_wrap.setSpacing(2); recursive_title = QHBoxLayout(); recursive_title.setSpacing(9)
        recursive_title.addWidget(self.recursive); recursive_title.addWidget(QLabel("递归扫描")); recursive_title.addStretch(); recursive_wrap.addLayout(recursive_title)
        recursive_help = QLabel("导入文件夹时，同时搜索其中的所有子文件夹"); recursive_help.setObjectName("helper"); recursive_wrap.addWidget(recursive_help)
        threads_wrap = QVBoxLayout(); threads_wrap.setSpacing(2)
        threads_line = QHBoxLayout(); threads_line.setSpacing(8); threads_line.addWidget(QLabel("并行线程"))
        self.thread_count = ThreadStepper(min(4, os.cpu_count() or 4))
        self.thread_count.setToolTip("线程越多不一定越快；机械硬盘建议 2–4，SSD 通常建议 4–8")
        threads_line.addWidget(self.thread_count); threads_wrap.addLayout(threads_line)
        threads_help = QLabel("机械硬盘 2–4，SSD 4–8；20 线程可能受磁盘限制"); threads_help.setObjectName("helper"); threads_wrap.addWidget(threads_help)
        scan_options.addLayout(preserve_wrap); scan_options.addLayout(recursive_wrap); scan_options.addLayout(threads_wrap); scan_options.addStretch()
        settings.addLayout(scan_options); page.addWidget(panel)

        table_panel = QWidget(); table_panel.setObjectName("panel"); table_layout = QVBoxLayout(table_panel); table_layout.setContentsMargins(0, 0, 0, 0)
        table_head = QHBoxLayout(); table_head.setContentsMargins(14, 10, 14, 8); self.summary_label = QLabel("0 个文件"); table_head.addWidget(self.summary_label); table_head.addStretch()
        formats = QLabel("支持 NCM · KGM/KGMA/VPR · MFLAC/MGG"); formats.setObjectName("muted"); table_head.addWidget(formats); table_layout.addLayout(table_head)
        self.table = QTableWidget(0, 5); self.table.setHorizontalHeaderLabels(["文件", "来源", "真实格式", "大小", "状态"]); self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows); self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch); self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3): self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        table_layout.addWidget(self.table); page.addWidget(table_panel, 1)

        footer = QHBoxLayout(); progress_col = QVBoxLayout(); progress_head = QHBoxLayout()
        self.status_label = QLabel("可拖入文件或文件夹"); self.percent = QLabel("0%"); self.percent.setObjectName("muted")
        progress_head.addWidget(self.status_label); progress_head.addStretch(); progress_head.addWidget(self.percent); progress_col.addLayout(progress_head)
        self.progress_bar = QProgressBar(); self.progress_bar.setTextVisible(False); progress_col.addWidget(self.progress_bar); footer.addLayout(progress_col, 1)
        self.remove_btn = QPushButton("移除选中"); self.stop_btn = QPushButton("停止"); self.stop_btn.setObjectName("danger"); self.stop_btn.setEnabled(False)
        self.start_btn = QPushButton("开始解码"); self.start_btn.setObjectName("primary")
        self.remove_btn.clicked.connect(self.remove_selected); self.stop_btn.clicked.connect(self.stop); self.start_btn.clicked.connect(self.start)
        footer.addWidget(self.remove_btn); footer.addWidget(self.stop_btn); footer.addWidget(self.start_btn); page.addLayout(footer)
        self.success_toast = QLabel("")
        self.success_toast.setObjectName("successToast")
        self.success_toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.success_toast.hide()
        page.addWidget(self.success_toast)
        self.toast_effect = QGraphicsOpacityEffect(self.success_toast); self.success_toast.setGraphicsEffect(self.toast_effect)
        self.toast_animation = QPropertyAnimation(self.toast_effect, b"opacity", self)
        self.toast_animation.finished.connect(self.success_toast.hide)

    def apply_style(self):
        self.setStyleSheet("""
        * { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #20262e; }
        QMainWindow, QWidget { background: #f3f6f7; } #title { font-size: 29px; font-weight: 700; color: #11161c; } #muted, #helper { color: #74808a; } #helper { font-size: 11px; }
        #panel { background: #ffffff; border: 1px solid #dde5e7; border-radius: 8px; } #panel QLabel, #panel QRadioButton { background: transparent; }
        #segmentShell { background: #edf2f1; border: 1px solid #dbe4e1; border-radius: 8px; }
        QPushButton#segmentButton { background: transparent; color: #59656b; border: 0; border-radius: 6px; padding: 7px 12px; }
        QPushButton#segmentButton:hover { background: #e4ece9; color: #263932; }
        QPushButton#segmentButton:checked { background: #ffffff; color: #176b58; font-weight: 600; border: 1px solid #b9d4cc; }
        QPushButton { background: #ffffff; border: 1px solid #ccd6d9; border-radius: 8px; padding: 9px 15px; } QPushButton:hover { background: #eff5f3; border-color: #9fb6af; }
        QPushButton:pressed { background: #e9eded; } QPushButton:disabled { color: #99a2a8; background: #eef1f2; }
        QPushButton#primary { background: #176b58; color: white; border-color: #176b58; font-weight: 600; } QPushButton#primary:hover { background: #125747; }
        QPushButton#danger { color: #a63832; border-color: #d8bbb8; } QLineEdit { background: white; border: 1px solid #cfd6da; border-radius: 8px; padding: 8px 10px; }
        QLineEdit:focus { border: 2px solid #2b7c69; padding: 7px 9px; }
        #threadStepper { background: #edf2f1; border: 1px solid #dbe4e1; border-radius: 8px; }
        QPushButton#stepButton { min-width: 34px; max-width: 34px; min-height: 30px; padding: 0; border: 0; background: transparent; color: #176b58; font-size: 18px; font-weight: 500; }
        QPushButton#stepButton:hover { background: #dfeae7; }
        QPushButton#stepButton:disabled { background: transparent; color: #b3bfbc; }
        #stepValue { background: #ffffff; border-left: 1px solid #d4e0dc; border-right: 1px solid #d4e0dc; color: #173d34; font-weight: 700; font-size: 14px; min-height: 30px; }
        QTableWidget { background: #ffffff; alternate-background-color: #f8f9fa; border: 0; gridline-color: #edf0f1; selection-background-color: #dcece7; selection-color: #173d34; }
        QHeaderView::section { background: #eef1f3; border: 0; border-right: 1px solid #dfe4e7; padding: 9px; font-weight: 600; color: #505963; }
        QProgressBar { height: 7px; background: #dfe6e4; border: 0; border-radius: 3px; } QProgressBar::chunk { background: #1d8069; border-radius: 3px; }
        #successToast { background: #dff3ea; color: #155b49; border: 1px solid #a9dacb; border-radius: 8px; padding: 12px 18px; font-size: 14px; font-weight: 600; }
        """)

    def mode(self): return ("auto", "mp3", "flac")[self.format_group.checkedId()]
    def overwrite_mode(self): return ("rename", "skip", "overwrite")[self.overwrite_group.checkedId()]

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择加密音乐", "", "加密音乐 (*.ncm *.kgm *.kgma *.vpr *.mflac *.mgg);;所有文件 (*)")
        self.add_paths(map(Path, paths))

    def add_folder(self):
        selected = QFileDialog.getExistingDirectory(self, "选择音乐文件夹")
        if selected:
            root = Path(selected).resolve(); iterator = root.rglob("*") if self.recursive.isChecked() else root.glob("*"); self.add_paths(iterator, root)

    def add_paths(self, paths, root=None):
        existing = {item.path for item in self.items}; added = 0
        for path in paths:
            path = path.resolve()
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES or path in existing: continue
            self.items.append(QueueItem(path, root)); row = self.table.rowCount(); self.table.insertRow(row)
            for col, value in enumerate((path.name, service_for(path), "待识别", format_size(path.stat().st_size), "等待")): self.table.setItem(row, col, QTableWidgetItem(value))
            existing.add(path); added += 1
        self.summary_label.setText(f"{len(self.items)} 个文件"); self.status_label.setText(f"已添加 {added} 个文件" if added else "没有发现新的支持文件")

    def choose_output(self):
        selected = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if selected: self.output.setText(selected)

    def open_output_folder(self):
        path = Path(self.output.text()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer.exe", str(path.resolve())])
        except OSError as error:
            QMessageBox.warning(self, APP_NAME, f"无法打开输出目录：\n{error}")

    def clear_queue(self): self.items.clear(); self.table.setRowCount(0); self.summary_label.setText("0 个文件"); self.status_label.setText("可拖入文件或文件夹")
    def remove_selected(self):
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True): self.table.removeRow(row); self.items.pop(row)
        self.summary_label.setText(f"{len(self.items)} 个文件")

    def set_running(self, running):
        for button in (self.add_files_btn, self.add_folder_btn, self.clear_btn, self.remove_btn, self.start_btn): button.setEnabled(not running)
        self.thread_count.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def start(self):
        if not self.items: QMessageBox.information(self, APP_NAME, "请先添加需要解码的文件。"); return
        output = Path(self.output.text()).expanduser()
        try: output.mkdir(parents=True, exist_ok=True)
        except OSError as error: QMessageBox.critical(self, APP_NAME, f"无法创建输出目录：\n{error}"); return
        self.cancel.clear(); self.set_running(True); self.thread = QThread(self)
        self.worker = BatchWorker(list(self.items), output, self.mode(), self.overwrite_mode(), self.preserve.isChecked(), self.thread_count.value(), self.cancel); self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run); self.worker.row_status.connect(self.set_row_status); self.worker.progress.connect(self.set_progress)
        self.worker.status.connect(self.status_label.setText); self.worker.summary.connect(self.set_summary); self.worker.finished.connect(self.batch_finished); self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater); self.thread.finished.connect(self.thread_finished)
        self.thread.finished.connect(self.thread.deleteLater); self.thread.start()

    def set_row_status(self, row, fmt, status):
        if fmt: self.table.item(row, 2).setText(fmt)
        self.table.item(row, 4).setText(status); self.table.scrollToItem(self.table.item(row, 0))
    def set_progress(self, value): self.progress_bar.setValue(value); self.percent.setText(f"{value}%")
    def set_summary(self, counts): self.summary_label.setText(f"{len(self.items)} 个文件 · 完成 {counts['done']} · 跳过 {counts['skipped'] + counts['filtered']} · 失败 {counts['failed']}")
    def batch_finished(self, counts, cancelled):
        self.set_running(False)
        if not cancelled: self.set_progress(100)
        self.status_label.setText("任务已停止" if cancelled else f"处理完成，成功输出 {counts['done']} 个文件")
        if not cancelled and counts["done"]:
            self.show_success(f"解码完成  ·  已成功输出 {counts['done']} 个文件")
    def show_success(self, text):
        self.success_toast.setText(text); self.success_toast.show()
        self.toast_animation.stop(); self.toast_animation.setDuration(2400)
        self.toast_animation.setStartValue(0.0); self.toast_animation.setKeyValueAt(0.12, 1.0)
        self.toast_animation.setKeyValueAt(0.78, 1.0); self.toast_animation.setEndValue(0.0)
        self.toast_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toast_animation.start()
    def thread_finished(self): self.worker = self.thread = None
    def stop(self): self.cancel.set(); self.stop_btn.setEnabled(False); self.status_label.setText("正在停止，请稍候...")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
    def dropEvent(self, event):
        files = []
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir(): self.add_paths(path.rglob("*") if self.recursive.isChecked() else path.glob("*"), path.resolve())
            else: files.append(path)
        self.add_paths(files)
    def closeEvent(self, event: QCloseEvent):
        if self.thread and self.thread.isRunning():
            QMessageBox.information(self, APP_NAME, "文件仍在解码或写入磁盘。为避免文件损坏，完成前不能关闭应用。")
            event.ignore(); return
        event.accept()


def main():
    app = QApplication(sys.argv); app.setStyle("Fusion"); window = MainWindow(); window.show(); return app.exec()

if __name__ == "__main__": raise SystemExit(main())
