#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
C盘清理工具 - 现代化界面 (PySide6)
复用已修复的 CleanerLogic 引擎，提供卖相更好的产品化界面。
"""
import os
import subprocess
import sys
import time
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QCheckBox, QTreeWidget, QTreeWidgetItem,
    QMessageBox, QFrame, QSizePolicy, QProgressDialog,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont, QIcon, QColor

from cleaner_logic import (
    ANALYSIS_ONLY_CATEGORIES,
    DISABLED_CLEANUP_CATEGORIES,
    CleanerLogic,
)
from settings import load_settings, save_settings
from backup_manager import BackupManagerDialog
from storage_cleaner import StorageCleanerDialog
from elevate import is_admin, relaunch_as_admin
from version import APP_VERSION

APP_TITLE = f"C盘清理工具 Pro v{APP_VERSION}"
# 高风险类别：默认不勾选，用户明确选择后才进入“清理选中项”。
DANGEROUS = {'downloads'}


def resource_path(rel):
    """兼容开发环境与 PyInstaller 打包后的资源路径。"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _qss_url(rel):
    """QSS url() 里 Windows 路径必须用正斜杠。"""
    return resource_path(rel).replace('\\', '/')

CATEGORIES = {
    'temp': "临时文件", 'recycle': "回收站", 'cache': "浏览器缓存",
    'logs': "系统日志", 'updates': "Windows更新缓存", 'thumbnails': "缩略图缓存",
    'prefetch': "预读取文件", 'old_windows': "旧Windows文件", 'error_reports': "错误报告",
    'service_packs': "服务包备份", 'memory_dumps': "内存转储文件", 'font_cache': "字体缓存",
    'disk_cleanup': "磁盘清理备份", 'app_cache': "应用程序缓存", 'media_cache': "媒体播放器缓存",
    'search_index': "搜索索引临时文件", 'backup_temp': "备份临时文件", 'update_temp': "更新临时文件",
    'driver_backup': "驱动备份", 'app_crash': "应用程序崩溃转储", 'app_logs': "应用程序日志",
    'recent_items': "最近使用的文件列表", 'notification': "Windows通知缓存", 'dns_cache': "DNS缓存",
    'printer_temp': "打印机临时文件", 'device_temp': "设备临时文件",
    'windows_defender': "Windows Defender缓存", 'store_cache': "Windows Store缓存",
    'onedrive_cache': "OneDrive缓存", 'downloads': "下载文件夹 ⚠高风险",
    'installer_cache': "安装程序缓存", 'delivery_opt': "Windows传递优化缓存",
    'ide_cache': "IDE开发工具缓存", 'dev_pkg_cache': "开发包管理器缓存",
    'ai_cache': "AI应用缓存", 'ai_models': "AI模型文件 (仅供查看)",
    'messaging_cache': "通讯应用缓存", 'browser_extra': "其它浏览器缓存",
    'gaming_cache': "游戏娱乐缓存", 'tool_cache': "办公工具缓存",
    'docker_data': "Docker数据 (仅供查看)",
    'large_files': "大文件 (>100MB，仅供查看)",
}


def is_category_cleanable(category):
    return category not in ANALYSIS_ONLY_CATEGORIES | DISABLED_CLEANUP_CATEGORIES


def category_default_checked(category):
    return is_category_cleanable(category) and category not in DANGEROUS


def collect_one_click_items(results):
    return [
        item
        for category, items in results.items()
        if is_category_cleanable(category)
        for item in items
    ]


def cleanable_size(results):
    return sum(item['size'] for item in collect_one_click_items(results))

