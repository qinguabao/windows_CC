#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
C盘清理工具 - 核心清理逻辑
"""

import errno
import hashlib
import json
import os
import shutil
import tempfile
import time
import glob
import logging
import datetime
import threading
import concurrent.futures

# 配置日志
_LOG_DIR = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'CCleaner')
_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _log_handler = logging.FileHandler(
        os.path.join(_LOG_DIR, 'cleaner.log'), encoding='utf-8')
except OSError:
    try:
        _log_handler = logging.FileHandler(
            os.path.join(tempfile.gettempdir(), 'CCleaner-cleaner.log'),
            encoding='utf-8',
        )
    except OSError:
        _log_handler = logging.NullHandler()
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=[_log_handler])
logger = logging.getLogger('CCleaner')

DEFAULT_MAX_BACKUPS = 5
DEFAULT_MAX_BACKUP_SIZE = 20 * 1024 * 1024 * 1024  # 20 GiB，按清理批次保存的总容量目标


# 这些类别只能用于分析展示，任何调用方都不能把它们交给删除核心。
ANALYSIS_ONLY_CATEGORIES = frozenset({'large_files'})

# 这些系统维护场景不适合通过递归删除实现，应使用 Windows 官方维护接口。
DISABLED_CLEANUP_CATEGORIES = frozenset({
    'updates',
    'old_windows',
    'service_packs',
    'disk_cleanup',
    'backup_temp',
    'update_temp',
    'driver_backup',
    'windows_defender',
    'installer_cache',
})

# 其它磁盘分析结果可以清理，但必须走专用的盘符/范围校验。
STORAGE_CLEANUP_CATEGORIES = frozenset({
    'storage_large_file',
    'storage_duplicate_file',
})
STORAGE_PROTECTED_ROOTS = frozenset({
    '$recycle.bin',
    'recovery',
    'system volume information',
    'windows',
    'program files',
    'program files (x86)',
    'programdata',
})


class BackupError(RuntimeError):
    """备份未可靠落盘时阻止后续删除。"""


class CleanerLogic:
    """清理逻辑核心类"""

    def __init__(self):
        """初始化清理器"""
        self.options = {
            'simulate': True,  # 默认为模拟模式
            'backup': True     # 默认备份文件
        }

        # 安全路径列表 - 这些路径不会被扫描或清理
        self.safe_paths = [
            os.path.join('C:', os.sep, 'Windows', 'System32'),
            os.path.join('C:', os.sep, 'Windows', 'SysWOW64'),
            os.path.join('C:', os.sep, 'Program Files'),
            os.path.join('C:', os.sep, 'Program Files (x86)'),
        ]

        # 默认备份目录
        default_backup_dir = os.path.join(tempfile.gettempdir(), 'CCleaner_Backup')

        # 尝试找到非C盘的默认备份位置
        try:
            # 获取所有磁盘
            import string
            import ctypes

            drives = []
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    drives.append(letter + ':')
                bitmask >>= 1

            # 如果有非C盘，使用第一个非C盘作为默认备份位置
            for drive in drives:
                if drive.upper() != 'C:' and os.path.exists(drive):
                    # 修正：os.path.join('D:', 'x') 得到 'D:x'（相对路径），需补盘符根分隔符
                    default_backup_dir = os.path.join(drive + os.sep, 'CCleaner_Backup')
                    break
        except Exception as e:
            logger.warning(f"无法获取非C盘作为备份位置: {e}")

        # 设置备份目录
        self.backup_dir = default_backup_dir

        # 备份限制
        self.max_backups = DEFAULT_MAX_BACKUPS  # 最多保留几个备份集
        self.max_backup_size = DEFAULT_MAX_BACKUP_SIZE

        # 确保备份目录存在
        if not os.path.exists(self.backup_dir):
            os.makedirs(self.backup_dir, exist_ok=True)

        # 扫描/清理过程中用于跨线程请求中断的事件
        self._abort_event = threading.Event()

        # 扫描进度状态（跨线程共享，用锁保护）
        self._prog_lock = threading.Lock()
        self._prog_value = 0.0        # 0.0 ~ 1.0
        self._progress_cb = None
        self._large_budget = 0.0      # 大文件扫描内部可用于推进的进度预算

    def _advance_progress(self, delta, name):
        """推进扫描进度并回调 UI。delta 为 0~1 的增量，name 为当前正在执行的任务显示名。"""
        with self._prog_lock:
            self._prog_value = min(1.0, self._prog_value + delta)
            v = self._prog_value
        if self._progress_cb:
            self._progress_cb(name, int(v * 1000), 1000)

    def set_options(self, options):
        """设置选项"""
        self.options.update(options)

        # 如果设置了自定义备份目录
        if 'backup_dir' in options and options['backup_dir']:
            self.backup_dir = options['backup_dir']
            # 确保备份目录存在
            if not os.path.exists(self.backup_dir):
                os.makedirs(self.backup_dir, exist_ok=True)

        # 如果设置了备份限制
        if 'max_backups' in options:
            self.max_backups = max(1, int(options['max_backups']))
        if 'max_backup_size' in options:
            self.max_backup_size = max(1, int(options['max_backup_size']))

    def get_disk_info(self):
        """获取C盘信息"""
        try:
            # 使用os.statvfs替代psutil
            # 但Windows不支持statvfs，所以我们使用ctypes调用Windows API
            import ctypes

            free_bytes = ctypes.c_ulonglong(0)
            total_bytes = ctypes.c_ulonglong(0)

            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p('C:'),
                None,
                ctypes.pointer(total_bytes),
                ctypes.pointer(free_bytes)
            )

            total = total_bytes.value / (1024 * 1024 * 1024)  # GB
            free = free_bytes.value / (1024 * 1024 * 1024)    # GB
            used = total - free
            percent = (used / total) * 100 if total > 0 else 0

            return {
                'total': total,
                'used': used,
                'free': free,
                'percent': round(percent, 1)
            }
        except Exception as e:
            logger.error(f"获取磁盘信息失败: {e}")
            return {
                'total': 0,
                'used': 0,
                'free': 0,
                'percent': 0
            }

    @staticmethod
    def _drive_key(path):
        drive, _ = os.path.splitdrive(os.path.abspath(path))
        return os.path.normcase(drive.rstrip('\\/'))

    @classmethod
    def same_volume(cls, first_path, second_path):
        """判断两个路径是否位于同一 Windows 卷。"""
        first_drive = cls._drive_key(first_path)
        second_drive = cls._drive_key(second_path)
        return bool(first_drive and first_drive == second_drive)

    def get_available_drives(self, include_system=False):
        """返回可分析的本地固定盘和可移动盘。"""
        try:
            import ctypes
            import string

            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            system_drive = os.path.normcase(
                os.environ.get('SystemDrive', 'C:').rstrip('\\/'))
            drives = []
            for letter in string.ascii_uppercase:
                if not bitmask & 1:
                    bitmask >>= 1
                    continue
                bitmask >>= 1
                root = f'{letter}:\\'
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(
                    ctypes.c_wchar_p(root))
                if drive_type not in (2, 3):  # 可移动盘、固定盘
                    continue
                if not include_system and os.path.normcase(f'{letter}:') == system_drive:
                    continue
                try:
                    usage = shutil.disk_usage(root)
                except OSError:
                    continue
                drives.append({
                    'path': root,
                    'name': f'{letter}:',
                    'total': usage.total,
                    'used': usage.used,
                    'free': usage.free,
                    'percent': round(usage.used * 100 / usage.total, 1)
                    if usage.total else 0,
                    'removable': drive_type == 2,
                })
            return drives
        except Exception as e:
            logger.warning(f'无法枚举其它磁盘: {e}')
            return []

    @staticmethod
    def _is_reparse_directory(path):
        try:
            stat_result = os.stat(path, follow_symlinks=False)
            attributes = getattr(stat_result, 'st_file_attributes', 0)
            return os.path.islink(path) or bool(attributes & 0x400)
        except OSError:
            return True

    @staticmethod
    def _path_is_within(root, path):
        try:
            root_real = os.path.normcase(os.path.realpath(root))
            path_real = os.path.normcase(os.path.realpath(path))
            return os.path.commonpath([root_real, path_real]) == root_real
        except (OSError, TypeError, ValueError):
            return False

    def _storage_scan_directory_allowed(self, scan_root, directory):
        """过滤系统根目录、备份目录和重解析目录。"""
        if self._is_reparse_directory(directory):
            return False
        directory_norm = os.path.normcase(os.path.realpath(directory))
        backup_norm = os.path.normcase(os.path.realpath(self.backup_dir))
        if directory_norm == backup_norm or directory_norm.startswith(backup_norm + os.sep):
            return False
        parent_norm = os.path.normcase(os.path.realpath(os.path.dirname(directory)))
        root_norm = os.path.normcase(os.path.realpath(scan_root))
        if parent_norm == root_norm and os.path.basename(directory).lower() in STORAGE_PROTECTED_ROOTS:
            return False
        return True

    @staticmethod
    def _file_hash(path, size, quick, abort_callback=None):
        """分层哈希：先首尾采样，再对候选文件做完整 SHA-256。"""
        digest = hashlib.sha256()
        chunk_size = 1024 * 1024
        try:
            with open(path, 'rb') as handle:
                if quick:
                    sample_size = 64 * 1024
                    digest.update(handle.read(sample_size))
                    if size > sample_size:
                        handle.seek(max(0, size - sample_size))
                        digest.update(handle.read(sample_size))
                    digest.update(str(size).encode('ascii'))
                else:
                    while True:
                        if abort_callback and abort_callback():
                            return None, True
                        chunk = handle.read(chunk_size)
                        if not chunk:
                            break
                        digest.update(chunk)
            return digest.hexdigest(), False
        except (OSError, ValueError):
            return None, False

    def scan_storage(self, scan_root, min_large_size=1024 * 1024 * 1024,
                     min_duplicate_size=1024 * 1024, find_large=True,
                     find_duplicates=True, abort_callback=None,
                     progress_callback=None):
        """一次遍历分析其它磁盘中的大文件和内容重复文件。"""
        root = os.path.realpath(os.path.abspath(scan_root))
        if not os.path.isdir(root):
            raise ValueError('扫描目录不存在或不可访问')
        if self._path_is_within(self.backup_dir, root):
            raise ValueError('不能扫描当前备份目录')
        drive, tail = os.path.splitdrive(root)
        system_drive = os.path.normcase(
            os.environ.get('SystemDrive', 'C:').rstrip('\\/'))
        if (drive and os.path.normcase(drive) == system_drive
                and tail in (os.sep, '/', '\\')):
            raise ValueError('系统盘请使用主界面的系统扫描功能')
        if tail not in (os.sep, '/', '\\') and not self._is_safe_path(root):
            raise ValueError('不能扫描系统或受保护目录')

        min_large_size = max(1, int(min_large_size))
        min_duplicate_size = max(1, int(min_duplicate_size))
        result = {
            'scan_root': root,
            'large_files': [],
            'duplicate_groups': [],
            'scanned_files': 0,
            'scanned_size': 0,
            'errors': [],
            'aborted': False,
        }
        duplicate_candidates = []
        seen_files = set()

        for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
            if abort_callback and abort_callback():
                result['aborted'] = True
                return result
            dirs[:] = [
                name for name in dirs
                if self._storage_scan_directory_allowed(
                    root, os.path.join(current_root, name))
            ]
            for name in files:
                if abort_callback and abort_callback():
                    result['aborted'] = True
                    return result
                path = os.path.join(current_root, name)
                try:
                    if os.path.islink(path) or not os.path.isfile(path):
                        continue
                    stat_result = os.stat(path, follow_symlinks=False)
                    attributes = getattr(stat_result, 'st_file_attributes', 0)
                    if attributes & 0x400:
                        continue
                    identity = ((stat_result.st_dev, stat_result.st_ino)
                                if stat_result.st_ino else
                                ('path', os.path.normcase(os.path.realpath(path))))
                    if identity in seen_files:
                        continue
                    seen_files.add(identity)
                    size = stat_result.st_size
                    result['scanned_files'] += 1
                    result['scanned_size'] += size
                    record = {
                        'path': path,
                        'size': size,
                        'scan_root': root,
                        'modified': datetime.datetime.fromtimestamp(
                            stat_result.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                        'extension': os.path.splitext(path)[1].lower(),
                    }
                    if find_large and size >= min_large_size:
                        large_item = dict(record, type='storage_large_file')
                        record['large_item'] = large_item
                        result['large_files'].append(large_item)
                    if find_duplicates and size >= min_duplicate_size:
                        duplicate_candidates.append(record)
                    if progress_callback and result['scanned_files'] % 100 == 0:
                        progress_callback(path, result['scanned_files'])
                except (PermissionError, FileNotFoundError):
                    continue
                except OSError as e:
                    if len(result['errors']) < 100:
                        result['errors'].append({'path': path, 'error': str(e)})

        result['large_files'].sort(key=lambda item: item['size'], reverse=True)
        if not find_duplicates or result['aborted']:
            return result

        by_size = {}
        for record in duplicate_candidates:
            by_size.setdefault(record['size'], []).append(record)

        duplicate_groups = []
        for size, same_size_records in by_size.items():
            if len(same_size_records) < 2:
                continue
            quick_groups = {}
            for record in same_size_records:
                digest, aborted = self._file_hash(
                    record['path'], size, True, abort_callback)
                if aborted:
                    result['aborted'] = True
                    return result
                if digest:
                    quick_groups.setdefault(digest, []).append(record)
            for quick_records in quick_groups.values():
                if len(quick_records) < 2:
                    continue
                exact_groups = {}
                for record in quick_records:
                    digest, aborted = self._file_hash(
                        record['path'], size, False, abort_callback)
                    if aborted:
                        result['aborted'] = True
                        return result
                    if digest:
                        exact_groups.setdefault(digest, []).append(record)
                for digest, exact_records in exact_groups.items():
                    if len(exact_records) < 2:
                        continue
                    exact_records.sort(key=lambda item: (item['modified'], item['path']))
                    group_id = f'{size}:{digest}'
                    group_files = []
                    for index, record in enumerate(exact_records):
                        duplicate_item = dict(
                            record,
                            type='storage_duplicate_file',
                            duplicate_group=group_id,
                            duplicate_count=len(exact_records),
                            recommended_keep=index == 0,
                        )
                        duplicate_item.pop('large_item', None)
                        group_files.append(duplicate_item)
                        if record.get('large_item') is not None:
                            record['large_item'].update({
                                'duplicate_group': group_id,
                                'duplicate_count': len(exact_records),
                            })
                    duplicate_groups.append({
                        'id': group_id,
                        'size': size,
                        'count': len(group_files),
                        'reclaimable_size': size * (len(group_files) - 1),
                        'files': group_files,
                    })
        duplicate_groups.sort(
            key=lambda group: group['reclaimable_size'], reverse=True)
        result['duplicate_groups'] = duplicate_groups
        return result

    def get_backup_info(self):
        """获取备份信息"""
        try:
            if not os.path.exists(self.backup_dir):
                return {
                    'backup_dir': self.backup_dir,
                    'backup_count': 0,
                    'total_size': 0,
                    'backups': []
                }

            # 获取所有备份文件夹
            backups = []
            total_size = 0

            for item in os.listdir(self.backup_dir):
                item_path = os.path.join(self.backup_dir, item)
                if os.path.isdir(item_path):
                    # 计算备份大小
                    backup_size = 0
                    for root, _, files in os.walk(item_path):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    backup_size += os.path.getsize(file_path)
                            except (PermissionError, FileNotFoundError):
                                pass

                    # 尝试从文件夹名解析时间
                    try:
                        try:
                            backup_time = datetime.datetime.strptime(
                                item, '%Y%m%d_%H%M%S_%f')
                        except ValueError:
                            backup_time = datetime.datetime.strptime(
                                item, '%Y%m%d_%H%M%S')
                        backup_time_str = backup_time.strftime('%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        backup_time = datetime.datetime.fromtimestamp(os.path.getctime(item_path))
                        backup_time_str = backup_time.strftime('%Y-%m-%d %H:%M:%S')

                    manifest_path = os.path.join(item_path, 'manifest.json')
                    restorable = False
                    file_count = 0
                    manifest_error = ''
                    if os.path.isfile(manifest_path):
                        try:
                            with open(manifest_path, 'r', encoding='utf-8') as handle:
                                manifest = json.load(handle)
                            entries = manifest.get('entries', [])
                            if manifest.get('version') == 1 and isinstance(entries, list):
                                restorable = True
                                file_count = len(entries)
                            else:
                                manifest_error = '备份清单格式不受支持'
                        except (OSError, ValueError, TypeError) as e:
                            manifest_error = f'备份清单损坏: {e}'
                    else:
                        manifest_error = '旧版备份缺少恢复清单'

                    backups.append({
                        'name': item,
                        'path': item_path,
                        'size': backup_size,
                        'time': backup_time_str,
                        'timestamp': backup_time.timestamp(),
                        'file_count': file_count,
                        'restorable': restorable,
                        'manifest_error': manifest_error,
                    })

                    total_size += backup_size

            # 按时间排序，最新的在前面
            backups.sort(key=lambda x: x['timestamp'], reverse=True)

            return {
                'backup_dir': self.backup_dir,
                'backup_count': len(backups),
                'total_size': total_size,
                'backups': backups
            }
        except Exception as e:
            logger.error(f"获取备份信息失败: {e}")
            return {
                'backup_dir': self.backup_dir,
                'backup_count': 0,
                'total_size': 0,
                'backups': []
            }

    def clean_old_backups(self):
        """清理旧备份"""
        try:
            backup_info = self.get_backup_info()
            backups = backup_info['backups']

            remaining_size = backup_info['total_size']
            remaining_count = len(backups)
            success = True
            # 始终保留最新备份，避免单个超大清理批次导致刚完成清理就无法恢复。
            for backup in reversed(backups[1:]):
                over_count = remaining_count > self.max_backups
                over_size = remaining_size > self.max_backup_size
                if not over_count and not over_size:
                    break
                if self.delete_backup(backup['path']):
                    logger.info(f"删除旧备份: {backup['name']}")
                    remaining_count -= 1
                    remaining_size -= backup['size']
                else:
                    success = False
            return success
        except Exception as e:
            logger.error(f"清理旧备份失败: {e}")
            return False

    def _is_managed_backup_path(self, backup_path):
        """只允许操作备份根目录下的直接子目录。"""
        try:
            root = os.path.normcase(os.path.realpath(self.backup_dir))
            target = os.path.normcase(os.path.realpath(backup_path))
            return target != root and os.path.dirname(target) == root
        except (OSError, TypeError, ValueError):
            return False

    def delete_backup(self, backup_path):
        """安全删除一个由本程序管理的备份集。"""
        if not self._is_managed_backup_path(backup_path):
            logger.error(f"拒绝删除非托管备份路径: {backup_path}")
            return False
        try:
            shutil.rmtree(backup_path)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            logger.error(f"删除备份失败: {backup_path}, {e}")
            return False

    def restore_backup_detailed(self, backup_path):
        """根据清单恢复备份，并返回可供界面展示的详细结果。"""
        result = {'success': False, 'restored_count': 0, 'errors': []}
        if not self._is_managed_backup_path(backup_path):
            result['errors'].append('备份路径不在当前备份目录中')
            return result

        manifest_path = os.path.join(backup_path, 'manifest.json')
        try:
            with open(manifest_path, 'r', encoding='utf-8') as handle:
                manifest = json.load(handle)
        except (OSError, ValueError, TypeError) as e:
            result['errors'].append(f'无法读取备份清单: {e}')
            return result

        entries = manifest.get('entries')
        if manifest.get('version') != 1 or not isinstance(entries, list):
            result['errors'].append('备份清单格式不受支持')
            return result

        backup_root = os.path.realpath(backup_path)
        for entry in entries:
            original_path = entry.get('original_path', '') if isinstance(entry, dict) else ''
            backup_rel = entry.get('backup_path', '') if isinstance(entry, dict) else ''
            try:
                if not original_path or not os.path.isabs(original_path):
                    raise ValueError('原始路径不是绝对路径')
                payload_path = os.path.realpath(os.path.join(backup_root, backup_rel))
                if os.path.commonpath([backup_root, payload_path]) != backup_root:
                    raise ValueError('备份文件路径越界')
                if not os.path.isfile(payload_path):
                    raise FileNotFoundError('备份文件不存在')
                os.makedirs(os.path.dirname(original_path), exist_ok=True)
                shutil.copy2(payload_path, original_path)
                result['restored_count'] += 1
            except Exception as e:
                result['errors'].append(f"{original_path or backup_rel}: {e}")

        result['success'] = not result['errors'] and result['restored_count'] == len(entries)
        logger.info(
            f"恢复完成，共恢复 {result['restored_count']} 个文件，"
            f"错误 {len(result['errors'])} 个")
        return result

    def restore_backup(self, backup_path):
        """兼容旧调用方的布尔返回值。"""
        return self.restore_backup_detailed(backup_path)['success']

    def _create_backup_session(self):
        """创建唯一备份集；每个文件写入后立即原子更新清单。"""
        name = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        session_dir = os.path.join(self.backup_dir, name)
        suffix = 1
        while os.path.exists(session_dir):
            session_dir = os.path.join(self.backup_dir, f'{name}_{suffix}')
            suffix += 1
        os.makedirs(os.path.join(session_dir, 'files'), exist_ok=False)
        return {
            'path': session_dir,
            'manifest_path': os.path.join(session_dir, 'manifest.json'),
            'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'entries': [],
        }

    @staticmethod
    def _persist_backup_manifest(session):
        manifest = {
            'version': 1,
            'created_at': session['created_at'],
            'entries': session['entries'],
        }
        temp_path = session['manifest_path'] + '.tmp'
        try:
            with open(temp_path, 'w', encoding='utf-8') as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, session['manifest_path'])
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _backup_file(self, file_path, session):
        """可靠备份单个文件；任一步失败都抛出 BackupError。"""
        original_path = os.path.abspath(file_path)
        index = len(session['entries']) + 1
        backup_rel = os.path.join('files', f'{index:08d}.bin')
        payload_path = os.path.join(session['path'], backup_rel)
        try:
            shutil.copy2(file_path, payload_path)
            entry = {
                'original_path': original_path,
                'backup_path': backup_rel,
                'size': os.path.getsize(payload_path),
            }
            session['entries'].append(entry)
            try:
                self._persist_backup_manifest(session)
            except Exception:
                session['entries'].pop()
                raise
            logger.info(f"已备份文件: {file_path} -> {payload_path}")
        except Exception as e:
            try:
                if os.path.exists(payload_path):
                    os.remove(payload_path)
            except OSError:
                pass
            raise BackupError(f"备份失败，已取消删除：{e}") from e

    @staticmethod
    def _discard_empty_backup_session(session):
        if session and not session['entries']:
            try:
                shutil.rmtree(session['path'])
            except OSError as e:
                logger.warning(f"删除空备份目录失败: {session['path']}, {e}")

    def scan_system(self, progress_callback=None, abort_callback=None,
                    skip_categories=None, completed_callback=None):
        """扫描系统中可清理的文件

        progress_callback(name, done, total) 会在扫描过程中被调用，
        name 为当前完成的中文扫描项名称，done/total 为完成数和总数。
        completed_callback(category) 仅在类别完整扫描后调用，供暂停后续扫使用。
        """
        logger.info("开始扫描系统")
        results = {
            # 基本清理
            'temp': [],          # 临时文件
            'recycle': [],       # 回收站
            'cache': [],         # 浏览器缓存
            'logs': [],          # 系统日志
            'updates': [],       # Windows更新缓存
            'thumbnails': [],    # 缩略图缓存

            # 扩展清理
            'prefetch': [],      # 预读取文件
            'old_windows': [],   # 旧Windows文件
            'error_reports': [], # 错误报告
            'service_packs': [], # 服务包备份
            'memory_dumps': [],  # 内存转储文件
            'font_cache': [],    # 字体缓存
            'disk_cleanup': [],  # 磁盘清理备份

            # 新增安全清理项
            'app_cache': [],     # 应用程序缓存
            'media_cache': [],   # 媒体播放器缓存
            'search_index': [],  # 搜索索引临时文件
            'backup_temp': [],   # 备份临时文件
            'update_temp': [],   # 更新临时文件
            'driver_backup': [], # 驱动备份
            'app_crash': [],     # 应用程序崩溃转储
            'app_logs': [],      # 应用程序日志
            'recent_items': [],  # 最近使用的文件列表缓存
            'notification': [],  # Windows通知缓存
            'dns_cache': [],     # DNS缓存
            'printer_temp': [],  # 打印机临时文件
            'device_temp': [],   # 设备临时文件
            'windows_defender': [], # Windows Defender缓存
            'store_cache': [],   # Windows Store缓存
            'onedrive_cache': [], # OneDrive缓存

            # 新增用户请求的清理项
            'downloads': [],     # 下载文件夹(安全版)
            'installer_cache': [], # 安装程序缓存(安全版)
            'delivery_opt': [],  # Windows传递优化缓存

            # 大文件扫描
            'large_files': []    # 大文件
        }

        # 定义扫描任务
        scan_tasks = [
            self._scan_temp_files,
            self._scan_recycle_bin,
            self._scan_browser_cache,
            self._scan_system_logs,
            self._scan_windows_updates,
            self._scan_thumbnails_cache,
            self._scan_prefetch,
            self._scan_old_windows,
            self._scan_error_reports,
            self._scan_service_packs,
            self._scan_memory_dumps,
            self._scan_font_cache,
            self._scan_disk_cleanup_backup,
            self._scan_app_cache,
            self._scan_media_cache,
            self._scan_search_index,
            self._scan_backup_temp,
            self._scan_update_temp,
            self._scan_driver_backup,
            self._scan_app_crash,
            self._scan_app_logs,
            self._scan_recent_items,
            self._scan_notification_cache,
            self._scan_dns_cache,
            self._scan_printer_temp,
            self._scan_device_temp,
            self._scan_windows_defender,
            self._scan_store_cache,
            self._scan_onedrive_cache,
            self._scan_downloads_immediate,
            self._scan_installer_cache_safe,
            self._scan_delivery_optimization,
            self._scan_large_files
        ]

        # 中文显示名（用于 UI 进度提示）
        task_names = {
            self._scan_temp_files: "临时文件",
            self._scan_recycle_bin: "回收站",
            self._scan_browser_cache: "浏览器缓存",
            self._scan_system_logs: "系统日志",
            self._scan_windows_updates: "Windows更新缓存",
            self._scan_thumbnails_cache: "缩略图缓存",
            self._scan_prefetch: "预读取文件",
            self._scan_old_windows: "旧Windows文件",
            self._scan_error_reports: "错误报告",
            self._scan_service_packs: "服务包备份",
            self._scan_memory_dumps: "内存转储文件",
            self._scan_font_cache: "字体缓存",
            self._scan_disk_cleanup_backup: "磁盘清理备份",
            self._scan_app_cache: "应用程序缓存",
            self._scan_media_cache: "媒体播放器缓存",
            self._scan_search_index: "搜索索引临时文件",
            self._scan_backup_temp: "备份临时文件",
            self._scan_update_temp: "更新临时文件",
            self._scan_driver_backup: "驱动备份",
            self._scan_app_crash: "应用程序崩溃转储",
            self._scan_app_logs: "应用程序日志",
            self._scan_recent_items: "最近使用的文件列表",
            self._scan_notification_cache: "Windows通知缓存",
            self._scan_dns_cache: "DNS缓存",
            self._scan_printer_temp: "打印机临时文件",
            self._scan_device_temp: "设备临时文件",
            self._scan_windows_defender: "Windows Defender缓存",
            self._scan_store_cache: "Windows Store缓存",
            self._scan_onedrive_cache: "OneDrive缓存",
            self._scan_downloads_immediate: "下载文件夹",
            self._scan_installer_cache_safe: "安装程序缓存",
            self._scan_delivery_optimization: "Windows传递优化缓存",
            self._scan_large_files: "大文件",
        }

        task_category = {
            self._scan_temp_files: 'temp',
            self._scan_recycle_bin: 'recycle',
            self._scan_browser_cache: 'cache',
            self._scan_system_logs: 'logs',
            self._scan_windows_updates: 'updates',
            self._scan_thumbnails_cache: 'thumbnails',
            self._scan_prefetch: 'prefetch',
            self._scan_old_windows: 'old_windows',
            self._scan_error_reports: 'error_reports',
            self._scan_service_packs: 'service_packs',
            self._scan_memory_dumps: 'memory_dumps',
            self._scan_font_cache: 'font_cache',
            self._scan_disk_cleanup_backup: 'disk_cleanup',
            self._scan_app_cache: 'app_cache',
            self._scan_media_cache: 'media_cache',
            self._scan_search_index: 'search_index',
            self._scan_backup_temp: 'backup_temp',
            self._scan_update_temp: 'update_temp',
            self._scan_driver_backup: 'driver_backup',
            self._scan_app_crash: 'app_crash',
            self._scan_app_logs: 'app_logs',
            self._scan_recent_items: 'recent_items',
            self._scan_notification_cache: 'notification',
            self._scan_dns_cache: 'dns_cache',
            self._scan_printer_temp: 'printer_temp',
            self._scan_device_temp: 'device_temp',
            self._scan_windows_defender: 'windows_defender',
            self._scan_store_cache: 'store_cache',
            self._scan_onedrive_cache: 'onedrive_cache',
            self._scan_downloads_immediate: 'downloads',
            self._scan_installer_cache_safe: 'installer_cache',
            self._scan_delivery_optimization: 'delivery_opt',
            self._scan_large_files: 'large_files',
        }
        skip = set(skip_categories or [])
        scan_tasks = [
            task for task in scan_tasks
            if task_category.get(task) not in skip
            and task_category.get(task) not in DISABLED_CLEANUP_CATEGORIES
        ]
        total = len(scan_tasks)
        self._abort_event.clear()

        # ---- 按耗时加权的进度模型 ----
        # 大文件扫描要遍历整个 C 盘，耗时占大头；其余轻量任务均分剩余权重。
        # 这样进度条反映的是"离真正结束还有多远"，而不是"完成了几个任务"。
        heavy = {'large_files': 0.45, 'updates': 0.12, 'installer_cache': 0.06, 'downloads': 0.05}
        used_cats = {task_category.get(t) for t in scan_tasks}
        hw = {c: w for c, w in heavy.items() if c in used_cats}
        light_tasks = [t for t in scan_tasks if task_category.get(t) not in hw]
        light_w = (1.0 - sum(hw.values())) / max(1, len(light_tasks))
        task_weight = {}
        for t in scan_tasks:
            c = task_category.get(t)
            task_weight[t] = hw[c] if c in hw else light_w

        # 重置进度状态
        with self._prog_lock:
            self._prog_value = 0.0
        self._progress_cb = progress_callback
        self._large_spent = 0.0
        # 大文件扫描内部可推进的预算：启动占 20%，内部推进占 55%，完成收尾占 25%
        self._large_budget = task_weight.get(self._scan_large_files, 0.0) * 0.55

        if progress_callback:
            progress_callback("准备扫描", 0, 1000)

        # 使用ThreadPoolExecutor并发运行扫描任务
        # 根据测试调整max_workers，None通常默认为os.cpu_count（）*5
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # 提交所有任务，每提交一个推进其权重的 20%（任务已启动）
            future_to_task = {}
            for task in scan_tasks:
                if abort_callback and abort_callback():
                    self._abort_event.set()
                    break
                future_to_task[executor.submit(task, results)] = task
                self._advance_progress(task_weight[task] * 0.20,
                                       task_names.get(task, task.__name__))

            # 等待所有任务完成并处理潜在的异常
            for future in concurrent.futures.as_completed(future_to_task):
                task_func = future_to_task[future]
                try:
                    future.result()  # 任务期间发生的任何异常
                    logger.info(f"Task {task_func.__name__} completed successfully.")
                    if not (abort_callback and abort_callback()) and completed_callback:
                        completed_callback(task_category[task_func])
                except Exception as exc:
                    logger.error(f'Task {task_func.__name__} generated an exception: {exc}')
                # 完成时补齐该任务剩余权重：启动时已推进 20%，这里补剩余 80%。
                # 大文件扫描内部已推进过 self._large_spent，需从收尾部分扣除，避免超发。
                finish_delta = task_weight[task_func] * 0.80
                if task_category[task_func] == 'large_files':
                    finish_delta = max(0.0, task_weight[task_func] * 0.80
                                       - getattr(self, '_large_spent', 0.0))
                self._advance_progress(finish_delta,
                                       task_names.get(task_func, task_func.__name__))
                if abort_callback and abort_callback():
                    self._abort_event.set()
                    for f in list(future_to_task):
                        f.cancel()
                    logger.info("扫描被用户中断")
                    break

        # 正常跑完所有任务后，把进度强制补到 100%（覆盖各类四舍五入/提前结束的零头）
        if not (abort_callback and abort_callback()):
            self._advance_progress(1.0, "扫描完成")

        self._progress_cb = None

        self._deduplicate_scan_results(results)

        logger.info(f"扫描完成，找到 {sum(len(items) for items in results.values())} 个可清理项目")
        return results

    @staticmethod
    def _deduplicate_scan_results(results):
        """清理类别间的完全重复路径；分析视图保持独立。"""
        seen = set()
        for category, items in results.items():
            if category in ANALYSIS_ONLY_CATEGORIES:
                continue
            unique = []
            for item in items:
                try:
                    key = os.path.normcase(os.path.realpath(item['path']))
                except (KeyError, OSError, TypeError, ValueError):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                unique.append(item)
            results[category] = unique

    def _scan_directories(self, results, category, directories, per_file=False, file_filter=None):
        """通用扫描模板：遍历目录列表，收集文件信息。

        Args:
            results: scan results dict
            category: key in results dict (e.g. 'temp', 'logs')
            directories: list of paths to scan
            per_file: if True, each file is a separate result item;
                      if False, aggregate size per-directory into one item
            file_filter: optional callable(file_path, file_name) -> bool,
                         only include files where this returns True
        """
        for scan_dir in directories:
            if not os.path.exists(scan_dir) or not self._is_safe_path(scan_dir):
                continue
            try:
                if os.path.isfile(scan_dir):
                    file_size = os.path.getsize(scan_dir)
                    if file_size > 0:
                        results[category].append({
                            'path': scan_dir,
                            'size': file_size,
                            'type': category
                        })
                    continue
                total_size = 0
                for root, _, files in os.walk(scan_dir):
                    for file in files:
                        try:
                            file_path = os.path.join(root, file)
                            if not os.path.isfile(file_path):
                                continue
                            if file_filter and not file_filter(file_path, file):
                                continue
                            file_size = os.path.getsize(file_path)
                            if per_file:
                                results[category].append({
                                    'path': file_path,
                                    'size': file_size,
                                    'type': category
                                })
                            else:
                                total_size += file_size
                        except (PermissionError, FileNotFoundError):
                            pass
                if not per_file and total_size > 0:
                    results[category].append({
                        'path': scan_dir,
                        'size': total_size,
                        'type': category
                    })
            except (PermissionError, FileNotFoundError) as e:
                logger.warning(f"无法访问 {scan_dir}: {e}")

    def _scan_temp_files(self, results):
        """扫描临时文件"""
        # 扫描Windows临时文件夹
        temp_dirs = [
            os.environ.get('TEMP', os.path.join('C:', os.sep, 'Windows', 'Temp')),
            os.path.join('C:', os.sep, 'Windows', 'Temp')
        ]

        for temp_dir in temp_dirs:
            if os.path.exists(temp_dir) and self._is_safe_path(temp_dir):
                for root, _, files in os.walk(temp_dir):
                    for file in files:
                        try:
                            file_path = os.path.join(root, file)
                            if os.path.isfile(file_path):
                                file_size = os.path.getsize(file_path)
                                results['temp'].append({
                                    'path': file_path,
                                    'size': file_size,
                                    'type': 'temp'
                                })
                        except (PermissionError, FileNotFoundError) as e:
                            logger.warning(f"无法访问文件 {file_path}: {e}")

    def _scan_recycle_bin(self, results):
        """扫描回收站"""
        recycle_bin = os.path.join('C:', os.sep, '$Recycle.Bin')
        if os.path.exists(recycle_bin):
            total_size = 0
            try:
                for root, _, files in os.walk(recycle_bin):
                    for file in files:
                        try:
                            file_path = os.path.join(root, file)
                            if os.path.isfile(file_path):
                                file_size = os.path.getsize(file_path)
                                total_size += file_size
                        except (PermissionError, FileNotFoundError):
                            pass

                if total_size > 0:
                    results['recycle'].append({
                        'path': recycle_bin,
                        'size': total_size,
                        'type': 'recycle'
                    })
            except (PermissionError, FileNotFoundError) as e:
                logger.warning(f"无法访问回收站: {e}")

    def _scan_browser_cache(self, results):
        """扫描浏览器缓存"""
        # Chrome缓存
        chrome_cache = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                                   'Google', 'Chrome', 'User Data', 'Default', 'Cache')

        # Edge缓存
        edge_cache = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                                 'Microsoft', 'Edge', 'User Data', 'Default', 'Cache')

        # Firefox缓存
        firefox_profiles = os.path.join(os.environ.get('APPDATA', ''),
                                      'Mozilla', 'Firefox', 'Profiles')

        cache_dirs = [chrome_cache, edge_cache]

        # 添加Firefox配置文件缓存
        if os.path.exists(firefox_profiles):
            try:
                for profile in os.listdir(firefox_profiles):
                    profile_cache = os.path.join(firefox_profiles, profile, 'cache2')
                    if os.path.exists(profile_cache):
                        cache_dirs.append(profile_cache)
            except (PermissionError, FileNotFoundError) as e:
                logger.warning(f"无法访问Firefox配置文件: {e}")

        # 扫描所有缓存目录
        for cache_dir in cache_dirs:
            if os.path.exists(cache_dir) and self._is_safe_path(cache_dir):
                total_size = 0
                try:
                    for root, _, files in os.walk(cache_dir):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    file_size = os.path.getsize(file_path)
                                    total_size += file_size
                            except (PermissionError, FileNotFoundError):
                                pass

                    if total_size > 0:
                        results['cache'].append({
                            'path': cache_dir,
                            'size': total_size,
                            'type': 'cache'
                        })
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问缓存目录 {cache_dir}: {e}")

    def _scan_system_logs(self, results):
        """扫描系统日志"""
        log_dirs = [
            os.path.join('C:', os.sep, 'Windows', 'Logs'),
            os.path.join('C:', os.sep, 'Windows', 'debug')
        ]

        for log_dir in log_dirs:
            if os.path.exists(log_dir) and self._is_safe_path(log_dir):
                try:
                    for root, _, files in os.walk(log_dir):
                        for file in files:
                            if file.endswith('.log') or file.endswith('.etl') or file.endswith('.dmp'):
                                try:
                                    file_path = os.path.join(root, file)
                                    if os.path.isfile(file_path):
                                        file_size = os.path.getsize(file_path)
                                        results['logs'].append({
                                            'path': file_path,
                                            'size': file_size,
                                            'type': 'logs'
                                        })
                                except (PermissionError, FileNotFoundError):
                                    pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问日志目录 {log_dir}: {e}")

    def _scan_windows_updates(self, results):
        """扫描Windows更新缓存"""
        update_dirs = [
            os.path.join('C:', os.sep, 'Windows', 'SoftwareDistribution', 'Download'),
            os.path.join('C:', os.sep, 'Windows', 'SoftwareDistribution', 'DataStore')
        ]

        for update_dir in update_dirs:
            if self._abort_event.is_set():
                return
            if os.path.exists(update_dir) and self._is_safe_path(update_dir):
                total_size = 0
                try:
                    for root, _, files in os.walk(update_dir):
                        if self._abort_event.is_set():
                            return
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    file_size = os.path.getsize(file_path)
                                    total_size += file_size
                            except (PermissionError, FileNotFoundError):
                                pass

                    if total_size > 0:
                        results['updates'].append({
                            'path': update_dir,
                            'size': total_size,
                            'type': 'updates'
                        })
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问Windows更新缓存 {update_dir}: {e}")

    def _scan_thumbnails_cache(self, results):
        """扫描缩略图缓存"""
        thumbnail_dirs = [
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'Explorer'),
            os.path.join('C:', os.sep, 'Users', os.environ.get('USERNAME', ''), 'AppData', 'Local', 'Microsoft', 'Windows', 'Explorer'),
        ]

        for thumb_dir in thumbnail_dirs:
            if os.path.exists(thumb_dir) and self._is_safe_path(thumb_dir):
                try:
                    thumb_db = os.path.join(thumb_dir, 'thumbcache_*.db')
                    for thumb_file in glob.glob(thumb_db):
                        try:
                            if os.path.isfile(thumb_file):
                                file_size = os.path.getsize(thumb_file)
                                results['thumbnails'].append({
                                    'path': thumb_file,
                                    'size': file_size,
                                    'type': 'thumbnails'
                                })
                        except (PermissionError, FileNotFoundError):
                            pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问缩略图缓存 {thumb_dir}: {e}")

    def _scan_prefetch(self, results):
        """扫描预读取文件"""
        prefetch_dir = os.path.join('C:', os.sep, 'Windows', 'Prefetch')

        if os.path.exists(prefetch_dir) and self._is_safe_path(prefetch_dir):
            try:
                for root, _, files in os.walk(prefetch_dir):
                    for file in files:
                        if file.endswith('.pf'):
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    file_size = os.path.getsize(file_path)
                                    results['prefetch'].append({
                                        'path': file_path,
                                        'size': file_size,
                                        'type': 'prefetch'
                                    })
                            except (PermissionError, FileNotFoundError):
                                pass
            except (PermissionError, FileNotFoundError) as e:
                logger.warning(f"无法访问预读取文件夹 {prefetch_dir}: {e}")

    def _scan_downloads(self, results):
        """扫描下载文件夹"""
        # 获取当前用户的下载文件夹
        download_dirs = [
            os.path.join('C:', os.sep, 'Users', os.environ.get('USERNAME', ''), 'Downloads'),
            os.path.join(os.path.expanduser('~'), 'Downloads')
        ]

        # 添加一些常见的临时下载文件类型
        temp_extensions = ['.tmp', '.temp', '.part', '.crdownload', '.download']
        old_threshold = datetime.datetime.now() - datetime.timedelta(days=30)  # 30天前的文件

        for download_dir in download_dirs:
            if os.path.exists(download_dir) and self._is_safe_path(download_dir):
                try:
                    for root, _, files in os.walk(download_dir):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    # 检查是否是临时下载文件或者超过30天的旧文件
                                    is_temp = any(file.endswith(ext) for ext in temp_extensions)

                                    # 获取文件修改时间
                                    mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                                    is_old = mod_time < old_threshold

                                    if is_temp or is_old:
                                        file_size = os.path.getsize(file_path)
                                        results['downloads'].append({
                                            'path': file_path,
                                            'size': file_size,
                                            'type': 'downloads'
                                        })
                            except (PermissionError, FileNotFoundError):
                                pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问下载文件夹 {download_dir}: {e}")

    def _scan_old_windows(self, results):
        """扫描旧Windows文件"""
        self._scan_directories(results, 'old_windows', [
            os.path.join('C:', os.sep, 'Windows.old'),
            os.path.join('C:', os.sep, '$Windows.~BT'),
            os.path.join('C:', os.sep, '$Windows.~WS'),
        ])

    def _scan_error_reports(self, results):
        """扫描错误报告"""
        self._scan_directories(results, 'error_reports', [
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Windows', 'WER'),
            os.path.join('C:', os.sep, 'Users', os.environ.get('USERNAME', ''), 'AppData', 'Local', 'Microsoft', 'Windows', 'WER'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'WER'),
        ])

    def _scan_service_packs(self, results):
        """扫描服务包备份"""
        self._scan_directories(results, 'service_packs', [
            os.path.join('C:', os.sep, 'Windows', '$NtServicePackUninstall$'),
            os.path.join('C:', os.sep, 'Windows', '$hf_mig$'),
        ])

    def _scan_hibernation_file(self, results):
        """扫描休眠文件"""
        hibernation_file = os.path.join('C:', os.sep, 'hiberfil.sys')

        if os.path.exists(hibernation_file) and self._is_safe_path(hibernation_file):
            try:
                file_size = os.path.getsize(hibernation_file)
                if file_size > 0:
                    results['hibernation'].append({
                        'path': hibernation_file,
                        'size': file_size,
                        'type': 'hibernation'
                    })
            except (PermissionError, FileNotFoundError) as e:
                logger.warning(f"无法访问休眠文件 {hibernation_file}: {e}")

    def _scan_memory_dumps(self, results):
        """扫描内存转储文件"""
        self._scan_directories(results, 'memory_dumps', [
            os.path.join('C:', os.sep, 'Windows', 'Minidump'),
            os.path.join('C:', os.sep, 'Windows', 'MEMORY.DMP'),
            os.path.join('C:', os.sep, 'Windows', 'memory.dmp'),
        ])

    def _scan_delivery_optimization(self, results):
        """扫描Windows传递优化缓存"""
        self._scan_directories(results, 'delivery_opt', [
            os.path.join('C:', os.sep, 'Windows', 'ServiceProfiles', 'NetworkService', 'AppData', 'Local', 'Microsoft', 'Windows', 'DeliveryOptimization', 'Cache'),
            os.path.join('C:', os.sep, 'Windows', 'SoftwareDistribution', 'DeliveryOptimization', 'Cache'),
        ])

    def _scan_font_cache(self, results):
        """扫描字体缓存"""
        self._scan_directories(results, 'font_cache', [
            os.path.join('C:', os.sep, 'Windows', 'ServiceProfiles', 'LocalService', 'AppData', 'Local', 'FontCache'),
            os.path.join('C:', os.sep, 'Windows', 'System32', 'FNTCACHE.DAT'),
        ])

    def _scan_installer_cache(self, results):
        """扫描安装程序缓存"""
        installer_cache_dirs = [
            os.path.join('C:', os.sep, 'Windows', 'Installer'),
            os.path.join('C:', os.sep, 'ProgramData', 'Package Cache'),
            os.path.join('C:', os.sep, 'Windows', 'Downloaded Program Files')
        ]

        # 超过90天的安装程序缓存
        old_threshold = datetime.datetime.now() - datetime.timedelta(days=90)

        for installer_dir in installer_cache_dirs:
            if os.path.exists(installer_dir) and self._is_safe_path(installer_dir):
                try:
                    for root, _, files in os.walk(installer_dir):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    # 检查文件是否超过90天未修改
                                    mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                                    if mod_time < old_threshold:
                                        file_size = os.path.getsize(file_path)
                                        results['installer_cache'].append({
                                            'path': file_path,
                                            'size': file_size,
                                            'type': 'installer_cache'
                                        })
                            except (PermissionError, FileNotFoundError):
                                pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问安装程序缓存 {installer_dir}: {e}")

    def _scan_disk_cleanup_backup(self, results):
        """扫描磁盘清理备份"""
        self._scan_directories(results, 'disk_cleanup', [
            os.path.join('C:', os.sep, 'Windows', 'System32', 'LogFiles', 'setupapi'),
            os.path.join('C:', os.sep, 'Windows', 'Temp', 'CheckSur'),
            os.path.join('C:', os.sep, 'Windows', 'Logs', 'CBS'),
        ])

    def _scan_app_cache(self, results):
        """扫描应用程序缓存"""
        # 常见应用程序缓存目录
        app_cache_dirs = [
            # Adobe缓存
            os.path.join(os.environ.get('APPDATA', ''), 'Adobe', 'Common'),
            # Office缓存
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Office', 'Recent'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Office', 'OTele'),
            # 其他常见应用缓存
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'DriveFS'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Teams', 'Cache'),
            os.path.join(os.environ.get('APPDATA', ''), 'Slack', 'Cache'),
            os.path.join(os.environ.get('APPDATA', ''), 'discord', 'Cache'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'INetCache', 'IE')
        ]

        for cache_dir in app_cache_dirs:
            if os.path.exists(cache_dir) and self._is_safe_path(cache_dir):
                try:
                    total_size = 0
                    for root, _, files in os.walk(cache_dir):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    total_size += os.path.getsize(file_path)
                            except (PermissionError, FileNotFoundError):
                                pass

                    if total_size > 0:
                        results['app_cache'].append({
                            'path': cache_dir,
                            'size': total_size,
                            'type': 'app_cache'
                        })
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问应用程序缓存 {cache_dir}: {e}")

    def _scan_media_cache(self, results):
        """扫描媒体播放器缓存"""
        # 媒体播放器缓存目录
        media_cache_dirs = [
            # Windows Media Player
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Media Player'),
            # VLC
            os.path.join(os.environ.get('APPDATA', ''), 'vlc', 'art'),
            # Spotify
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Spotify', 'Storage'),
            os.path.join(os.environ.get('APPDATA', ''), 'Spotify', 'cache'),
            # 其他媒体应用
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'Explorer', 'iconcache*')
        ]

        for cache_dir in media_cache_dirs:
            # 处理通配符模式
            if '*' in cache_dir:
                try:
                    for matched_path in glob.glob(cache_dir):
                        if os.path.exists(matched_path) and self._is_safe_path(matched_path):
                            try:
                                if os.path.isfile(matched_path):
                                    file_size = os.path.getsize(matched_path)
                                    if file_size > 0:
                                        results['media_cache'].append({
                                            'path': matched_path,
                                            'size': file_size,
                                            'type': 'media_cache'
                                        })
                            except (PermissionError, FileNotFoundError) as e:
                                logger.warning(f"无法访问媒体缓存文件 {matched_path}: {e}")
                except Exception as e:
                    logger.warning(f"处理通配符模式时出错 {cache_dir}: {e}")
                continue

            # 处理普通目录
            if os.path.exists(cache_dir) and self._is_safe_path(cache_dir):
                try:
                    total_size = 0
                    for root, _, files in os.walk(cache_dir):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    total_size += os.path.getsize(file_path)
                            except (PermissionError, FileNotFoundError):
                                pass

                    if total_size > 0:
                        results['media_cache'].append({
                            'path': cache_dir,
                            'size': total_size,
                            'type': 'media_cache'
                        })
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问媒体缓存 {cache_dir}: {e}")

    def _scan_search_index(self, results):
        """扫描搜索索引临时文件"""
        # Windows搜索索引临时文件目录
        search_index_dirs = [
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Search', 'Data', 'Temp'),
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Search', 'Data', 'Applications', 'Windows'),
            os.path.join('C:', os.sep, 'Windows', 'ServiceProfiles', 'LocalService', 'AppData', 'Local', 'Microsoft', 'Windows', 'Search')
        ]

        # 只清理临时文件和旧索引文件
        temp_extensions = ['.tmp', '.old', '.bak', '.log']

        for index_dir in search_index_dirs:
            if os.path.exists(index_dir) and self._is_safe_path(index_dir):
                try:
                    for root, _, files in os.walk(index_dir):
                        for file in files:
                            try:
                                # 只清理临时文件和旧索引文件
                                if any(file.endswith(ext) for ext in temp_extensions):
                                    file_path = os.path.join(root, file)
                                    if os.path.isfile(file_path):
                                        file_size = os.path.getsize(file_path)
                                        results['search_index'].append({
                                            'path': file_path,
                                            'size': file_size,
                                            'type': 'search_index'
                                        })
                            except (PermissionError, FileNotFoundError):
                                pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问搜索索引目录 {index_dir}: {e}")

    def _scan_backup_temp(self, results):
        """扫描备份临时文件"""
        def _older_than_30_days(file_path, file_name):
            mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
            return (datetime.datetime.now() - mod_time).days > 30

        self._scan_directories(results, 'backup_temp', [
            os.path.join('C:', os.sep, 'Windows', 'Temp', 'WindowsBackup'),
            os.path.join('C:', os.sep, 'Windows', 'Logs', 'WindowsBackup'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'WindowsBackup'),
        ], per_file=True, file_filter=_older_than_30_days)

    def _scan_update_temp(self, results):
        """扫描更新临时文件"""
        self._scan_directories(results, 'update_temp', [
            os.path.join('C:', os.sep, 'Windows', 'SoftwareDistribution', 'PostRebootEventCache'),
            os.path.join('C:', os.sep, 'Windows', 'SoftwareDistribution', 'Temp'),
            os.path.join('C:', os.sep, 'Windows', 'WinSxS', 'Temp'),
            os.path.join('C:', os.sep, 'Windows', 'Temp', 'TrustedInstaller'),
        ])

    def _scan_driver_backup(self, results):
        """扫描驱动备份"""
        self._scan_directories(results, 'driver_backup', [
            os.path.join('C:', os.sep, 'Windows', 'inf', 'OLD'),
            os.path.join('C:', os.sep, 'Windows', 'System32', 'DriverStore', 'Temp'),
        ])

    def _scan_app_crash(self, results):
        """扫描应用程序崩溃转储"""
        self._scan_directories(results, 'app_crash', [
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Windows', 'WER', 'ReportArchive'),
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Windows', 'WER', 'ReportQueue'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'CrashDumps'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'WER', 'ReportArchive'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'WER', 'ReportQueue'),
        ])

    def _scan_app_logs(self, results):
        """扫描应用程序日志"""
        # 常见应用程序日志目录
        app_log_dirs = [
            os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Teams', 'logs.txt'),
            os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Teams', 'logs'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Office', '*.log'),
            os.path.join(os.environ.get('APPDATA', ''), 'Slack', 'logs'),
            os.path.join(os.environ.get('APPDATA', ''), 'discord', 'logs')
        ]

        # 超过30天的日志文件
        old_threshold = datetime.datetime.now() - datetime.timedelta(days=30)

        for log_dir in app_log_dirs:
            # 处理通配符模式
            if '*' in log_dir:
                try:
                    for matched_path in glob.glob(log_dir):
                        if os.path.exists(matched_path) and self._is_safe_path(matched_path):
                            try:
                                if os.path.isfile(matched_path):
                                    # 检查是否是旧文件
                                    mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(matched_path))
                                    if mod_time < old_threshold:
                                        file_size = os.path.getsize(matched_path)
                                        if file_size > 0:
                                            results['app_logs'].append({
                                                'path': matched_path,
                                                'size': file_size,
                                                'type': 'app_logs'
                                            })
                            except (PermissionError, FileNotFoundError) as e:
                                logger.warning(f"无法访问应用程序日志文件 {matched_path}: {e}")
                except Exception as e:
                    logger.warning(f"处理通配符模式时出错 {log_dir}: {e}")
                continue

            # 处理普通目录
            if os.path.exists(log_dir) and self._is_safe_path(log_dir):
                try:
                    if os.path.isfile(log_dir):
                        # 如果是文件
                        mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(log_dir))
                        if mod_time < old_threshold:
                            file_size = os.path.getsize(log_dir)
                            if file_size > 0:
                                results['app_logs'].append({
                                    'path': log_dir,
                                    'size': file_size,
                                    'type': 'app_logs'
                                })
                    else:
                        # 如果是目录
                        for root, _, files in os.walk(log_dir):
                            for file in files:
                                try:
                                    file_path = os.path.join(root, file)
                                    if os.path.isfile(file_path):
                                        # 检查是否是旧文件
                                        mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                                        if mod_time < old_threshold:
                                            file_size = os.path.getsize(file_path)
                                            results['app_logs'].append({
                                                'path': file_path,
                                                'size': file_size,
                                                'type': 'app_logs'
                                            })
                                except (PermissionError, FileNotFoundError):
                                    pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问应用程序日志 {log_dir}: {e}")

    def _scan_recent_items(self, results):
        """扫描最近使用的文件列表缓存"""
        self._scan_directories(results, 'recent_items', [
            os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Recent'),
            os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Office', 'Recent'),
        ])

    def _scan_notification_cache(self, results):
        """扫描Windows通知缓存"""
        self._scan_directories(results, 'notification', [
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'Notifications'),
            os.path.join('C:', os.sep, 'Users', os.environ.get('USERNAME', ''), 'AppData', 'Local', 'Microsoft', 'Windows', 'ActionCenterCache'),
        ])

    def _scan_dns_cache(self, results):
        """扫描DNS缓存"""
        self._scan_directories(results, 'dns_cache', [
            os.path.join('C:', os.sep, 'Windows', 'System32', 'dnsrslvr.log'),
            os.path.join('C:', os.sep, 'Windows', 'System32', 'dns', 'cache.dns'),
        ])


    def _scan_printer_temp(self, results):
        """扫描打印机临时文件"""
        self._scan_directories(results, 'printer_temp', [
            os.path.join('C:', os.sep, 'Windows', 'System32', 'spool', 'PRINTERS'),
        ])

    def _scan_device_temp(self, results):
        """扫描设备临时文件"""
        self._scan_directories(results, 'device_temp', [
            os.path.join('C:', os.sep, 'Windows', 'INF', 'setupapi.dev.log'),
            os.path.join('C:', os.sep, 'Windows', 'INF', 'setupapi.log'),
            os.path.join('C:', os.sep, 'Windows', 'System32', 'LogFiles', 'setupapi'),
        ])

    def _scan_windows_defender(self, results):
        """扫描Windows Defender缓存"""
        self._scan_directories(results, 'windows_defender', [
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Windows Defender', 'Scans', 'History'),
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Windows Defender', 'Quarantine'),
            os.path.join('C:', os.sep, 'ProgramData', 'Microsoft', 'Windows Defender', 'Support'),
        ])

    def _scan_store_cache(self, results):
        """扫描Windows Store缓存"""
        self._scan_directories(results, 'store_cache', [
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Packages', 'Microsoft.WindowsStore_8wekyb3d8bbwe', 'LocalCache'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Packages', 'Microsoft.WindowsStore_8wekyb3d8bbwe', 'TempState'),
        ])

    def _scan_onedrive_cache(self, results):
        """扫描OneDrive缓存"""
        self._scan_directories(results, 'onedrive_cache', [
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'OneDrive', 'logs'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'OneDrive', 'settings', 'Personal', 'logs'),
        ])

    def _scan_downloads_immediate(self, results):
        """扫描下载文件夹 - 列出所有文件供用户逐个选择"""
        # 获取当前用户的下载文件夹
        download_dirs = [
            os.path.join('C:', os.sep, 'Users', os.environ.get('USERNAME', ''), 'Downloads'),
            os.path.join(os.path.expanduser('~'), 'Downloads')
        ]

        # 去重：两个路径可能指向同一目录
        seen_paths = set()

        for download_dir in download_dirs:
            if os.path.exists(download_dir) and self._is_safe_path(download_dir):
                try:
                    for root, _, files in os.walk(download_dir):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                normalized = os.path.normcase(os.path.abspath(file_path))
                                if normalized in seen_paths:
                                    continue
                                seen_paths.add(normalized)

                                if os.path.isfile(file_path):
                                    file_size = os.path.getsize(file_path)
                                    results['downloads'].append({
                                        'path': file_path,
                                        'size': file_size,
                                        'type': 'downloads'
                                    })
                            except (PermissionError, FileNotFoundError):
                                pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问下载文件夹 {download_dir}: {e}")

    def _scan_installer_cache_safe(self, results):
        """扫描安装程序缓存(安全版)"""
        # 安装程序缓存目录
        installer_cache_dirs = [
            os.path.join('C:', os.sep, 'Windows', 'Installer', 'Temp'),
            os.path.join('C:', os.sep, 'ProgramData', 'Package Cache', 'Temp'),
            os.path.join('C:', os.sep, 'Windows', 'Downloaded Program Files', 'Temp'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Package Cache'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Temp', 'Downloaded Installations')
        ]

        # 安全可清理的文件类型
        safe_extensions = ['.tmp', '.temp', '.msi.cache', '.exe.cache', '.log', '.old']

        # 超过30天的安装程序缓存
        very_old_threshold = datetime.datetime.now() - datetime.timedelta(days=30)  # 30天前的文件

        for installer_dir in installer_cache_dirs:
            if os.path.exists(installer_dir) and self._is_safe_path(installer_dir):
                try:
                    for root, _, files in os.walk(installer_dir):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    # 检查是否是安全可清理的文件
                                    is_safe_temp = any(file.lower().endswith(ext) for ext in safe_extensions)

                                    # 检查是否是超过365天的文件
                                    is_very_old = False
                                    if not is_safe_temp:  # 如果不是安全的临时文件，检查是否非常旧
                                        mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                                        is_very_old = mod_time < very_old_threshold

                                    if is_safe_temp or is_very_old:
                                        file_size = os.path.getsize(file_path)
                                        file_type = "temp_installer" if is_safe_temp else "very_old_installer"
                                        results['installer_cache'].append({
                                            'path': file_path,
                                            'size': file_size,
                                            'type': 'installer_cache',
                                            'subtype': file_type
                                        })
                            except (PermissionError, FileNotFoundError):
                                pass
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法访问安装程序缓存目录 {installer_dir}: {e}")

        # 特殊处理Windows Installer目录
        windows_installer = os.path.join('C:', os.sep, 'Windows', 'Installer')
        if os.path.exists(windows_installer) and self._is_safe_path(windows_installer):
            try:
                # 查找安全可清理的文件
                for root, _, files in os.walk(windows_installer):
                    for file in files:
                        try:
                            if file.lower().endswith(('.msp.cache', '.msi.cache', '.tmp', '.temp')):
                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path):
                                    file_size = os.path.getsize(file_path)
                                    results['installer_cache'].append({
                                        'path': file_path,
                                        'size': file_size,
                                        'type': 'installer_cache',
                                        'subtype': 'windows_installer_cache'
                                    })
                        except (PermissionError, FileNotFoundError):
                            pass
            except (PermissionError, FileNotFoundError) as e:
                logger.warning(f"无法访问Windows Installer目录 {windows_installer}: {e}")

    def _scan_large_files(self, results):
        """扫描C盘中的大文件。该任务最耗时，内部会定期检查中断请求。"""
        # 大文件的最小大小（100MB）
        min_size = 100 * 1024 * 1024

        # 要扫描的目录
        scan_dirs = [
            'C:\\Users',
            'C:\\Program Files',
            'C:\\Program Files (x86)',
            'C:\\ProgramData'
        ]

        # 要排除的目录
        exclude_dirs = [
            'C:\\Windows',
            'C:\\Program Files\\WindowsApps',
            'C:\\Program Files (x86)\\WindowsApps',
            'C:\\$Recycle.Bin'
        ]

        # 要排除的文件类型
        exclude_extensions = [
            '.sys', '.dll', '.exe', '.msi', '.mui', '.idx', '.cat', '.db'
        ]

        # 大文件列表
        large_files = []
        checked_count = 0
        start_time = time.time()
        # 单个大文件扫描最多运行 60 秒，避免用户等太久
        max_scan_time = 60
        # 内部进度推进：把预算按时间分成若干小步，让进度条在深度扫描期间持续爬升
        prog_spent = 0.0
        last_prog_time = start_time
        # 4 个顶级目录各占一部分，目录内再按时间缓推
        dir_count = len(scan_dirs)

        # 扫描指定目录
        for dir_idx, scan_dir in enumerate(scan_dirs):
            if os.path.exists(scan_dir) and self._is_safe_path(scan_dir):
                try:
                    for root, dirs, files in os.walk(scan_dir):
                        # 跳过排除的目录
                        dirs[:] = [d for d in dirs if os.path.join(root, d) not in exclude_dirs]

                        # 定期检查是否被用户请求暂停/停止
                        if self._abort_event.is_set():
                            logger.info("大文件扫描被用户中断")
                            return

                        if time.time() - start_time > max_scan_time:
                            logger.info(f"大文件扫描达到时间上限 {max_scan_time}s，提前结束")
                            break

                        # 每进入一个新目录，按目录进度推进一小步，让进度条持续动
                        # 预算按 (目录序号 + 目录内已耗时比例) 平滑分配
                        now = time.time()
                        if self._large_budget > 0 and now - last_prog_time >= 1.0:
                            dir_base = dir_idx / dir_count
                            dir_span = 1.0 / dir_count
                            time_ratio = min(1.0, (now - start_time) / max_scan_time)
                            target = self._large_budget * min(1.0, dir_base + dir_span * time_ratio * 2)
                            if target > prog_spent:
                                delta = target - prog_spent
                                prog_spent = target
                                self._advance_progress(delta, "大文件（深度扫描中…）")
                            last_prog_time = now

                        for file in files:
                            checked_count += 1
                            # 每检查 200 个文件就查一次中断/超时，保证响应速度
                            if checked_count % 200 == 0:
                                if self._abort_event.is_set():
                                    logger.info("大文件扫描被用户中断")
                                    return
                                if time.time() - start_time > max_scan_time:
                                    logger.info(f"大文件扫描达到时间上限 {max_scan_time}s，提前结束")
                                    break

                            try:
                                # 跳过排除的文件类型
                                if any(file.lower().endswith(ext) for ext in exclude_extensions):
                                    continue

                                file_path = os.path.join(root, file)
                                if os.path.isfile(file_path) and self._is_safe_path(file_path):
                                    file_size = os.path.getsize(file_path)
                                    if file_size >= min_size:
                                        # 获取文件修改时间
                                        mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                                        # 获取文件类型
                                        _, ext = os.path.splitext(file_path)

                                        large_files.append({
                                            'path': file_path,
                                            'size': file_size,
                                            'type': 'large_files',
                                            'modified': mod_time.strftime('%Y-%m-%d %H:%M:%S'),
                                            'extension': ext.lower() if ext else ''
                                        })
                            except (PermissionError, FileNotFoundError):
                                pass
                    if time.time() - start_time > max_scan_time:
                        break
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"无法扫描目录 {scan_dir}: {e}")

        # 按文件大小降序排序
        large_files.sort(key=lambda x: x['size'], reverse=True)

        # 只保留前100个最大的文件
        large_files = large_files[:100]

        # 添加到结果中
        results['large_files'].extend(large_files)

        # 标记是否因超时提前结束
        if time.time() - start_time > max_scan_time:
            results['_large_files_incomplete'] = True

        # 记录内部实际推进量，供 scan_system 完成时精确收尾
        self._large_spent = prog_spent

        logger.info(f"找到 {len(large_files)} 个大文件")

    def clean_selected(self, items, progress_callback=None):
        """清理选中的项目"""
        logger.info(f"开始清理 {len(items)} 个项目")

        results = {
            'cleaned_items': [],
            'errors': [],
            'freed_space': 0
        }

        storage_errors = {}
        duplicate_selection = {}
        for item in items:
            item_type = item.get('type', 'unknown')
            if item_type not in STORAGE_CLEANUP_CATEGORIES:
                continue
            path = item.get('path', '')
            normalized = os.path.normcase(os.path.abspath(path))
            error = self._storage_item_error(item)
            if error:
                storage_errors[normalized] = error
            group_id = item.get('duplicate_group')
            duplicate_count = int(item.get('duplicate_count', 0) or 0)
            if group_id and duplicate_count > 1:
                duplicate_selection.setdefault(group_id, {
                    'expected': duplicate_count,
                    'paths': set(),
                })['paths'].add(normalized)
        for group in duplicate_selection.values():
            if len(group['paths']) >= group['expected']:
                for path in group['paths']:
                    storage_errors[path] = '重复文件组至少保留一个文件，未执行删除'

        backup_session = None
        if self.options['backup'] and not self.options['simulate']:
            self.clean_old_backups()
            try:
                backup_session = self._create_backup_session()
            except OSError as e:
                reason = f"无法创建备份，已取消本次清理：{e}"
                logger.error(reason)
                results['errors'].append({
                    'path': self.backup_dir,
                    'error': reason,
                })
                return results

        try:
            for i, item in enumerate(items):
                try:
                    path = item['path']
                    item_type = item.get('type', 'unknown')

                    storage_error = storage_errors.get(
                        os.path.normcase(os.path.abspath(path)))
                    if item_type in STORAGE_CLEANUP_CATEGORIES and storage_error:
                        results['errors'].append({
                            'path': path,
                            'error': storage_error,
                        })
                        continue

                    if item_type in ANALYSIS_ONLY_CATEGORIES:
                        results['errors'].append({
                            'path': path,
                            'error': '该类别仅供查看，不能由清理程序删除'
                        })
                        continue
                    if item_type in DISABLED_CLEANUP_CATEGORIES:
                        results['errors'].append({
                            'path': path,
                            'error': '该高风险系统清理类别已停用'
                        })
                        continue

                    # 更新进度
                    if progress_callback:
                        progress_callback.emit(path, i + 1)

                    # 检查路径安全性
                    if not self._is_safe_path(path):
                        logger.warning(f"跳过不安全路径: {path}")
                        results['errors'].append({
                            'path': path,
                            'error': '不安全的路径'
                        })
                        continue
                    if os.path.isdir(path) and self._path_contains_backup_dir(path):
                        results['errors'].append({
                            'path': path,
                            'error': '该目录包含当前备份目录，为防止备份被一并删除已跳过'
                        })
                        continue

                    # 处理不同类型的项目
                    if item_type == 'recycle':
                        if backup_session:
                            results['errors'].append({
                                'path': path,
                                'error': '回收站无法生成可靠备份，已跳过；关闭备份后可单独清空'
                            })
                            continue
                        if not self.options['simulate']:
                            if not self._empty_recycle_bin():
                                raise RuntimeError('清空回收站失败')
                        results['freed_space'] += item['size']
                        results['cleaned_items'].append(path)
                    elif os.path.isdir(path):
                        # 目录内每个文件都必须先可靠备份，之后才允许删除。
                        r = self._clean_directory(path, backup_session)
                        results['freed_space'] += r['freed']
                        if r['freed'] or self.options['simulate']:
                            results['cleaned_items'].append(path)
                        if r['failed']:
                            reason_str = "、".join(f"{k}({v}个)" for k, v in r['reasons'].items())
                            failed_paths = r.get('failed_paths', [])
                            if failed_paths:
                                reason_str += "；失败路径：" + "；".join(failed_paths[:5])
                                if len(failed_paths) > 5:
                                    reason_str += f"；另有 {len(failed_paths) - 5} 个文件"
                            results['errors'].append({
                                'path': path,
                                'error': f"{r['failed']} 个文件未能删除：{reason_str}"
                            })
                    elif os.path.isfile(path):
                        freed = self._clean_file(
                            path,
                            backup_session,
                            permanent=item_type in STORAGE_CLEANUP_CATEGORIES,
                        )
                        results['freed_space'] += freed
                        results['cleaned_items'].append(path)

                except FileNotFoundError:
                    # 扫描完成后文件可能已被其他程序移走，不应作为清理失败。
                    logger.info(f"文件已不存在，跳过: {item.get('path', '')}")
                except Exception as e:
                    reason = self._friendly_error(e)
                    logger.error(f"清理项目 {item['path']} 时出错: {reason}")
                    results['errors'].append({
                        'path': item['path'],
                        'error': reason
                    })
        finally:
            self._discard_empty_backup_session(backup_session)
            if backup_session and not self.clean_old_backups():
                logger.warning("备份保留策略未能完全应用，请检查备份目录权限")

        logger.info(f"清理完成，释放空间: {results['freed_space']} 字节，错误: {len(results['errors'])}")
        return results

    def _storage_item_error(self, item):
        """校验其它磁盘结果，防止跨盘、越界或系统目录误删。"""
        path = item.get('path', '')
        scan_root = item.get('scan_root', '')
        if not path or not scan_root or not os.path.isabs(path):
            return '其它磁盘清理项缺少有效扫描范围'
        if not os.path.isabs(scan_root):
            return '其它磁盘清理项的扫描范围无效'
        if not self._path_is_within(scan_root, path):
            return '文件不在本次扫描范围内，已跳过'
        if self.same_volume(scan_root, os.environ.get('SystemDrive', 'C:')):
            return '其它磁盘清理不能操作系统盘'
        if (self.options.get('backup') and not self.options.get('simulate')
                and self.same_volume(scan_root, self.backup_dir)):
            return '备份目录与目标磁盘相同，无法真正释放目标磁盘空间'
        if not self._is_safe_path(path):
            return '文件位于系统或受保护目录，已跳过'
        return None

    def _clean_file(self, file_path, backup_session=None, permanent=False):
        """清理单个文件"""
        try:
            if not os.path.exists(file_path):
                return 0

            file_size = os.path.getsize(file_path)

            # 模拟模式下不实际删除
            if self.options['simulate']:
                logger.info(f"模拟删除文件: {file_path}")
                return file_size

            if backup_session:
                self._backup_file(file_path, backup_session)

            # 其它磁盘清理必须永久删除，否则文件仍占用原磁盘空间。
            if permanent:
                os.remove(file_path)
                logger.info(f"已永久删除文件: {file_path}")
                return file_size

            # 普通系统清理默认移动到回收站，保留 Windows 撤销能力。
            try:
                # 尝试使用 Windows API 移动到回收站。
                import ctypes
                from ctypes import windll
                from ctypes.wintypes import HWND, UINT, LPCWSTR, BOOL

                SHFileOperationW = windll.shell32.SHFileOperationW

                class SHFILEOPSTRUCTW(ctypes.Structure):
                    _fields_ = [
                        ("hwnd", HWND),
                        ("wFunc", UINT),
                        ("pFrom", LPCWSTR),
                        ("pTo", LPCWSTR),
                        ("fFlags", UINT),
                        ("fAnyOperationsAborted", BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", LPCWSTR)
                    ]

                FO_DELETE = 3
                FOF_ALLOWUNDO = 0x40  # 允许撤销（移动到回收站）
                FOF_NOCONFIRMATION = 0x10  # 不显示确认对话框
                path = file_path + '\0\0'
                fileop = SHFILEOPSTRUCTW(
                    None, FO_DELETE, path, None,
                    FOF_ALLOWUNDO | FOF_NOCONFIRMATION,
                    None, None, None
                )
                result = SHFileOperationW(ctypes.byref(fileop))
            except Exception:
                # API 不可用时只回退一次；回退失败的原始异常交给上层分类。
                os.remove(file_path)
                logger.info(f"已直接删除文件: {file_path}")
            else:
                if result == 0:
                    logger.info(f"已删除文件到回收站: {file_path}")
                else:
                    os.remove(file_path)
                    logger.info(f"已直接删除文件: {file_path}")

            return file_size
        except FileNotFoundError:
            # 文件可能在扫描后被应用程序自动清理或移动。
            logger.info(f"文件已不存在，跳过: {file_path}")
            return 0
        except PermissionError as e:
            # 文件被占用(WinError 32)或权限不足(WinError 5)：给友好中文原因
            reason = self._friendly_error(e)
            logger.warning(f"清理文件 {file_path} 失败: {reason}")
            raise RuntimeError(reason) from e
        except Exception as e:
            logger.error(f"清理文件 {file_path} 失败: {e}")
            raise

    @staticmethod
    def _friendly_error(e):
        """把 Windows 错误码翻译成用户能看懂的原因。"""
        if isinstance(e, BackupError):
            return str(e)
        winerror = getattr(e, 'winerror', None)
        error_no = getattr(e, 'errno', None)
        if winerror == 32 or error_no == errno.EBUSY:
            return "文件正在使用中，被其他程序占用"
        if winerror == 5 or error_no in (errno.EACCES, errno.EPERM):
            return "权限不足（建议以管理员身份运行）"
        if winerror in (3, 2) or error_no == errno.ENOENT:
            return "文件已不存在"
        return str(e)

    def _clean_directory(self, dir_path, backup_session=None):
        """清理目录。

        返回 dict：{'freed': 释放字节数, 'failed': 失败文件数,
        'reasons': {原因: 数量}, 'failed_paths': 失败路径列表}。
        删除失败的文件（占用/权限等）不再静默吞掉，而是统计上报给 UI 展示。
        """
        result = {'freed': 0, 'failed': 0, 'reasons': {}, 'failed_paths': []}
        try:
            if not os.path.exists(dir_path):
                return result

            # 模拟模式下不实际删除
            if self.options['simulate']:
                logger.info(f"模拟清理目录: {dir_path}")
                for root, _, files in os.walk(dir_path):
                    for file in files:
                        try:
                            file_path = os.path.join(root, file)
                            if os.path.isfile(file_path):
                                result['freed'] += os.path.getsize(file_path)
                        except (PermissionError, FileNotFoundError):
                            pass
                return result

            # 实际清理目录
            for root, dirs, files in os.walk(dir_path, topdown=False):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        if backup_session:
                            self._backup_file(file_path, backup_session)

                        # 删除文件
                        if os.path.isfile(file_path):
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            result['freed'] += file_size
                            logger.info(f"已删除文件: {file_path}")
                    except FileNotFoundError:
                        # 目录遍历期间文件被其他程序移走，属于正常竞态。
                        logger.info(f"文件已不存在，跳过: {file_path}")
                    except BackupError as e:
                        reason = str(e)
                        result['failed'] += 1
                        result['reasons'][reason] = result['reasons'].get(reason, 0) + 1
                        result['failed_paths'].append(f"{file_path}：{reason}")
                        logger.warning(f"跳过删除 {file_path}: {reason}")
                    except PermissionError as e:
                        reason = self._friendly_error(e)
                        result['failed'] += 1
                        result['reasons'][reason] = result['reasons'].get(reason, 0) + 1
                        result['failed_paths'].append(f"{file_path}：{reason}")
                        logger.warning(f"删除文件 {file_path} 失败: {reason}")
                    except OSError as e:
                        result['failed'] += 1
                        reason = self._friendly_error(e)
                        result['reasons'][reason] = result['reasons'].get(reason, 0) + 1
                        result['failed_paths'].append(f"{file_path}：{reason}")
                        logger.warning(f"删除文件 {file_path} 失败: {reason}")

                # 删除空目录
                for dir_name in dirs:
                    try:
                        dir_to_remove = os.path.join(root, dir_name)
                        if os.path.exists(dir_to_remove) and not os.listdir(dir_to_remove):
                            os.rmdir(dir_to_remove)
                            logger.info(f"已删除空目录: {dir_to_remove}")
                    except (PermissionError, FileNotFoundError, OSError) as e:
                        logger.warning(f"删除目录 {os.path.join(root, dir_name)} 失败: {e}")

            return result
        except Exception as e:
            logger.error(f"清理目录 {dir_path} 失败: {e}")
            raise

    def _empty_recycle_bin(self):
        """清空回收站（改用 Windows API，不弹窗、不依赖 PowerShell 执行策略）"""
        try:
            import ctypes
            SHERB_NOCONFIRMATION = 0x00000001
            SHERB_NOPROGRESSUI = 0x00000002
            SHERB_NOSOUND = 0x00000004
            flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
            result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
            # 回收站本为空时 API 也可能返回非0，属正常，不强制报错
            logger.info(f"已清空回收站 (SHEmptyRecycleBinW 返回: {result})")
            return True
        except Exception as e:
            logger.error(f"清空回收站失败: {e}")
            return False

    def _is_safe_path(self, path):
        """检查路径是否安全（不在系统关键目录中）

        修复：原实现用 startswith 判断受保护目录，会误伤仅前缀相同的目录
        （如 C:\\Program FilesBad），且未做大小写/分隔符归一化。
        现改为规范化后按「等于或为其子路径」判断，保持原语义：
        System32/Program Files 等整棵子树保护；Windows/ProgramData 仅保护
        根目录本身（子目录如 Windows\\Temp 仍可清理）。
        """
        try:
            norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        except Exception:
            return False  # 无法规范化的路径，宁可视为不安全

        drive, tail = os.path.splitdrive(norm)
        if drive and tail in (os.sep, '/', '\\'):
            return False

        # 非系统盘也可能包含另一套 Windows 或受保护的卷目录，整棵子树禁止清理。
        system_drive = os.path.normcase(
            os.environ.get('SystemDrive', 'C:').rstrip('\\/'))
        if drive and os.path.normcase(drive.rstrip('\\/')) != system_drive:
            top_level = tail.lstrip('\\/').replace('/', '\\').split('\\', 1)[0]
            if top_level.lower() in STORAGE_PROTECTED_ROOTS:
                return False

        # 这些目录「及其所有子目录」一律保护
        for safe_path in self.safe_paths:
            pn = os.path.normcase(os.path.normpath(os.path.abspath(safe_path)))
            if norm == pn or norm.startswith(pn + os.sep):
                return False

        # 宽泛用户/系统根目录，以及必须交给 Windows 官方维护接口的目录。
        protected_roots = [
            os.path.join('C:', os.sep, 'Users'),
            os.path.expanduser('~'),
        ]
        protected_subtrees = [
            os.path.join('C:', os.sep, 'Windows', 'WinSxS'),
            os.path.join('C:', os.sep, 'Windows', 'SoftwareDistribution'),
            os.path.join('C:', os.sep, 'Windows', 'Installer'),
            os.path.join(
                'C:', os.sep, 'ProgramData', 'Microsoft', 'Windows Defender'),
        ]
        for protected in protected_roots:
            protected_norm = os.path.normcase(
                os.path.normpath(os.path.abspath(protected)))
            if norm == protected_norm:
                return False
        for protected in protected_subtrees:
            protected_norm = os.path.normcase(
                os.path.normpath(os.path.abspath(protected)))
            if norm == protected_norm or norm.startswith(protected_norm + os.sep):
                return False

        # 备份目录及其子路径永远不能作为普通清理项处理。
        backup_norm = os.path.normcase(
            os.path.normpath(os.path.abspath(self.backup_dir)))
        if norm == backup_norm or norm.startswith(backup_norm + os.sep):
            return False

        # 这些根目录「本身」保护（不拦截其子目录，否则 Windows\\Temp 等无法清理）
        system_roots = [
            os.path.join('C:', os.sep, 'Windows'),
            os.path.join('C:', os.sep, 'Program Files'),
            os.path.join('C:', os.sep, 'Program Files (x86)'),
            os.path.join('C:', os.sep, 'ProgramData')
        ]
        for sys_dir in system_roots:
            if norm == os.path.normcase(os.path.normpath(os.path.abspath(sys_dir))):
                return False

        return True

    def _path_contains_backup_dir(self, path):
        """目录清理前判断其范围内是否包含当前备份目录。"""
        try:
            norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
            backup_norm = os.path.normcase(
                os.path.normpath(os.path.abspath(self.backup_dir)))
            return norm == backup_norm or backup_norm.startswith(norm + os.sep)
        except (OSError, TypeError, ValueError):
            return True
