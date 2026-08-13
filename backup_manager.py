#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PySide6 备份管理对话框。"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
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


class BackupManagerDialog(QDialog):
    """查看、恢复和删除 CleanerLogic 管理的备份集。"""

    def __init__(self, cleaner, parent=None):
        super().__init__(parent)
        self.cleaner = cleaner
        self.setWindowTitle("备份管理")
        self.resize(820, 520)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        path_row = QHBoxLayout()
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        choose_button = QPushButton("更改备份目录")
        choose_button.clicked.connect(self.choose_backup_dir)
        path_row.addWidget(QLabel("备份目录："))
        path_row.addWidget(self.path_label, 1)
        path_row.addWidget(choose_button)
        layout.addLayout(path_row)

        limit_row = QHBoxLayout()
        self.max_count = QSpinBox()
        self.max_count.setRange(1, 1000)
        self.max_size_mb = QSpinBox()
        self.max_size_mb.setRange(1, 1024 * 1024)
        apply_limits = QPushButton("应用限制")
        apply_limits.clicked.connect(self.apply_limits)
        limit_row.addWidget(QLabel("最多保留"))
        limit_row.addWidget(self.max_count)
        limit_row.addWidget(QLabel("个备份集，保留总大小上限"))
        limit_row.addWidget(self.max_size_mb)
        limit_row.addWidget(QLabel("MB"))
        limit_row.addWidget(apply_limits)
        limit_row.addStretch()
        layout.addLayout(limit_row)

        note = QLabel(
            "每次实际清理生成一个备份集。超出数量或上限时优先删除较早备份；"
            "最新备份始终保留，因此单次备份本身超出上限时，总占用可能暂时超过该值。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["备份时间", "大小", "文件数", "恢复状态"])
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 110)
        self.tree.setColumnWidth(2, 90)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree, 1)

        self.summary = QLabel()
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh)
        self.restore_button = QPushButton("恢复选中备份")
        self.restore_button.clicked.connect(self.restore_selected)
        self.delete_button = QPushButton("删除选中备份")
        self.delete_button.clicked.connect(self.delete_selected)
        clean_button = QPushButton("按限制清理旧备份")
        clean_button.clicked.connect(self.clean_old_backups)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(refresh_button)
        buttons.addWidget(self.restore_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(clean_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self._update_buttons()

    def refresh(self):
        info = self.cleaner.get_backup_info()
        self.path_label.setText(info['backup_dir'])
        self.max_count.setValue(int(self.cleaner.max_backups))
        self.max_size_mb.setValue(max(1, int(self.cleaner.max_backup_size / (1024 * 1024))))
        self.tree.clear()
        for backup in info['backups']:
            status = "可恢复" if backup.get('restorable') else "不可自动恢复"
            item = QTreeWidgetItem([
                f"{backup['time']}  ({backup['name']})",
                format_size(backup['size']),
                str(backup.get('file_count', 0)),
                status,
            ])
            item.setData(0, Qt.UserRole, backup)
            if not backup.get('restorable'):
                item.setToolTip(3, backup.get('manifest_error', '缺少有效备份清单'))
                item.setForeground(3, Qt.darkRed)
            self.tree.addTopLevelItem(item)
        self.summary.setText(
            f"共 {info['backup_count']} 个备份集，占用 {format_size(info['total_size'])}"
            f"；当前容量目标 {format_size(self.cleaner.max_backup_size)}")
        self._update_buttons()

    def _selected_backup(self):
        selected = self.tree.selectedItems()
        return selected[0].data(0, Qt.UserRole) if selected else None

    def _update_buttons(self):
        backup = self._selected_backup()
        self.delete_button.setEnabled(backup is not None)
        self.restore_button.setEnabled(bool(backup and backup.get('restorable')))

    def choose_backup_dir(self):
        selected = QFileDialog.getExistingDirectory(
            self, "选择备份目录", self.cleaner.backup_dir)
        if not selected:
            return
        try:
            self.cleaner.set_options({'backup_dir': selected})
            self.refresh()
        except OSError as e:
            QMessageBox.critical(self, "设置失败", str(e))

    def apply_limits(self):
        self.cleaner.set_options({
            'max_backups': self.max_count.value(),
            'max_backup_size': self.max_size_mb.value() * 1024 * 1024,
        })
        if self.cleaner.clean_old_backups():
            self.refresh()
            QMessageBox.information(self, "设置完成", "备份限制已应用。")
        else:
            self.refresh()
            QMessageBox.warning(self, "部分失败", "限制已保存，但部分旧备份无法删除。")

    def restore_selected(self):
        backup = self._selected_backup()
        if not backup:
            return
        if not backup.get('restorable'):
            QMessageBox.warning(
                self, "无法恢复", backup.get('manifest_error', '备份清单无效'))
            return
        answer = QMessageBox.question(
            self,
            "确认恢复",
            f"将按清单把 {backup.get('file_count', 0)} 个文件恢复到原始路径。\n"
            "同名现有文件会被覆盖，是否继续？",
        )
        if answer != QMessageBox.Yes:
            return
        result = self.cleaner.restore_backup_detailed(backup['path'])
        if result['success']:
            QMessageBox.information(
                self, "恢复完成", f"已恢复 {result['restored_count']} 个文件。")
            return
        details = "\n".join(result['errors'][:10])
        if len(result['errors']) > 10:
            details += f"\n... 以及 {len(result['errors']) - 10} 个其他错误"
        QMessageBox.warning(
            self,
            "恢复未完全成功",
            f"已恢复 {result['restored_count']} 个文件。\n\n{details}",
        )

    def delete_selected(self):
        backup = self._selected_backup()
        if not backup:
            return
        if QMessageBox.question(
                self, "确认删除", f"永久删除备份 {backup['name']}？") != QMessageBox.Yes:
            return
        if self.cleaner.delete_backup(backup['path']):
            self.refresh()
        else:
            QMessageBox.critical(self, "删除失败", "无法删除选中的备份。")

    def clean_old_backups(self):
        if self.cleaner.clean_old_backups():
            self.refresh()
            QMessageBox.information(self, "清理完成", "旧备份已按当前限制清理。")
        else:
            self.refresh()
            QMessageBox.warning(self, "部分失败", "部分旧备份无法删除，请检查权限。")