QSS = """
QMainWindow, QWidget { background: #f4f6fb; font-family: "Microsoft YaHei", "Segoe UI"; }
#card { background: #ffffff; border-radius: 12px; border: 1px solid #e6e9f2; }
#title { font-size: 20px; font-weight: 700; color: #1b2233; }
#badgeAdmin { background:#e7f6ec; color:#1f9d4d; border-radius:9px; padding:2px 10px; font-size:12px; font-weight:600; }
#badgeUser { background:#fdeaea; color:#d33; border-radius:9px; padding:2px 10px; font-size:12px; font-weight:600; }
#bigNum { font-size: 34px; font-weight: 800; color: #4f7cff; }
#bigNumLabel { font-size: 12px; color: #8a93a6; }
#diskLabel { font-size: 13px; color: #3a4356; }
QPushButton { background:#4f7cff; color:#fff; border:none; border-radius:8px;
              padding:9px 18px; font-size:14px; font-weight:600; }
QPushButton:hover { background:#3f6ae6; }
QPushButton:disabled { background:#c4ccdd; }
QPushButton#secondary { background:#eef1f8; color:#33415c; }
QPushButton#secondary:hover { background:#e2e7f3; }
QPushButton#danger { background:#e5484d; }
QPushButton#danger:hover { background:#d13438; }
QTreeWidget { background:#fff; border:1px solid #e6e9f2; border-radius:12px; font-size:13px; }
QTreeWidget::item { padding:5px; min-height: 22px; }
QTreeWidget::indicator { width: 18px; height: 18px; margin-right: 6px; border-radius: 4px; }
QTreeWidget::indicator:unchecked { border: 2px solid #c4ccdd; background: #fff; }
QTreeWidget::indicator:checked { border: 2px solid #4f7cff; background: #4f7cff; image: url("{{CHECK_IMG}}"); }
QTreeWidget::indicator:indeterminate { border: 2px solid #4f7cff; background: #4f7cff; image: url("{{DASH_IMG}}"); }
QProgressBar { border:none; border-radius:6px; background:#e9edf5; height:12px; text-align:center; color:#3a4356; }
QProgressBar::chunk { border-radius:6px; background:#4f7cff; }
QCheckBox { font-size:13px; color:#33415c; }
#status { color:#8a93a6; font-size:12px; }
"""


