#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清理历史记录：持久化每次清理的摘要信息，供用户回顾。"""

import datetime
import json
import logging
import os
import secrets
import shutil

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QMessageBox, QFileDialog,
)
from PySide6.QtCore import Qt

logger = logging.getLogger('CCleaner')

HISTORY_PATH = os.path.join(
    os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
    'CCleaner', 'cleanup_history.json')

MAX_RECORDS = 100
MAX_SAMPLE_PATHS = 5
_CURRENT_VERSION = 1


# ─── 数据层 ───────────────────────────────────────────────────────────────

def load_history() -> list:
    """读取历史记录列表。出错返回空列表。"""
    try:
        if os.path.isfile(HISTORY_PATH):
            with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                records = data.get('records', [])
                if isinstance(records, list):
                    return records
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f'加载清理历史失败: {e}')
    return []


def save_history(records: list):
    """原子写入历史记录（同 settings.py 模式）。"""
    records = records[-MAX_RECORDS:]
    payload = {'version': _CURRENT_VERSION, 'records': records}
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        tmp = HISTORY_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HISTORY_PATH)
    except OSError as e:
        logger.warning(f'保存清理历史失败: {e}')
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def add_record(cleaned_items: list, errors: list, freed_space: int,
               simulate: bool, items: list) -> dict:
    """从清理结果构建一条历史记录并追加保存。

    Args:
        cleaned_items: 已清理的路径列表
        errors: 错误列表 [{'path', 'error'}]
        freed_space: 释放字节数
        simulate: 是否为模拟模式
        items: 传入 clean_selected 的原始 items（含 path/size/type）

    Returns:
        新创建的记录 dict
    """
    now = datetime.datetime.now()
    record_id = now.strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(2)

    # 按分类汇总
    categories = {}
    items_by_path = {it['path']: it for it in items if isinstance(it, dict)}
    for it in items:
        if not isinstance(it, dict):
            continue
        cat = it.get('type', 'unknown')
        if cat not in categories:
            categories[cat] = {'count': 0, 'size': 0}
        categories[cat]['count'] += 1
        categories[cat]['size'] += it.get('size', 0)

    # 示例路径：每分类取前 N 条（优先已清理的，不足则从全部 items 补充）
    cleaned_set = set(cleaned_items) if cleaned_items else set()
    sample_paths = {}
    cat_counters = {}
    # 第一轮：从已清理列表取
    for it in items:
        if not isinstance(it, dict):
            continue
        path = it.get('path', '')
        if not path:
            continue
        cat = it.get('type', 'unknown')
        if path in cleaned_set:
            cat_counters.setdefault(cat, 0)
            if cat_counters[cat] < MAX_SAMPLE_PATHS:
                sample_paths.setdefault(cat, []).append(path)
                cat_counters[cat] += 1
    # 第二轮：cleaned_items 为空或不足时，从全部 items 补充
    for it in items:
        if not isinstance(it, dict):
            continue
        path = it.get('path', '')
        if not path:
            continue
        cat = it.get('type', 'unknown')
        cat_counters.setdefault(cat, 0)
        if cat_counters[cat] >= MAX_SAMPLE_PATHS:
            continue
        if path not in cleaned_set:
            sample_paths.setdefault(cat, []).append(path)
            cat_counters[cat] += 1

    record = {
        'id': record_id,
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
        'simulate': simulate,
        'freed_space': freed_space,
        'item_count': len(items),
        'error_count': len(errors),
        'categories': categories,
        'sample_paths': sample_paths,
    }

    records = load_history()
    records.append(record)
    save_history(records)
    return record


def clear_history():
    """清空所有历史记录。"""
    save_history([])


def export_history(filepath: str):
    """导出历史记录到指定路径。"""
    shutil.copyfile(HISTORY_PATH, filepath)


# ─── UI 层 ────────────────────────────────────────────────────────────────

