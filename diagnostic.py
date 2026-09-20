#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""深度诊断：只读统计清理工具覆盖不到的占用，并给出处理建议。

本模块**只展示，不提供任何删除入口**。结果全部来自
`CleanerLogic.scan_diagnostics()`，其条目类型为 `diagnostic_only`
（已加入 `ANALYSIS_ONLY_CATEGORIES`），删除核心会拒绝处理这些条目。
"""

import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from cleaner_logic import DIAGNOSTIC_GROUPS, DIAGNOSTIC_GROUP_ORDER


def format_size(size_bytes):
    value = float(size_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f"{int(value)} B" if unit == 'B' else f"{value:.2f} {unit}"
        value /= 1024


def _size_text(item):
    """渲染大小，区分「诊断失败」「无权限」「超时」，避免把读不到的显示成 0 B。"""
    if item.get('failed'):
        return '诊断失败'
    if not item.get('exists'):
        return '—'
    if not item.get('accessible', True):
        return '无权限读取'
    text = format_size(item.get('size', 0))
    if item.get('incomplete'):
        text += '（可能更大）'
    return text


def _deduped_total(items):
    """按路径去重后求和。

    「应用数据 Top 榜」与具体探针（如 ms-playwright、JetBrains）会互相嵌套，
    直接相加会重复计算；这里只保留最外层路径，避免合计被虚高。
    """
    loose = 0
    by_path = {}
    for item in items:
        path = item.get('path', '')
        size = item.get('size', 0)
        if not path or ' | ' in path:
            loose += size
            continue
        key = os.path.normcase(os.path.normpath(path))
        # 多个探针可能指向同一个目录，同路径只计一次
        by_path.setdefault(key, size)

    kept = []
    for path in sorted(by_path, key=len):
        if any(path == root or path.startswith(root + os.sep) for root in kept):
            continue
        kept.append(path)
    return loose + sum(by_path[path] for path in kept)


class DiagnosticScanThread(QThread):
    """后台执行只读诊断。信号名用 scan_finished，避免遮蔽 QThread.finished。"""

    progress = Signal(str, int)      # 当前探针名, 0-100
    scan_finished = Signal(list)     # 诊断条目
    failed = Signal(str)

    def __init__(self, cleaner):
        super().__init__()
        self.cleaner = cleaner
        self._abort = False

    def run(self):
        try:
            items = self.cleaner.scan_diagnostics(
                progress_callback=lambda name, pct: self.progress.emit(name, pct),
                abort_callback=lambda: self._abort,
            )
        except Exception as exc:  # 线程内必须兜底，否则失败会被静默吞掉
            self.failed.emit(str(exc))
            return
        self.scan_finished.emit(list(items))

    def request_stop(self):
        self._abort = True


class DiagnosticDialog(QDialog):
    """只读展示「空间到底去了哪里」。整个对话框没有任何删除入口。"""

    def __init__(self, cleaner, parent=None):
        super().__init__(parent)
        self.cleaner = cleaner
        self.items = []
        self._scan_thread = None
        self.setWindowTitle("深度诊断 — 清理工具覆盖不到的占用")
        self.resize(1180, 720)
        self.setMinimumSize(980, 600)
        self._build_ui()

    # ────────────────────────── UI ──────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        intro = QLabel(
            "这里统计的是<b>清理工具覆盖不到的占用</b>，用来快速判断空间去了哪里。<br>"
            "本页面<b>只读</b>：不会删除、移动或修改任何文件；"
            "「处理建议」仅供参考，请自行确认后手动执行。"
        )
        intro.setWordWrap(True)
        intro.setObjectName("status")
        layout.addWidget(intro)

        control_row = QHBoxLayout()
        self.scan_button = QPushButton("开始诊断")
        self.scan_button.clicked.connect(self.start_scan)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("secondary")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_scan)
        control_row.addWidget(self.scan_button)
        control_row.addWidget(self.stop_button)
        control_row.addStretch()
        layout.addLayout(control_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("点击「开始诊断」扫描占用大户。")
        self.status_label.setObjectName("status")
        layout.addWidget(self.status_label)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["项目", "大小", "备注", "路径", "处理建议"])
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 150)
        self.tree.setColumnWidth(2, 220)
        self.tree.setColumnWidth(3, 330)
        self.tree.setColumnWidth(4, 430)
        self.tree.setAlternatingRowColors(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.tree, 1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

    # ────────────────────── 扫描生命周期 ──────────────────────

    def start_scan(self):
        if self._scan_thread and self._scan_thread.isRunning():
            return
        self.tree.clear()
        self.summary_label.clear()
        self.items = []
        self.progress_bar.setValue(0)
        self.status_label.setText("正在诊断…")
        self.scan_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        thread = DiagnosticScanThread(self.cleaner)
        thread.progress.connect(self._on_progress)
        thread.scan_finished.connect(self._on_scan_finished)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(self._on_thread_done)
        self._scan_thread = thread
        thread.start()

    def stop_scan(self):
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.request_stop()
            self.status_label.setText("正在停止…")
            self.stop_button.setEnabled(False)

    def _on_progress(self, name, percent):
        self.progress_bar.setValue(max(0, min(100, percent)))
        if name:
            self.status_label.setText(f"正在诊断：{name}")

    def _on_scan_finished(self, items):
        self.items = list(items)
        self.progress_bar.setValue(100)
        self._populate(self.items)

    def _on_failed(self, message):
        self.status_label.setText("诊断失败。")
        QMessageBox.warning(self, "深度诊断", f"诊断过程中出现错误：\n{message}")

    def _on_thread_done(self):
        self.scan_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    # ────────────────────────── 渲染 ──────────────────────────

    def _populate(self, items):
        self.tree.clear()
        visible = [item for item in items if item.get('exists')]
        skipped = len(items) - len(visible)

        order = list(DIAGNOSTIC_GROUP_ORDER)
        groups = {}
        for item in visible:
            groups.setdefault(item.get('group', ''), []).append(item)
        for key in sorted(groups):
            if key not in order:
                order.append(key)

        for key in order:
            entries = groups.get(key)
            if not entries:
                continue
            entries.sort(key=lambda item: item.get('size', 0), reverse=True)
            total = _deduped_total(entries)

            group_node = QTreeWidgetItem([
                DIAGNOSTIC_GROUPS.get(key, key),
                format_size(total),
                f"{len(entries)} 项",
                "",
                "",
            ])
            bold = group_node.font(0)
            bold.setBold(True)
            group_node.setFont(0, bold)
            group_node.setFont(1, bold)

            for entry in entries:
                child = QTreeWidgetItem([
                    self._name_text(entry),
                    _size_text(entry),
                    entry.get('note', '') or '',
                    entry.get('path', ''),
                    entry.get('suggestion', ''),
                ])
                child.setToolTip(3, entry.get('path', ''))
                child.setToolTip(4, entry.get('suggestion', ''))
                group_node.addChild(child)
            self.tree.addTopLevelItem(group_node)
        self.tree.expandAll()

        total_all = _deduped_total(visible)
        parts = [f"合计占用（已去重）：<b>{format_size(total_all)}</b>", f"共 {len(visible)} 项"]
        if skipped:
            parts.append(f"跳过 {skipped} 项（不存在）")
        denied = sum(1 for item in visible if not item.get('accessible', True))
        if denied:
            parts.append(f"{denied} 项无权限读取")
        timed_out = sum(1 for item in visible if item.get('incomplete'))
        if timed_out:
            parts.append(f"{timed_out} 项未统计完整（超时）")
        self.summary_label.setText("　·　".join(parts))
        self.status_label.setText("诊断完成。本页未修改任何文件。")

    @staticmethod
    def _name_text(item):
        text = item.get('name', '')
        count = item.get('file_count', 0)
        if count:
            text = f"{text}（{count} 个文件）"
        return text

    def _show_context_menu(self, pos):
        node = self.tree.itemAt(pos)
        if node is None:
            return
        path = node.text(3)
        if not path:
            return
        menu = QMenu(self)
        copy_action = menu.addAction("复制路径")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is copy_action:
            QApplication.clipboard().setText(path)
            self.status_label.setText("已复制路径到剪贴板。")

    # ────────────────────── 关闭时的线程清理 ──────────────────────

    def _confirm_stop(self):
        answer = QMessageBox.question(
            self, "正在诊断", "诊断仍在进行，是否停止并关闭？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def closeEvent(self, event):
        if self._scan_thread and self._scan_thread.isRunning():
            if self._confirm_stop():
                self.stop_scan()
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self):
        if self._scan_thread and self._scan_thread.isRunning():
            if self._confirm_stop():
                self.stop_scan()
            return
        super().reject()