def fmt_size(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024 or unit == 'TB':
            return f"{n:.2f} {unit}" if unit != 'B' else f"{int(n)} B"
        n /= 1024


class ScanThread(QThread):
    progress = Signal(str, int, int)  # name, done, total
    finished = Signal(dict)
    paused = Signal(dict, object)    # 部分结果，以及确定已完整扫描的类别
    stopped = Signal()               # 用户点了停止

    def __init__(self, cleaner, skip=None):
        super().__init__()
        self.cleaner = cleaner
        self.skip = skip or set()
        self._abort = False
        self._pause = False

    def run(self):
        completed_categories = set()

        def cb(name, done, total):
            self.progress.emit(name, done, total)

        def abort():
            return self._abort or self._pause

        results = self.cleaner.scan_system(
            progress_callback=cb,
            abort_callback=abort,
            skip_categories=self.skip,
            completed_callback=completed_categories.add,
        )
        if self._abort:
            self.stopped.emit()
        elif self._pause:
            self.paused.emit(results, completed_categories)
        else:
            self.finished.emit(results)

    def request_stop(self):
        self._abort = True

    def request_pause(self):
        self._pause = True


class CleanThread(QThread):
    progress = Signal(str, int)
    finished = Signal(dict)

    def __init__(self, cleaner, items):
        super().__init__()
        self.cleaner = cleaner
        self.items = items

    def run(self):
        res = self.cleaner.clean_selected(self.items, self.progress)
        self.finished.emit(res)


class UpdateCheckThread(QThread):
    """后台检查更新线程（网络请求由 updater.check_update 完成）。"""
    update_available = Signal(dict)  # emit update info dict
    no_update = Signal(str)  # emit reason
    check_failed = Signal(str)

    def run(self):
        import traceback
        try:
            from updater import check_update, UpdateError
            info = check_update()  # 网络失败抛 UpdateError，无更新返回 None
            if info:
                self.update_available.emit(info)
            else:
                from version import APP_VERSION
                self.no_update.emit(f'local={APP_VERSION} (remote not newer)')
        except Exception as e:
            self.check_failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")


class ModernCleanerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cleaner = CleanerLogic()

        # 恢复用户设置
        self._user_settings = load_settings()
        if self._user_settings.get('backup_dir'):
            self.cleaner.set_options({'backup_dir': self._user_settings['backup_dir']})
        self.cleaner.set_options({
            'max_backups': self._user_settings.get('max_backups', 5),
            'max_backup_size': self._user_settings.get('max_backup_size_mb', 20480) * 1024 * 1024,
        })

        self.scan_results = {}
        self._completed_scan_categories = set()
        self._restore_paused_controls_after_clean = False
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(860, 640)
        self._build_ui()

        self.simulate_cb.setChecked(self._user_settings.get('simulate', True))
        self.backup_cb.setChecked(self._user_settings.get('backup', True))

        self._refresh_disk()
        self._set_big(0)

        # 上次自动更新的遗留清理；删除了 .old 说明这是一次成功更新后的首次启动
        from updater import cleanup_after_update
        if cleanup_after_update():
            from version import APP_VERSION
            self.statusBar().showMessage(f"已成功更新到 v{APP_VERSION}", 10000)

        # 启动后延迟 3 秒自动检查更新
        if self._user_settings.get('check_updates', True):
            QTimer.singleShot(3000, self._auto_check_update)

    # ---------- UI ----------
    def _build_ui(self):
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        # Header
        header = QHBoxLayout()
        t = QLabel(APP_TITLE)
        t.setObjectName("title")
        header.addWidget(t)
        header.addStretch()
        badge = QLabel("管理员模式" if is_admin() else "普通模式")
        badge.setObjectName("badgeAdmin" if is_admin() else "badgeUser")
        header.addWidget(badge)
        v.addLayout(header)

        # Dashboard card
        card = QFrame()
        card.setObjectName("card")
        ch = QHBoxLayout(card)
        ch.setContentsMargins(18, 14, 18, 14)
        left = QVBoxLayout()
        self.disk_label = QLabel("C盘使用情况：读取中…")
        self.disk_label.setObjectName("diskLabel")
        self.disk_bar = QProgressBar()
        left.addWidget(self.disk_label)
        left.addWidget(self.disk_bar)
        ch.addLayout(left, 3)
        right = QVBoxLayout()
        right.setAlignment(Qt.AlignCenter)
        self.big_num = QLabel("0 MB")
        self.big_num.setObjectName("bigNum")
        self.big_num.setAlignment(Qt.AlignCenter)
        cap = QLabel("可释放空间")
        cap.setObjectName("bigNumLabel")
        cap.setAlignment(Qt.AlignCenter)
        right.addWidget(self.big_num)
        right.addWidget(cap)
        ch.addLayout(right, 1)
        v.addWidget(card)

        # Buttons
        brow = QHBoxLayout()
        self.scan_btn = QPushButton("扫描系统")
        self.scan_btn.clicked.connect(self.start_scan)
        self.clean_sel_btn = QPushButton("清理选中项")
        self.clean_sel_btn.setObjectName("secondary")
        self.clean_sel_btn.setEnabled(False)
        self.clean_sel_btn.clicked.connect(self.clean_selected)
        self.clean_all_btn = QPushButton("一键清理")
        self.clean_all_btn.setObjectName("danger")
        self.clean_all_btn.setEnabled(False)
        self.clean_all_btn.clicked.connect(self.clean_all)
        self.storage_btn = QPushButton("其它磁盘清理")
        self.storage_btn.setObjectName("secondary")
        self.storage_btn.clicked.connect(self.open_storage_cleaner)
        brow.addWidget(self.scan_btn)
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setObjectName("secondary")
        self.pause_btn.setVisible(False)
        self.pause_btn.clicked.connect(self._on_pause)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._request_stop)
        brow.addWidget(self.pause_btn)
        brow.addWidget(self.stop_btn)
        brow.addWidget(self.clean_sel_btn)
        brow.addWidget(self.clean_all_btn)
        brow.addWidget(self.storage_btn)
        brow.addStretch()
        v.addLayout(brow)

        # Progress + status
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        v.addWidget(self.progress)
        self.status = QLabel("就绪。建议首次使用开启“模拟模式”预览。")
        self.status.setObjectName("status")
        v.addWidget(self.status)

        # Result tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["项目", "大小", "路径"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 90)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.itemChanged.connect(self._on_item_changed)
        tree_actions = QHBoxLayout()
        select_all_btn = QPushButton("全选可清理项")
        select_all_btn.setObjectName("secondary")
        select_all_btn.clicked.connect(self._select_all_cleanable)
        deselect_all_btn = QPushButton("取消全选")
        deselect_all_btn.setObjectName("secondary")
        deselect_all_btn.clicked.connect(self._deselect_all)
        tree_actions.addWidget(select_all_btn)
        tree_actions.addWidget(deselect_all_btn)
        tree_actions.addStretch()
        v.addLayout(tree_actions)
        v.addWidget(self.tree, 1)

        # Options
        orow = QHBoxLayout()
        self.simulate_cb = QCheckBox("模拟模式（不实际删除）")
        self.simulate_cb.setChecked(True)
        self.backup_cb = QCheckBox("删除前备份")
        self.backup_cb.setChecked(True)
        self.backup_btn = QPushButton("备份管理")
        self.backup_btn.setObjectName("secondary")
        self.backup_btn.clicked.connect(self.open_backup_manager)
        orow.addWidget(self.simulate_cb)
        orow.addWidget(self.backup_cb)
        orow.addWidget(self.backup_btn)
        orow.addStretch()
        self.update_btn = QPushButton("检查更新")
        self.update_btn.setObjectName("secondary")
        self.update_btn.clicked.connect(self._manual_check_update)
        orow.addWidget(self.update_btn)
        v.addLayout(orow)

        self.setCentralWidget(root)
        qss = (QSS
               .replace('{{CHECK_IMG}}', _qss_url('icons/check_white.png'))
               .replace('{{DASH_IMG}}', _qss_url('icons/dash_white.png')))
        self.setStyleSheet(qss)

    # ---------- helpers ----------
    def _refresh_disk(self):
        info = self.cleaner.get_disk_info()
        self.disk_label.setText(
            f"C盘  总 {info['total']:.1f} GB · 已用 {info['used']:.1f} GB ({info['percent']}%) · 可用 {info['free']:.1f} GB")
        self.disk_bar.setRange(0, 100)
        self.disk_bar.setValue(int(info['percent']))

    def _set_big(self, nbytes):
        self.big_num.setText(fmt_size(nbytes))

    def _refresh_selected_size(self):
        """根据当前勾选项实时更新可释放空间。"""
        self._set_big(sum(item['size'] for item in self._collect_checked()))

    def _set_busy(self, busy, mode=None):
        self.scan_btn.setEnabled(not busy)
        self.clean_sel_btn.setEnabled(not busy and self._has_checked())
        self.clean_all_btn.setEnabled(
            not busy and bool(collect_one_click_items(self.scan_results)))
        self.backup_btn.setEnabled(not busy)
        self.storage_btn.setEnabled(not busy)
        if busy and mode == 'scan':
            self.scan_btn.setText("扫描中…")
        elif busy and mode == 'clean':
            self.clean_sel_btn.setText("清理中…")
            self.clean_all_btn.setText("清理中…")
        else:
            self.scan_btn.setText("扫描系统")
            self.clean_sel_btn.setText("清理选中项")
            self.clean_all_btn.setText("一键清理")
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def open_backup_manager(self):
        BackupManagerDialog(self.cleaner, self).exec()

    def open_storage_cleaner(self):
        StorageCleanerDialog(
            self.cleaner,
            self,
            simulate=self.simulate_cb.isChecked(),
            backup=self.backup_cb.isChecked(),
        ).exec()

    def _has_checked(self):
        return len(self._collect_checked()) > 0

    def _collect_checked(self):
        items = []
        for i in range(self.tree.topLevelItemCount()):
            cat = self.tree.topLevelItem(i)
            for j in range(cat.childCount()):
                node = cat.child(j)
                if node.checkState(0) == Qt.Checked:
                    data = node.data(0, Qt.UserRole)
                    if data:
                        items.append(data)
        return items

    def _apply_item_style(self, item):
        state = item.checkState(0)
        is_dangerous = item.data(0, Qt.UserRole + 1) is True
        if state == Qt.Checked:
            color = QColor("#d33") if is_dangerous else Qt.black
            bold = True
        elif state == Qt.PartiallyChecked:
            color = QColor("#d33") if is_dangerous else QColor("#4f7cff")
            bold = True
        else:
            color = QColor("#b04") if is_dangerous else QColor("#8a93a6")
            bold = False
        font = item.font(0)
        font.setBold(bold)
        item.setFont(0, font)
        item.setForeground(0, color)

    def _on_item_changed(self, item, col):
        if col != 0:
            return
        self.tree.blockSignals(True)
        try:
            if item.parent() is None:  # 类别 -> 同步子项
                st = item.checkState(0)
                for i in range(item.childCount()):
                    child = item.child(i)
                    child.setCheckState(0, st)
                    self._apply_item_style(child)
            else:
                # 子项变化 -> 更新父节点为全选/半选/未选
                parent = item.parent()
                states = [parent.child(i).checkState(0) for i in range(parent.childCount())]
                if all(s == Qt.Checked for s in states):
                    parent.setCheckState(0, Qt.Checked)
                elif any(s == Qt.Checked or s == Qt.PartiallyChecked for s in states):
                    parent.setCheckState(0, Qt.PartiallyChecked)
                else:
                    parent.setCheckState(0, Qt.Unchecked)
            self._apply_item_style(item)
            if item.parent() is not None:
                self._apply_item_style(item.parent())
        finally:
            self.tree.blockSignals(False)
        self._refresh_selected_size()
        self.clean_sel_btn.setEnabled(self._has_checked())

    def _on_tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        path = data.get('path', '')
        if path and os.path.exists(path):
            action = menu.addAction("打开文件位置")
            action.triggered.connect(lambda: self._open_file_location(path))
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _open_file_location(self, path):
        try:
            subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
        except OSError:
            pass

    def _select_all_cleanable(self):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            cat = self.tree.topLevelItem(i)
            cat_key = None
            for key, label in CATEGORIES.items():
                if label == cat.text(0):
                    cat_key = key
                    break
            if cat_key and is_category_cleanable(cat_key):
                cat.setCheckState(0, Qt.Checked)
                for j in range(cat.childCount()):
                    cat.child(j).setCheckState(0, Qt.Checked)
                self._apply_item_style(cat)
                for j in range(cat.childCount()):
                    self._apply_item_style(cat.child(j))
        self.tree.blockSignals(False)
        self._refresh_selected_size()
        self.clean_sel_btn.setEnabled(self._has_checked())

    def _deselect_all(self):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            cat = self.tree.topLevelItem(i)
            cat.setCheckState(0, Qt.Unchecked)
            for j in range(cat.childCount()):
                cat.child(j).setCheckState(0, Qt.Unchecked)
            self._apply_item_style(cat)
            for j in range(cat.childCount()):
                self._apply_item_style(cat.child(j))
        self.tree.blockSignals(False)
        self._refresh_selected_size()
        self.clean_sel_btn.setEnabled(False)

    # ---------- scan ----------
    def start_scan(self):
        self.tree.clear()
        self.scan_results = {}
        self._completed_scan_categories = set()
        self._set_big(0)
        self._set_busy(True, mode='scan')
        self.progress.setVisible(True)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self._scan_start = time.time()
        self.status.setText("正在初始化扫描…")
        self.pause_btn.setText("暂停")
        self.pause_btn.setVisible(True)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setVisible(True)
        self.stop_btn.setEnabled(True)
        self._scan = ScanThread(self.cleaner)
        self._scan.progress.connect(self._scan_progress)
        self._scan.finished.connect(self._scan_done)
        self._scan.paused.connect(self._scan_paused)
        self._scan.stopped.connect(self._scan_stopped)
        self._scan.start()

    def _scan_progress(self, name, value, total):
        """value/total 为 0~1000 的加权进度，直接换算百分比显示。

        name 为当前真实在执行/刚完成的任务名；大文件扫描期间会持续收到
        “大文件（深度扫描中…）”的推进回调，进度条在慢任务阶段也会持续爬升。
        """
        self.progress.setRange(0, total)
        self.progress.setValue(value)
        pct = int(value * 100 / total)
        elapsed = int(time.time() - self._scan_start)
        self.status.setText(f"正在扫描：{name}（{pct}% · 已耗时 {elapsed} 秒）")

    def _scan_done(self, results):
        self.scan_results = results
        self.progress.setVisible(False)
        self.pause_btn.setVisible(False)
        self.stop_btn.setVisible(False)
        self._populate(results)
        total = sum(item['size'] for item in self._collect_checked())
        self._set_busy(False, mode=None)
        status_text = f"扫描完成，当前选中可释放空间：{fmt_size(total)}。高风险项目默认未选中。"
        if results.get('_large_files_incomplete'):
            status_text += " ⚠ 大文件扫描因超时未完成，结果可能不完整。"
        self.status.setText(status_text)
        self._refresh_disk()

    def _scan_paused(self, results, completed_categories):
        """用户点了暂停：保留已扫到的部分结果，冻结进度，等待继续/停止"""
        self.scan_results = results
        self._completed_scan_categories = set(completed_categories)
        self.pause_btn.setText("继续")
        self.pause_btn.setVisible(True)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setVisible(True)
        self.stop_btn.setEnabled(True)
        self.progress.setVisible(False)
        self._populate(results)
        total = sum(item['size'] for item in self._collect_checked())
        self._set_busy(False, mode=None)
        self.scan_btn.setEnabled(False)  # 暂停态不允许重开全新扫描
        n = sum(len(v) for v in results.values())
        self.status.setText(
            f"已暂停，当前扫描到 {n} 项（约 {fmt_size(total)}）。"
            f"可点“继续”接着扫，或直接清理已扫到的部分。")
        self._refresh_disk()

    def _scan_stopped(self):
        """后台扫描已停止：完全复位。"""
        self.progress.setVisible(False)
        self.pause_btn.setVisible(False)
        self.stop_btn.setVisible(False)
        self._set_busy(False, mode=None)
        self.clean_sel_btn.setEnabled(False)
        self.clean_all_btn.setEnabled(False)
        self.tree.clear()
        self.scan_results = {}
        self._completed_scan_categories = set()
        self._set_big(0)
        self.status.setText("扫描已停止。")
        self._refresh_disk()

    def _request_stop(self):
        """请求运行中的线程停止；暂停态没有活动线程，可立即复位。"""
        if hasattr(self, '_scan') and self._scan.isRunning():
            self._scan.request_stop()
            self.pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.status.setText("正在停止扫描，请稍候…")
            return
        self._scan_stopped()

    def _on_pause(self):
        if self.pause_btn.text() == "暂停":
            # 立即给用户反馈：按钮变"继续"，状态提示正在等待当前任务收尾
            self.pause_btn.setText("继续")
            self.pause_btn.setEnabled(False)
            self.status.setText("正在等待当前扫描任务结束以暂停，请稍候…")
            self._scan.request_pause()
        else:
            self._resume_scan()

    def _resume_scan(self):
        """从已完成的类别继续扫描剩余部分"""
        done_cats = set(self._completed_scan_categories)
        partial = {k: list(v) for k, v in self.scan_results.items()}
        self.pause_btn.setText("暂停")
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self._set_busy(True, mode='scan')
        self.progress.setVisible(True)
        self._scan_start = time.time()
        self.status.setText("继续扫描…")
        self._scan = ScanThread(self.cleaner, skip=done_cats)
        self._scan.progress.connect(self._scan_progress)
        self._scan.finished.connect(
            lambda r: self._on_resume_done(r, partial, done_cats))
        self._scan.paused.connect(
            lambda r, completed: self._on_resume_paused(
                r, completed, partial, done_cats))
        self._scan.stopped.connect(self._scan_stopped)
        self._scan.start()

    @staticmethod
    def _merge_scan_results(partial, results, skipped_categories):
        """保留跳过的完整类别，以本轮结果替换此前的部分类别。"""
        merged = {k: list(v) for k, v in partial.items()}
        for k, v in results.items():
            if k not in skipped_categories:
                merged[k] = list(v)
        return merged

    def _on_resume_done(self, results, partial, skipped_categories):
        merged = self._merge_scan_results(partial, results, skipped_categories)
        self._scan_done(merged)

    def _on_resume_paused(self, results, completed_categories, partial,
                          skipped_categories):
        merged = self._merge_scan_results(partial, results, skipped_categories)
        completed = set(skipped_categories) | set(completed_categories)
        self._scan_paused(merged, completed)

    def _populate(self, results):
        self.tree.blockSignals(True)
        self.tree.clear()
        for cat, items in results.items():
            if not items:
                continue
            dangerous = cat in DANGEROUS
            cleanable = is_category_cleanable(cat)
            checked = Qt.Checked if category_default_checked(cat) else Qt.Unchecked
            csize = sum(it['size'] for it in items)
            cnode = QTreeWidgetItem(self.tree)
            cnode.setText(0, CATEGORIES.get(cat, cat))
            cnode.setText(1, fmt_size(csize))
            if cleanable:
                cnode.setFlags(cnode.flags() | Qt.ItemIsUserCheckable)
            cnode.setCheckState(0, checked)
            if not cleanable:
                cnode.setToolTip(0, "此类别仅用于分析展示，不能执行删除。")
            cnode.setData(0, Qt.UserRole + 1, dangerous)
            self._apply_item_style(cnode)
            for it in items:
                node = QTreeWidgetItem(cnode)
                node.setText(0, os.path.basename(it['path']) or it['path'])
                node.setText(1, fmt_size(it['size']))
                node.setText(2, it['path'])
                if cleanable:
                    node.setFlags(node.flags() | Qt.ItemIsUserCheckable)
                node.setCheckState(0, checked)
                if not cleanable:
                    node.setToolTip(0, "此项目仅供查看。")
                node.setData(0, Qt.UserRole, it)
                node.setData(0, Qt.UserRole + 1, dangerous)
                self._apply_item_style(node)
        self.tree.blockSignals(False)
        self._refresh_selected_size()
        self.clean_sel_btn.setEnabled(self._has_checked())

    # ---------- clean ----------
    def _do_clean(self, items, label):
        items = [it for it in items if is_category_cleanable(it.get('type'))]
        if not items:
            return
        total = sum(it['size'] for it in items)
        simulate = self.simulate_cb.isChecked()
        backup_enabled = self.backup_cb.isChecked()
        dangerous_items = [it for it in items if it.get('type') in DANGEROUS]
        danger_note = ""
        if dangerous_items:
            danger_note = (
                f"\n\n⚠ 包含 {len(dangerous_items)} 个高风险项目（下载文件夹）。"
                "其中可能有用户自己的文件，请确认后再继续。"
            )
        if simulate:
            text = f"【模拟】将预览清理 {len(items)} 项，约 {fmt_size(total)}。"
        elif backup_enabled:
            text = (f"确定清理 {len(items)} 项（约 {fmt_size(total)}）吗？"
                    "每个文件可靠备份并写入恢复清单后才会删除。")
        else:
            text = (f"确定清理 {len(items)} 项（约 {fmt_size(total)}）吗？"
                    "当前未启用备份，删除后可能无法恢复。")
        text += danger_note
        if QMessageBox.question(self, "确认清理", text) != QMessageBox.Yes:
            return
        self._restore_paused_controls_after_clean = (
            not self.pause_btn.isHidden() and self.pause_btn.text() == "继续")
        self.pause_btn.setVisible(False)
        self.stop_btn.setVisible(False)
        self.cleaner.set_options({
            'simulate': simulate,
            'backup': self.backup_cb.isChecked(),
        })
        self._set_busy(True, mode='clean')
        self.progress.setVisible(True)
        self.progress.setRange(0, len(items))
        self.progress.setValue(0)
        self.status.setText(f"正在{label}…")
        self._clean = CleanThread(self.cleaner, items)
        self._clean.progress.connect(self._clean_progress)
        self._clean.finished.connect(self._clean_done)
        self._clean.start()

    def clean_selected(self):
        self._do_clean(self._collect_checked(), "清理选中项")

    def clean_all(self):
        items = collect_one_click_items(self.scan_results)
        self._do_clean(items, "一键清理")

    def _clean_progress(self, path, i):
        self.progress.setValue(i)
        self.status.setText(f"正在清理：{os.path.basename(path)}")

    def _clean_done(self, res):
        self.progress.setVisible(False)
        self._set_busy(False, mode=None)
        freed = res.get('freed_space', 0)
        errors = res.get('errors', [])
        simulate = self.simulate_cb.isChecked()
        title = "模拟完成" if simulate else "清理完成"
        verb = "可释放" if simulate else "已释放"
        msg = f"{verb}空间：{fmt_size(freed)}"
        if errors:
            msg += f"\n有 {len(errors)} 个项目清理失败。"
            details = "\n".join(
                f"{err.get('path', '未知路径')}: {err.get('error', '未知错误')}"
                for err in errors[:10]
            )
            if len(errors) > 10:
                details += f"\n... 以及 {len(errors) - 10} 个其他错误"
            QMessageBox.warning(self, title, f"{msg}\n\n失败详情：\n{details}")
        else:
            if not simulate and self.backup_cb.isChecked():
                msg += "\n\n如需恢复已删除的文件，请点击下方「备份管理」按钮。"
            QMessageBox.information(self, title, msg)
        self.status.setText(f"{title}，{verb} {fmt_size(freed)}")
        self._refresh_disk()
        if not simulate:
            self._restore_paused_controls_after_clean = False
            self.start_scan()
        elif self._restore_paused_controls_after_clean:
            self.pause_btn.setText("继续")
            self.pause_btn.setVisible(True)
            self.pause_btn.setEnabled(True)
            self.stop_btn.setVisible(True)
            self.stop_btn.setEnabled(True)
            self.scan_btn.setEnabled(False)
            self._restore_paused_controls_after_clean = False

    def closeEvent(self, event):
        """退出时保存用户设置。"""
        save_settings({
            'simulate': self.simulate_cb.isChecked(),
            'backup': self.backup_cb.isChecked(),
            'backup_dir': self.cleaner.backup_dir,
            'max_backups': self.cleaner.max_backups,
            'max_backup_size_mb': max(1, self.cleaner.max_backup_size // (1024 * 1024)),
            'check_updates': self._user_settings.get('check_updates', True),
            'skipped_version': self._user_settings.get('skipped_version', ''),
        })
        event.accept()

    # ---------- update ----------
    def _auto_check_update(self):
        """启动时自动检查更新（静默，无网络不提示）。"""
        self._update_thread = UpdateCheckThread()
        self._update_thread.update_available.connect(self._on_update_available)
        self._update_thread.check_failed.connect(lambda e: None)  # 静默失败
        self._update_thread.start()

    def _manual_check_update(self):
        """用户点击检查更新按钮。"""
        self.update_btn.setEnabled(False)
        self.update_btn.setText("检查中…")
        self._update_thread = UpdateCheckThread()
        self._update_thread.update_available.connect(self._on_update_available)
        self._update_thread.no_update.connect(self._on_no_update)
        self._update_thread.check_failed.connect(self._on_update_check_failed)
        self._update_thread.start()

    def _on_no_update(self, reason):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("检查更新")
        from version import APP_VERSION
        QMessageBox.information(self, "检查更新",
                                f"当前已是最新版本 v{APP_VERSION}。")

    def _on_update_check_failed(self, error):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("检查更新")
        QMessageBox.warning(self, "检查更新失败",
                            f"无法连接更新服务器：\n{error[:300]}")

    def _on_update_available(self, info):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("检查更新")
        new_ver = info['version']
        skipped = self._user_settings.get('skipped_version', '')
        if skipped == new_ver:
            return

        changelog = info.get('changelog', '') or '无更新说明'
        msg = (f"发现新版本 v{new_ver}\n\n"
               f"更新内容：\n{changelog[:500]}")
        box = QMessageBox(self)
        box.setWindowTitle("发现新版本")
        box.setText(msg)
        btn_update = box.addButton("立即更新", QMessageBox.AcceptRole)
        box.addButton("稍后提醒", QMessageBox.RejectRole)
        btn_skip = box.addButton("跳过此版本", QMessageBox.DestructiveRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked == btn_update:
            self._start_download(info)
        elif clicked == btn_skip:
            self._user_settings['skipped_version'] = new_ver

    def _start_download(self, info):
        """下载更新并显示进度。"""
        from updater import download_update

        url = info['download_url']
        expected_sha256 = info.get('sha256')

        class DownloadThread(QThread):
            progress_update = Signal(int)
            done = Signal(str)
            error = Signal(str)

            def __init__(self, url, expected_sha256=None):
                super().__init__()
                self.url = url
                self.expected_sha256 = expected_sha256

            def run(self):
                try:
                    def cb(downloaded, total):
                        if total > 0:
                            pct = min(int(downloaded * 100 / total), 100)
                            self.progress_update.emit(pct)
                    path = download_update(self.url, cb, self.expected_sha256)
                    self.done.emit(path)
                except Exception as e:
                    self.error.emit(str(e))

        self._dl_thread = DownloadThread(url, expected_sha256)

        def on_progress(pct):
            self.statusBar().showMessage(f"正在下载更新… {pct}%")

        def on_done(path):
            self.statusBar().showMessage("下载完成")
            reply = QMessageBox.question(
                self, "下载完成",
                f"新版本 v{info['version']} 已下载完成。\n点击'是'将关闭程序并自动完成更新。",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._update_path = path
                QTimer.singleShot(200, self._do_apply_update)

        def on_error(err):
            self.statusBar().showMessage("")
            QMessageBox.warning(self, "下载失败", f"更新下载失败：\n{err}")

        self._dl_thread.progress_update.connect(on_progress)
        self._dl_thread.done.connect(on_done)
        self._dl_thread.error.connect(on_error)
        self._dl_thread.start()

    def _do_apply_update(self):
        """延迟执行更新替换，确保 Qt 事件循环已完成对话框关闭。"""
        from updater import apply_update
        self.statusBar().showMessage("正在应用更新…")
        ok = apply_update(self._update_path)
        if not ok:
            # apply_update 失败时已自动回滚，当前版本仍可运行
            self.statusBar().showMessage("")
            QMessageBox.critical(
                self, "更新失败",
                "自动更新未能完成（程序已回滚到当前版本）。\n"
                f"可前往 GitHub 发布页手动下载新版：\n"
                f"https://github.com/qinguabao/windows_CC/releases")


def main():
    # 非管理员则尝试提权重启（打包后 exe 也适用）；失败则普通模式继续
    if not relaunch_as_admin():
        return
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    ico = resource_path(os.path.join('icons', 'cleaner.ico'))
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    w = ModernCleanerWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