def _fmt_size(n):
    """格式化字节数为可读字符串。"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024 or unit == 'TB':
            return f"{n:.2f} {unit}" if unit != 'B' else f"{int(n)} B"
        n /= 1024


class CleanupHistoryDialog(QDialog):
    """清理历史查看对话框。"""

    def __init__(self, parent=None, categories_map=None):
        super().__init__(parent)
        self.setWindowTitle("清理历史")
        self.resize(780, 520)
        self._categories_map = categories_map or {}
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.summary = QLabel()
        self.summary.setStyleSheet("font-size:13px; color:#3a4356;")
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["时间", "类型", "释放空间", "项目数", "错误数"])
        self.tree.setColumnWidth(0, 160)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 110)
        self.tree.setColumnWidth(3, 70)
        self.tree.setColumnWidth(4, 70)
        self.tree.header().setStretchLastSection(True)
        self.tree.setStyleSheet(
            "QTreeWidget { background:#fff; border:1px solid #e6e9f2; "
            "border-radius:8px; font-size:13px; color:#1b2233; }\n"
            "QTreeWidget::item { padding:4px; min-height:20px; color:#1b2233; }\n"
            "QTreeWidget QHeaderView::section { background:#f4f6fb; color:#3a4356; "
            "border:none; padding:4px 8px; font-size:12px; font-weight:600; }")
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        export_btn = QPushButton("导出历史")
        export_btn.setStyleSheet(
            "background:#eef1f8; color:#33415c; border:none; border-radius:6px; "
            "padding:7px 14px; font-size:13px;")
        export_btn.clicked.connect(self._export)
        clear_btn = QPushButton("清空历史")
        clear_btn.setStyleSheet(
            "background:#e5484d; color:#fff; border:none; border-radius:6px; "
            "padding:7px 14px; font-size:13px;")
        clear_btn.clicked.connect(self._clear)
        close_btn = QPushButton("关闭")
        close_btn.setStyleSheet(
            "background:#4f7cff; color:#fff; border:none; border-radius:6px; "
            "padding:7px 14px; font-size:13px;")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(export_btn)
        buttons.addWidget(clear_btn)
        buttons.addStretch()
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def refresh(self):
        self.tree.clear()
        records = load_history()
        if not records:
            self.summary.setText("暂无清理历史记录。")
            return

        total_freed = sum(r.get('freed_space', 0) for r in records if not r.get('simulate'))
        self.summary.setText(
            f"共 {len(records)} 条记录，累计实际释放 {_fmt_size(total_freed)}")

        for rec in reversed(records):
            node = QTreeWidgetItem(self.tree)
            node.setText(0, rec.get('timestamp', ''))
            node.setText(1, "模拟" if rec.get('simulate') else "实际清理")
            node.setText(2, _fmt_size(rec.get('freed_space', 0)))
            node.setText(3, str(rec.get('item_count', 0)))
            node.setText(4, str(rec.get('error_count', 0)))

            cats = rec.get('categories', {})
            samples = rec.get('sample_paths', {})
            for cat_key, info in cats.items():
                cat_name = self._categories_map.get(cat_key, cat_key)
                cat_node = QTreeWidgetItem(node)
                cat_node.setText(0, f"{cat_name}: {info.get('count', 0)}项")
                cat_node.setText(2, _fmt_size(info.get('size', 0)))

                paths = samples.get(cat_key, [])
                for p in paths:
                    path_node = QTreeWidgetItem(cat_node)
                    path_node.setText(0, os.path.basename(p))
                    path_node.setText(2, p)

    def _export(self):
        if not os.path.isfile(HISTORY_PATH):
            QMessageBox.information(self, "导出", "暂无历史记录可导出。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清理历史", "cleanup_history.json",
            "JSON 文件 (*.json)")
        if path:
            try:
                export_history(path)
                QMessageBox.information(self, "导出成功", f"历史记录已导出到:\n{path}")
            except OSError as e:
                QMessageBox.warning(self, "导出失败", str(e))

    def _clear(self):
        if QMessageBox.question(
                self, "确认清空", "确定要清空所有清理历史记录吗？") != QMessageBox.Yes:
            return
        clear_history()
        self.refresh()
