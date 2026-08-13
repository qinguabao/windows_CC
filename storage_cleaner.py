#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""其它磁盘的大文件、重复文件分析与清理界面。"""

import os
import subprocess
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)


def format_size(size_bytes):
    value = float(size_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f"{int(value)} B" if unit == 'B' else f"{value:.2f} {unit}"
        value /= 1024


class StorageScanThread(QThread):
    progress = Signal(str, int)  # path, file_count
    phase_changed = Signal(str)  # phase description
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, cleaner, options):
        super().__init__()
        self.cleaner = cleaner
        self.options = options
        self._abort = False

    def run(self):
        try:
            result = self.cleaner.scan_storage(
                abort_callback=lambda: self._abort,
                progress_callback=lambda path, count: self.progress.emit(path, count),
                **self.options,
            )
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.finished.emit(result)

    def request_stop(self):
        self._abort = True


class StorageCleanThread(QThread):
    progress = Signal(str, int)
    finished = Signal(dict)

    def __init__(self, cleaner, items):
        super().__init__()
        self.cleaner = cleaner
        self.items = items

    def run(self):
        result = self.cleaner.clean_selected(self.items, self.progress)
        self.finished.emit(result)


class StorageCleanerDialog(QDialog):
    """选择其它磁盘，分析并清理用户明确勾选的文件。"""

    def __init__(self, cleaner, parent=None, simulate=True, backup=True):
        super().__init__(parent)
        self.cleaner = cleaner
        self.results = {}
        self._busy = False
        self.setWindowTitle("其它磁盘清理")
        self.resize(1060, 700)
        self.setMinimumSize(900, 620)
        self._build_ui(simulate, backup)
        self.refresh_drives()

    def _build_ui(self, simulate, backup):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        drive_row = QHBoxLayout()
        drive_row.addWidget(QLabel("目标磁盘"))
        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(300)
        self.drive_combo.currentIndexChanged.connect(self._drive_changed)
        drive_row.addWidget(self.drive_combo, 1)
        refresh_button = QPushButton("刷新磁盘")
        refresh_button.setObjectName("secondary")
        refresh_button.clicked.connect(self.refresh_drives)
        drive_row.addWidget(refresh_button)
        self.drive_refresh_button = refresh_button
        layout.addLayout(drive_row)

        self.disk_summary = QLabel("正在读取磁盘信息…")
        self.disk_summary.setObjectName("diskLabel")
        layout.addWidget(self.disk_summary)

        scan_row = QHBoxLayout()
        self.large_checkbox = QCheckBox("大文件")
        self.large_checkbox.setChecked(True)
        self.large_limit = QSpinBox()
        self.large_limit.setRange(100, 102400)
        self.large_limit.setValue(1024)
        self.large_limit.setSuffix(" MB 起")
        self.large_checkbox.toggled.connect(self.large_limit.setEnabled)
        self.duplicate_checkbox = QCheckBox("重复文件")
        self.duplicate_checkbox.setChecked(True)
        self.duplicate_limit = QSpinBox()
        self.duplicate_limit.setRange(1, 10240)
        self.duplicate_limit.setValue(10)
        self.duplicate_limit.setSuffix(" MB 起")
        self.duplicate_checkbox.toggled.connect(self.duplicate_limit.setEnabled)
        scan_row.addWidget(self.large_checkbox)
        scan_row.addWidget(self.large_limit)
        scan_row.addSpacing(16)
        scan_row.addWidget(self.duplicate_checkbox)
        scan_row.addWidget(self.duplicate_limit)
        scan_row.addStretch()
        self.scan_button = QPushButton("开始扫描")
        self.scan_button.clicked.connect(self.start_scan)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("danger")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self.stop_scan)
        scan_row.addWidget(self.scan_button)
        scan_row.addWidget(self.stop_button)
        layout.addLayout(scan_row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel("选择目标磁盘并开始扫描。扫描结果默认不勾选。")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.tabs = QTabWidget()
        self.large_tree = self._make_tree()
        self.duplicate_tree = self._make_tree()
        self.tabs.addTab(self.large_tree, "大文件 (0)")
        self.tabs.addTab(self.duplicate_tree, "重复文件 (0 组)")
        layout.addWidget(self.tabs, 1)

        summary_row = QHBoxLayout()
        self.scan_summary = QLabel("尚未扫描")
        self.selected_summary = QLabel("已选 0 个文件，预计释放 0 B")
        self.selected_summary.setStyleSheet("font-weight: 700; color: #d33;")
        summary_row.addWidget(self.scan_summary, 1)
        summary_row.addWidget(self.selected_summary)
        layout.addLayout(summary_row)

        action_row = QHBoxLayout()
        self.simulate_checkbox = QCheckBox("模拟模式（不实际删除）")
        self.simulate_checkbox.setChecked(simulate)
        self.backup_checkbox = QCheckBox("删除前备份")
        self.backup_checkbox.setChecked(backup)
        action_row.addWidget(self.simulate_checkbox)
        action_row.addWidget(self.backup_checkbox)
        action_row.addStretch()
        self.open_button = QPushButton("打开所在位置")
        self.open_button.setObjectName("secondary")
        self.open_button.clicked.connect(self.open_selected_location)
        clear_button = QPushButton("取消选择")
        clear_button.setObjectName("secondary")
        clear_button.clicked.connect(self.clear_selection)
        self.clean_button = QPushButton("清理选中文件")
        self.clean_button.setObjectName("danger")
        self.clean_button.setEnabled(False)
        self.clean_button.clicked.connect(self.clean_selected)
        close_button = QPushButton("关闭")
        close_button.setObjectName("secondary")
        close_button.clicked.connect(self.accept)
        action_row.addWidget(self.open_button)
        action_row.addWidget(clear_button)
        action_row.addWidget(self.clean_button)
        action_row.addWidget(close_button)
        self.clear_button = clear_button
        self.close_button = close_button
        layout.addLayout(action_row)

    def _make_tree(self):
        tree = QTreeWidget()
        tree.setHeaderLabels(["文件/分组", "大小", "状态", "修改时间", "路径"])
        tree.setColumnWidth(0, 280)
        tree.setColumnWidth(1, 110)
        tree.setColumnWidth(2, 110)
        tree.setColumnWidth(3, 150)
        tree.itemChanged.connect(self._selection_changed)
        tree.itemDoubleClicked.connect(lambda *_: self.open_selected_location())
        return tree

    def refresh_drives(self):
        current_path = self.current_drive_path()
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()
        drives = self.cleaner.get_available_drives()
        selected_index = 0
        for index, drive in enumerate(drives):
            suffix = "可移动磁盘" if drive.get('removable') else "本地磁盘"
            text = (
                f"{drive['name']}  {suffix}  "
                f"可用 {format_size(drive['free'])} / {format_size(drive['total'])}"
            )
            self.drive_combo.addItem(text, drive)
            if drive['path'] == current_path:
                selected_index = index
        if drives:
            self.drive_combo.setCurrentIndex(selected_index)
        self.drive_combo.blockSignals(False)
        self.scan_button.setEnabled(bool(drives))
        self._drive_changed()

    def current_drive_info(self):
        return self.drive_combo.currentData() or {}

    def current_drive_path(self):
        return self.current_drive_info().get('path', '')

    def _drive_changed(self):
        drive = self.current_drive_info()
        if not drive:
            self.disk_summary.setText("未检测到可用的其它固定磁盘或可移动磁盘。")
            self._clear_results()
            return
        self.disk_summary.setText(
            f"{drive['name']}  总容量 {format_size(drive['total'])} · "
            f"已用 {format_size(drive['used'])} ({drive['percent']}%) · "
            f"可用 {format_size(drive['free'])}"
        )
        if self.results and self.results.get('scan_root') != os.path.realpath(drive['path']):
            self._clear_results()

    def _clear_results(self):
        self.results = {}
        self.large_tree.clear()
        self.duplicate_tree.clear()
        self.tabs.setTabText(0, "大文件 (0)")
        self.tabs.setTabText(1, "重复文件 (0 组)")
        self.scan_summary.setText("尚未扫描")
        self._update_selected_summary()

    def _set_busy(self, busy, scanning=False):
        self._busy = busy
        for widget in (
                self.drive_combo, self.drive_refresh_button, self.large_checkbox,
                self.large_limit, self.duplicate_checkbox, self.duplicate_limit,
                self.scan_button, self.simulate_checkbox, self.backup_checkbox,
                self.clear_button, self.open_button):
            widget.setEnabled(not busy)
        self.stop_button.setVisible(scanning)
        self.stop_button.setEnabled(scanning)
        self.close_button.setEnabled(not busy)
        self.clean_button.setEnabled(not busy and bool(self._collect_selected()))
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def start_scan(self):
        root = self.current_drive_path()
        if not root:
            return
        if not self.large_checkbox.isChecked() and not self.duplicate_checkbox.isChecked():
            QMessageBox.warning(self, "未选择扫描类型", "请至少选择大文件或重复文件。")
            return
        self._clear_results()
        options = {
            'scan_root': root,
            'min_large_size': self.large_limit.value() * 1024 * 1024,
            'min_duplicate_size': self.duplicate_limit.value() * 1024 * 1024,
            'find_large': self.large_checkbox.isChecked(),
            'find_duplicates': self.duplicate_checkbox.isChecked(),
        }
        self._scan_thread = StorageScanThread(self.cleaner, options)
        self._scan_thread.progress.connect(self._scan_progress)
        self._scan_thread.finished.connect(self._scan_finished)
        self._scan_thread.failed.connect(self._scan_failed)
        self._set_busy(True, scanning=True)
        self._scan_start_time = time.time()
        self._scan_file_count = 0
        self.scan_button.setText("扫描中…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText(f"正在扫描 {root}，请稍候…")
        # 定时刷新预估时间
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_scan_elapsed)
        self._elapsed_timer.start(1000)
        self._scan_thread.start()

    def stop_scan(self):
        if hasattr(self, '_scan_thread') and self._scan_thread.isRunning():
            self._scan_thread.request_stop()
            self.stop_button.setEnabled(False)
            self.status.setText("正在停止扫描，请稍候…")

    def _scan_progress(self, path, count):
        self._scan_file_count = count
        elapsed = time.time() - self._scan_start_time
        rate = count / elapsed if elapsed > 1 else 0
        rate_str = f"  ({int(rate)} 文件/秒）" if rate > 0 else ""
        self.status.setText(
            f"已扫描 {count} 个文件{rate_str}：{os.path.basename(path) or path}")

    def _update_scan_elapsed(self):
        if not hasattr(self, '_scan_start_time'):
            return
        elapsed = int(time.time() - self._scan_start_time)
        minutes, seconds = divmod(elapsed, 60)
        time_str = f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"
        count = getattr(self, '_scan_file_count', 0)
        self.progress.setFormat(f"已耗时 {time_str}，已扫描 {count} 个文件")
        self.progress.setTextVisible(True)

    def _scan_finished(self, results):
        self._stop_elapsed_timer()
        self._set_busy(False)
        self.scan_button.setText("开始扫描")
        self.progress.setVisible(False)
        self.results = results
        self._populate_results(results)
        elapsed = int(time.time() - self._scan_start_time)
        minutes, seconds = divmod(elapsed, 60)
        time_str = f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"
        if results.get('aborted'):
            self.status.setText(f"扫描已停止（耗时 {time_str}），当前显示停止前已完成的大文件结果。")
        else:
            self.status.setText(f"扫描完成（耗时 {time_str}，共 {results.get('scanned_files', 0)} 个文件）。所有结果默认未选中，请核对路径后再清理。")

    def _scan_failed(self, error):
        self._stop_elapsed_timer()
        self._set_busy(False)
        self.scan_button.setText("开始扫描")
        self.progress.setVisible(False)
        self.status.setText("扫描失败。")
        QMessageBox.critical(self, "扫描失败", error)

    def _stop_elapsed_timer(self):
        if hasattr(self, '_elapsed_timer') and self._elapsed_timer.isActive():
            self._elapsed_timer.stop()

    def _populate_results(self, results):
        self.large_tree.blockSignals(True)
        self.duplicate_tree.blockSignals(True)
        self.large_tree.clear()
        self.duplicate_tree.clear()
        for item in results.get('large_files', []):
            node = QTreeWidgetItem([
                os.path.basename(item['path']) or item['path'],
                format_size(item['size']),
                "待处理",
                item.get('modified', ''),
                item['path'],
            ])
            node.setFlags(node.flags() | Qt.ItemIsUserCheckable)
            node.setCheckState(0, Qt.Unchecked)
            node.setData(0, Qt.UserRole, item)
            node.setForeground(0, QColor('#b04'))
            self.large_tree.addTopLevelItem(node)
        for group in results.get('duplicate_groups', []):
            parent = QTreeWidgetItem([
                f"重复文件组 ({group['count']} 份，可释放 {format_size(group['reclaimable_size'])})",
                format_size(group['size']),
                "",
                "",
                "",
            ])
            for item in group['files']:
                label = os.path.basename(item['path']) or item['path']
                if item.get('recommended_keep'):
                    label += "  （建议保留）"
                node = QTreeWidgetItem(parent, [
                    label,
                    format_size(item['size']),
                    "待处理",
                    item.get('modified', ''),
                    item['path'],
                ])
                node.setFlags(node.flags() | Qt.ItemIsUserCheckable)
                node.setCheckState(0, Qt.Unchecked)
                node.setData(0, Qt.UserRole, item)
                if item.get('recommended_keep'):
                    node.setForeground(0, QColor('#1f7a45'))
            self.duplicate_tree.addTopLevelItem(parent)
            parent.setExpanded(True)
        self.large_tree.blockSignals(False)
        self.duplicate_tree.blockSignals(False)
        large_count = len(results.get('large_files', []))
        duplicate_groups = results.get('duplicate_groups', [])
        duplicate_reclaimable = sum(
            group['reclaimable_size'] for group in duplicate_groups)
        self.tabs.setTabText(0, f"大文件 ({large_count})")
        self.tabs.setTabText(1, f"重复文件 ({len(duplicate_groups)} 组)")
        self._base_scan_summary = (
            f"已检查 {results.get('scanned_files', 0)} 个文件，"
            f"共 {format_size(results.get('scanned_size', 0))}；"
            f"重复文件最多可释放 {format_size(duplicate_reclaimable)}"
        )
        self.scan_summary.setText(self._base_scan_summary)
        self._update_selected_summary()

    @staticmethod
    def _data_items(tree):
        stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        while stack:
            node = stack.pop()
            data = node.data(0, Qt.UserRole)
            if data:
                yield node, data
            stack.extend(node.child(i) for i in range(node.childCount()))

    def _collect_selected(self):
        selected = {}
        for tree in (self.large_tree, self.duplicate_tree):
            for node, data in self._data_items(tree):
                if node.checkState(0) != Qt.Checked:
                    continue
                if data.get('deleted'):
                    continue
                key = os.path.normcase(os.path.realpath(data['path']))
                current = selected.get(key)
                if current is None or data.get('duplicate_group'):
                    selected[key] = data
        return list(selected.values())

    def _selection_changed(self, _item, column):
        if column == 0:
            self._update_selected_summary()

    def _update_selected_summary(self):
        selected = self._collect_selected()
        total = sum(item['size'] for item in selected)
        self.selected_summary.setText(
            f"已选 {len(selected)} 个文件，预计释放 {format_size(total)}")
        self.clean_button.setEnabled(not self._busy and bool(selected))

    def clear_selection(self):
        for tree in (self.large_tree, self.duplicate_tree):
            tree.blockSignals(True)
            for node, _ in self._data_items(tree):
                node.setCheckState(0, Qt.Unchecked)
            tree.blockSignals(False)
        self._update_selected_summary()

    def _active_item_data(self):
        tree = self.large_tree if self.tabs.currentIndex() == 0 else self.duplicate_tree
        item = tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def open_selected_location(self):
        data = self._active_item_data()
        if not data or not os.path.exists(data['path']):
            return
        try:
            subprocess.Popen(['explorer.exe', '/select,', os.path.normpath(data['path'])])
        except OSError as e:
            QMessageBox.warning(self, "无法打开位置", str(e))

    @staticmethod
    def _duplicate_violation(items):
        groups = {}
        for item in items:
            group_id = item.get('duplicate_group')
            count = int(item.get('duplicate_count', 0) or 0)
            if group_id and count > 1:
                group = groups.setdefault(group_id, {'expected': count, 'paths': set()})
                group['paths'].add(os.path.normcase(os.path.realpath(item['path'])))
        return any(len(group['paths']) >= group['expected'] for group in groups.values())

    def clean_selected(self):
        items = self._collect_selected()
        if not items:
            return
        if self._duplicate_violation(items):
            QMessageBox.warning(
                self,
                "必须保留一个文件",
                "重复文件组不能全部删除，请取消勾选组内至少一个文件。",
            )
            return
        root = self.results.get('scan_root', '')
        simulate = self.simulate_checkbox.isChecked()
        backup = self.backup_checkbox.isChecked()
        if not simulate and backup and self.cleaner.same_volume(root, self.cleaner.backup_dir):
            QMessageBox.warning(
                self,
                "备份目录位于目标磁盘",
                "当前备份目录与目标磁盘相同，备份后不会真正释放该磁盘空间。\n"
                "请先在主界面的“备份管理”中改到其它磁盘，或关闭删除前备份。",
            )
            return
        total = sum(item['size'] for item in items)
        if simulate:
            text = f"【模拟】将预览永久删除 {len(items)} 个文件，约 {format_size(total)}。"
        elif backup:
            text = (
                f"将从 {root} 永久删除 {len(items)} 个文件，约 {format_size(total)}。\n"
                f"删除前会备份到：{self.cleaner.backup_dir}"
            )
        else:
            text = (
                f"将从 {root} 永久删除 {len(items)} 个文件，约 {format_size(total)}。\n"
                "当前未启用备份，删除后无法从回收站恢复。"
            )
        text += "\n\n这些可能是个人文件，请逐项确认路径后再继续。"
        if QMessageBox.question(self, "确认其它磁盘清理", text) != QMessageBox.Yes:
            return
        self.cleaner.set_options({'simulate': simulate, 'backup': backup})
        self._clean_items = list(items)
        self._clean_simulate = simulate
        self._clean_thread = StorageCleanThread(self.cleaner, items)
        self._clean_thread.progress.connect(self._clean_progress)
        self._clean_thread.finished.connect(self._clean_finished)
        self._set_busy(True)
        self.clean_button.setText("清理中…")
        self.progress.setVisible(True)
        self.progress.setRange(0, len(items))
        self.progress.setValue(0)
        self.status.setText(f"正在处理选中的 {len(items)} 个文件…")
        self._clean_thread.start()

    def _clean_progress(self, path, index):
        self.progress.setValue(index)
        total = self.progress.maximum()
        self.status.setText(f"正在处理 ({index}/{total})：{os.path.basename(path) or path}")

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.realpath(path))

    @staticmethod
    def _disable_item(node):
        node.setCheckState(0, Qt.Unchecked)
        node.setFlags(node.flags() & ~Qt.ItemIsUserCheckable)

    def _mark_deleted(self, node, data, status="已删除"):
        data['deleted'] = True
        node.setData(0, Qt.UserRole, data)
        self._disable_item(node)
        node.setText(2, status)
        node.setToolTip(2, "文件已从目标磁盘删除。")
        font = node.font(0)
        font.setStrikeOut(True)
        node.setFont(0, font)
        for column in range(node.columnCount()):
            node.setForeground(column, QColor('#8a93a6'))

    @staticmethod
    def _mark_failed(node, error, simulate=False):
        node.setCheckState(0, Qt.Unchecked)
        node.setText(2, "模拟失败" if simulate else "删除失败")
        node.setToolTip(2, error)
        node.setForeground(2, QColor('#d33'))

    def _refresh_duplicate_groups(self):
        processed_groups = 0
        active_groups = 0
        for index in range(self.duplicate_tree.topLevelItemCount()):
            parent = self.duplicate_tree.topLevelItem(index)
            remaining = []
            deleted_count = 0
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                data = child.data(0, Qt.UserRole)
                if not data:
                    continue
                if data.get('deleted'):
                    deleted_count += 1
                else:
                    remaining.append((child, data))
            remaining_count = len(remaining)
            size = remaining[0][1]['size'] if remaining else 0
            reclaimable = size * max(0, remaining_count - 1)
            parent.setText(
                0,
                f"重复文件组 (剩余 {remaining_count} 份，可释放 {format_size(reclaimable)})",
            )
            if deleted_count:
                processed_groups += 1
                parent.setText(2, f"已删除 {deleted_count} 份")
            if remaining_count > 1:
                active_groups += 1
            for child, data in remaining:
                data['duplicate_count'] = remaining_count
                child.setData(0, Qt.UserRole, data)
                if remaining_count <= 1:
                    self._disable_item(child)
                    child.setText(2, "唯一保留文件")
                    child.setToolTip(2, "重复组只剩这一份，已禁止继续删除。")
                    child.setForeground(0, QColor('#1f7a45'))
        self.tabs.setTabText(
            1, f"重复文件 ({active_groups} 组可处理，{processed_groups} 组已更新)")

    def _apply_clean_result(self, result, simulate):
        cleaned_paths = {
            self._path_key(path) for path in result.get('cleaned_items', [])
        }
        error_by_path = {
            self._path_key(error.get('path', '')): error.get('error', '处理失败')
            for error in result.get('errors', [])
            if error.get('path')
        }
        selected_paths = {
            self._path_key(item['path'])
            for item in getattr(self, '_clean_items', [])
        }
        deleted_unique = set()
        for tree in (self.large_tree, self.duplicate_tree):
            tree.blockSignals(True)
            for node, data in self._data_items(tree):
                key = self._path_key(data['path'])
                if key not in selected_paths:
                    continue
                if key in error_by_path:
                    self._mark_failed(node, error_by_path[key], simulate)
                elif simulate and key in cleaned_paths:
                    node.setCheckState(0, Qt.Unchecked)
                    node.setText(2, "模拟可删除")
                    node.setToolTip(2, "模拟模式未删除文件。")
                    node.setForeground(2, QColor('#4f7cff'))
                elif key in cleaned_paths:
                    self._mark_deleted(node, data)
                    deleted_unique.add(key)
                elif not os.path.exists(data['path']):
                    self._mark_deleted(node, data, "文件已不存在")
                    deleted_unique.add(key)
                else:
                    node.setCheckState(0, Qt.Unchecked)
                    node.setText(2, "未处理")
            tree.blockSignals(False)
        if not simulate:
            self._refresh_duplicate_groups()
            remaining_large = sum(
                1 for _, data in self._data_items(self.large_tree)
                if not data.get('deleted'))
            total_large = sum(1 for _ in self._data_items(self.large_tree))
            self.tabs.setTabText(
                0, f"大文件 (剩余 {remaining_large} / 共 {total_large})")
        self._update_selected_summary()
        base_summary = getattr(self, '_base_scan_summary', self.scan_summary.text())
        action = "模拟检查" if simulate else "本次已删除"
        self.scan_summary.setText(
            f"{base_summary}；{action} {len(deleted_unique) if not simulate else len(selected_paths)} 个文件，"
            f"{format_size(result.get('freed_space', 0))}")

    def _clean_finished(self, result):
        self._set_busy(False)
        self.clean_button.setText("清理选中文件")
        self.progress.setVisible(False)
        freed = result.get('freed_space', 0)
        errors = result.get('errors', [])
        simulate = getattr(
            self, '_clean_simulate', self.simulate_checkbox.isChecked())
        self._apply_clean_result(result, simulate)
        title = "模拟完成" if simulate else "清理完成"
        verb = "预计可释放" if simulate else "已释放"
        message = f"{verb} {format_size(freed)}。"
        if errors:
            details = "\n".join(
                f"{error.get('path', '')}: {error.get('error', '')}"
                for error in errors[:10]
            )
            message += f"\n有 {len(errors)} 个文件未处理。\n\n{details}"
            QMessageBox.warning(self, title, message)
        else:
            QMessageBox.information(self, title, message)
        self.status.setText(message.splitlines()[0])

    def closeEvent(self, event):
        if hasattr(self, '_clean_thread') and self._clean_thread.isRunning():
            QMessageBox.information(self, "正在清理", "清理完成前不能关闭窗口。")
            event.ignore()
            return
        if hasattr(self, '_scan_thread') and self._scan_thread.isRunning():
            self.stop_scan()
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self):
        if hasattr(self, '_clean_thread') and self._clean_thread.isRunning():
            QMessageBox.information(self, "正在清理", "清理完成前不能关闭窗口。")
            return
        if hasattr(self, '_scan_thread') and self._scan_thread.isRunning():
            self.stop_scan()
            return
        super().reject()
