#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用户设置持久化：JSON读写。"""

import json
import os
import logging

logger = logging.getLogger('CCleaner')

SETTINGS_PATH = os.path.join(
    os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
    'CCleaner', 'settings.json')

_DEFAULTS = {
    'simulate': True,
    'backup': True,
    'backup_dir': '',  # empty means use CleanerLogic default
    'max_backups': 5,
    'max_backup_size_mb': 20480,  # 20 GB in MB
    'check_updates': True,
    'skipped_version': '',
}


def load_settings() -> dict:
    """Load settings from JSON file. Returns defaults on any error."""
    settings = dict(_DEFAULTS)
    try:
        if os.path.isfile(SETTINGS_PATH):
            with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in _DEFAULTS:
                    if key in data:
                        settings[key] = data[key]
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f'加载设置失败，使用默认值: {e}')
    return settings


def save_settings(settings: dict):
    """Save settings to JSON file atomically."""
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_PATH)
    except OSError as e:
        logger.warning(f'保存设置失败: {e}')
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
