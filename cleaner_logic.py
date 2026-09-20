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
import stat
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


# 深度诊断条目专用类型：只读展示，绝不能进入删除路径。
DIAGNOSTIC_ITEM_TYPE = 'diagnostic_only'

# 这些类别只能用于分析展示，任何调用方都不能把它们交给删除核心。
ANALYSIS_ONLY_CATEGORIES = frozenset({
    'large_files', 'ai_models', 'docker_data', DIAGNOSTIC_ITEM_TYPE,
})

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
    'patch_cache',
    'event_logs',
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


# ─── 深度诊断：统计清理工具覆盖不到的占用（只读，绝不删除任何文件）──────────

DIAGNOSTIC_GROUPS = {
    'db': '数据库日志与数据',
    'appdata': '应用数据与运行时',
    'cloud': '云盘同步目录',
    'system': '系统与镜像残留',
    'ide': 'IDE / 编辑器缓存',
}
DIAGNOSTIC_GROUP_ORDER = ('db', 'appdata', 'cloud', 'system', 'ide')

_DIAG_TOP_THRESHOLD = 200 * 1024 * 1024   # 动态 Top 榜的子目录门槛
_DIAG_TOP_LIMIT = 12

# 云端「按需文件」属性：读取会触发下载，统计时必须跳过。
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
# reparse point（junction / symlink）属性，避免重复遍历。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _diag_env(env=None):
    """收集诊断需要的根路径。全部走环境变量，兼容文件夹重定向与测试替换。"""
    src = env if env is not None else os.environ
    windows = src.get('SystemRoot') or src.get('WINDIR') or r'C:\Windows'
    drive = os.path.splitdrive(windows)[0] + os.sep if os.path.splitdrive(windows)[0] else 'C:\\'
    return {
        'appdata': src.get('APPDATA', ''),
        'local': src.get('LOCALAPPDATA', ''),
        'profile': src.get('USERPROFILE', ''),
        'programdata': src.get('ProgramData') or src.get('PROGRAMDATA') or r'C:\ProgramData',
        'programfiles': src.get('ProgramFiles') or src.get('PROGRAMFILES') or r'C:\Program Files',
        'windows': windows,
        'drive': drive,
    }


def build_diagnostic_probes(env=None):
    """构建深度诊断探针表。

    每个探针是 dict：group/name/kind/suggestion 为通用字段，kind 取值为
    'dir' | 'glob' | 'files' | 'dynamic_top' | 'note_only'。
    路径全部来自环境变量，便于在测试中替换为临时目录。
    """
    e = _diag_env(env)
    appdata, local, profile = e['appdata'], e['local'], e['profile']
    programdata, programfiles = e['programdata'], e['programfiles']
    windows, drive = e['windows'], e['drive']
    probes = []

    def add(group, name, kind, suggestion, path='', **extra):
        probe = {'group': group, 'name': name, 'kind': kind,
                 'path': path, 'suggestion': suggestion}
        probe.update(extra)
        probes.append(probe)

    # ---------- 1. 数据库日志与数据 ----------
    for server_dir in sorted(glob.glob(os.path.join(programdata, 'MySQL', 'MySQL Server*'))):
        data_dir = os.path.join(server_dir, 'Data')
        if not os.path.isdir(data_dir):
            continue
        product = os.path.basename(server_dir)
        add('db', f'MySQL 数据目录（{product}）', 'dir',
            '数据库数据，请勿手动删除；要释放空间优先处理二进制日志（binlog）。',
            path=data_dir, budget=60)
        add('db', f'MySQL 二进制日志 binlog（{product}）', 'glob',
            '关闭日志：在 my.ini 注释 log-bin 并重启 MySQL；'
            '或设置 binlog_expire_logs_seconds 控制保留天数。'
            '清理用 SQL：PURGE BINARY LOGS BEFORE NOW() - INTERVAL 3 DAY; '
            '切勿手动删除 Data 目录下的 -bin.0000xx 文件。',
            path=data_dir, pattern='*-bin.[0-9]*',
            inspect='mysql_log_bin', server_dir=server_dir)
    for server_dir in sorted(glob.glob(os.path.join(programdata, 'MariaDB*'))):
        data_dir = os.path.join(server_dir, 'data')
        if os.path.isdir(data_dir):
            add('db', f'MariaDB 数据目录（{os.path.basename(server_dir)}）', 'dir',
                '数据库数据，请勿手动删除；二进制日志可用 PURGE BINARY LOGS 清理。',
                path=data_dir, budget=60)
    for pg_data in sorted(glob.glob(os.path.join(programfiles, 'PostgreSQL', '*', 'data'))):
        if os.path.isdir(pg_data):
            add('db', 'PostgreSQL 数据目录', 'dir',
                '数据库数据，请勿手动删除；可用 VACUUM 回收空间。', path=pg_data, budget=60)
    for mssql in sorted(glob.glob(os.path.join(programfiles, 'Microsoft SQL Server', '*', 'MSSQL', 'DATA'))):
        if os.path.isdir(mssql):
            add('db', 'SQL Server 数据库文件', 'dir',
                '数据库数据，请勿手动删除；收缩请用 DBCC SHRINKDATABASE。',
                path=mssql, budget=60)

    # ---------- 2. 应用数据与运行时 ----------
    if appdata:
        add('appdata', 'Roaming 应用数据', 'dynamic_top',
            '应用配置与数据：缓存类可清，config/配置目录请保留；也可迁移到其它磁盘。',
            base=appdata, threshold=_DIAG_TOP_THRESHOLD, limit=_DIAG_TOP_LIMIT)
    if local:
        add('appdata', 'LocalAppData', 'dynamic_top',
            '应用本地数据：缓存类可清，config/配置目录请保留；也可迁移到其它磁盘。',
            base=local, threshold=_DIAG_TOP_THRESHOLD, limit=_DIAG_TOP_LIMIT)
    if profile:
        for label, parts in (
            ('nvm（Node 版本）', ('nvm',)),
            ('npm 缓存', ('.npm',)),
            ('.nuget 包缓存', ('.nuget', 'packages')),
            ('.gradle 构建缓存', ('.gradle',)),
            ('.m2 仓库缓存', ('.m2',)),
        ):
            add('appdata', label, 'dir',
                '运行时 / 构建缓存，可安全清理或迁移到其它磁盘。',
                path=os.path.join(profile, *parts))
    if local:
        for label, parts in (
            ('pip 缓存', ('pip', 'cache')),
            ('uv 缓存', ('uv',)),
            ('ms-playwright 浏览器', ('ms-playwright',)),
            ('npm-cache', ('npm-cache',)),
        ):
            add('appdata', label, 'dir',
                '包管理器 / 运行时缓存，可安全清理，需要时会自动重新下载。',
                path=os.path.join(local, *parts))

    # ---------- 3. 云盘同步目录 ----------
    if profile:
        for label, parts in (
            ('WPS Cloud Files', ('WPS Cloud Files',)),
            ('WPSDrive', ('WPSDrive',)),
            ('OneDrive', ('OneDrive',)),
            ('坚果云', ('Nutstore',)),
            ('Dropbox', ('Dropbox',)),
            ('百度网盘下载', ('BaiduNetdiskDownload',)),
        ):
            add('cloud', label, 'dir',
                '云盘同步 / 占位目录：请在客户端内改存储盘或「释放空间」，不要直接删除文件；'
                '主界面只能清理其中部分日志缓存。',
                path=os.path.join(profile, *parts), budget=45)
        for extra in sorted(glob.glob(os.path.join(profile, 'OneDrive*'))):
            if os.path.basename(extra).lower() != 'onedrive':
                add('cloud', f'OneDrive（{os.path.basename(extra)}）', 'dir',
                    '云盘同步目录：请在 OneDrive 设置中改存储盘或「释放空间」。',
                    path=extra, budget=45)

    # ---------- 4. 系统与镜像残留 ----------
    add('system', 'WinSxS 组件存储', 'dir',
        '请勿手动删除。用管理员执行：'
        'DISM /Online /Cleanup-Image /StartComponentCleanup',
        path=os.path.join(windows, 'WinSxS'), budget=70, max_dirs=200000)
    add('system', 'Windows\\Installer 安装缓存', 'dir',
        '请勿手动删除，可能导致程序无法卸载 / 修复；用官方工具处理。',
        path=os.path.join(windows, 'Installer'), budget=45)
    add('system', 'Windows.old（旧系统）', 'dir',
        '用「设置 → 系统 → 存储 → 临时文件」或磁盘清理删除。',
        path=os.path.join(drive, 'Windows.old'), budget=45)
    add('system', '$WINDOWS.~BT（升级临时）', 'dir',
        '系统升级残留，可用磁盘清理删除。',
        path=os.path.join(drive, '$WINDOWS.~BT'), budget=30)
    add('system', '页面文件 / 休眠文件', 'files',
        '页面文件由系统管理；不需要休眠可用 `powercfg /h off` 关闭以释放 hiberfil.sys。',
        paths=[os.path.join(drive, 'pagefile.sys'),
               os.path.join(drive, 'hiberfil.sys'),
               os.path.join(drive, 'swapfile.sys')])
    add('system', '系统还原点 / 卷影副本', 'note_only',
        '需管理员查看与调整：vssadmin list shadowstorage；'
        '可在「系统属性 → 系统保护」限制其最大占用。',
        note='需管理员权限查看')
    if local:
        add('system', 'Android SDK 系统镜像', 'dir',
            'Android 模拟器系统镜像：不用的镜像可在 SDK Manager 中删除。',
            path=os.path.join(local, 'Android', 'Sdk', 'system-images'), budget=45)
    add('system', 'Android 模拟器镜像（应用内置）', 'dir',
        '应用内置的模拟器镜像：不使用该模拟器可直接卸载对应组件。',
        path=os.path.join(programfiles, 'MobileAppEngine'), budget=30)
    add('system', 'Docker 安装镜像 / 资源', 'glob',
        'Docker 自带资源文件，随 Docker 安装存在；卸载 Docker 即可回收。',
        path=os.path.join(programfiles, 'Docker', 'Docker', 'resources'), pattern='*.iso')
    for label, parts in (
        ('驱动 / 更新包残留 (Comms)', ('Comms',)),
        ('Intel 驱动下载缓存 (DSA)', ('Intel', 'DSA', 'Downloads')),
        ('Visual Studio 包缓存 (Package Cache)', ('Package Cache',)),
    ):
        add('system', label, 'dir',
            '下载的驱动 / 更新包残留，确认无需回滚后可删除。',
            path=os.path.join(programdata, *parts), budget=30)

    # ---------- 5. IDE / 编辑器缓存 ----------
    if local:
        add('ide', 'JetBrains 本地数据', 'dynamic_top',
            '其中 caches/log/index/tmp 可由主界面「IDE开发工具缓存」清理；'
            'config/system/plugins 请保留。',
            base=os.path.join(local, 'JetBrains'),
            threshold=100 * 1024 * 1024, limit=_DIAG_TOP_LIMIT)
    if appdata:
        add('ide', 'JetBrains 配置数据', 'dynamic_top',
            '配置 / 插件目录，请保留；如需迁移可在 IDE 内更改数据目录。',
            base=os.path.join(appdata, 'JetBrains'),
            threshold=100 * 1024 * 1024, limit=_DIAG_TOP_LIMIT)
    if profile:
        add('ide', '.vscode 扩展', 'dir',
            'VS Code 扩展目录：按需保留，删除后需重新安装。',
            path=os.path.join(profile, '.vscode', 'extensions'))
        add('ide', '.eclipse', 'dir',
            'Eclipse 工作区 / 缓存数据。',
            path=os.path.join(profile, '.eclipse'))
    for base_dir, label in ((appdata, 'Roaming'), (local, 'Local')):
        if not base_dir:
            continue
        for app in ('Code', 'Code - Insiders', 'CodeBuddy CN', 'Trae CN',
                    'Qoder', 'Cursor'):
            add('ide', f'{app}（{label}）', 'dir',
                '编辑器数据：Cache/CachedData/logs 类可由主界面清理，其余为配置请保留。',
                path=os.path.join(base_dir, app))
    if local:
        add('ide', 'Sublime Text 缓存', 'dir',
            '编辑器缓存与插件数据，Cache 目录可安全清理。',
            path=os.path.join(local, 'Sublime Text'))
    return probes


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
            if abort_callback and abort_callback():
                return None, True
            with open(path, 'rb') as handle:
                if quick:
                    # 这里只是预筛选；最终仍会做完整 SHA-256，因此采样越小越快且不会产生误报。
                    sample_size = 8 * 1024
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

    def _hash_storage_records(self, executor, records, size, quick,
                              abort_callback=None, completed_callback=None,
                              max_pending=2):
        """使用固定大小的任务窗口哈希，避免为整盘候选一次性创建 Future。"""
        record_iter = iter(records)
        pending = {}

        def submit_next():
            try:
                record = next(record_iter)
            except StopIteration:
                return False
            future = executor.submit(
                self._file_hash,
                record['path'], size, quick, abort_callback,
            )
            pending[future] = record
            return True

        for _ in range(max(1, int(max_pending))):
            if not submit_next():
                break
        hashed = []
        while pending:
            done, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                record = pending.pop(future)
                digest, aborted = future.result()
                if completed_callback:
                    completed_callback(record, digest)
                if aborted:
                    for remaining in pending:
                        remaining.cancel()
                    return hashed, True
                if digest:
                    hashed.append((record, digest))
                submit_next()
        return hashed, False

    def scan_storage(self, scan_root, min_large_size=1024 * 1024 * 1024,
                     min_duplicate_size=1024 * 1024, find_large=True,
                     find_duplicates=True, abort_callback=None,
                     progress_callback=None, phase_callback=None):
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
            'duplicate_scan': {
                'enabled': bool(find_duplicates),
                'min_size': min_duplicate_size,
                'eligible_files': 0,
                'size_matched_files': 0,
                'quick_hashed_files': 0,
                'exact_hashed_files': 0,
                'quick_match_files': 0,
                'quick_match_groups': 0,
                'exact_candidates': 0,
                'unreadable_files': 0,
            },
        }
        duplicate_by_size = {}
        seen_files = set()
        backup_norm = os.path.normcase(
            os.path.normpath(os.path.realpath(self.backup_dir)))
        directories = [(root, True)]

        if phase_callback:
            phase_callback('正在枚举磁盘文件')

        while directories:
            current_root, is_scan_root = directories.pop()
            try:
                entries = os.scandir(current_root)
            except (PermissionError, FileNotFoundError, OSError):
                continue
            with entries:
                for entry in entries:
                    if abort_callback and abort_callback():
                        result['aborted'] = True
                        return result
                    path = entry.path
                    try:
                        stat_result = entry.stat(follow_symlinks=False)
                        attributes = getattr(stat_result, 'st_file_attributes', 0)
                        if stat.S_ISLNK(stat_result.st_mode) or attributes & 0x400:
                            continue
                        if stat.S_ISDIR(stat_result.st_mode):
                            directory_norm = os.path.normcase(os.path.normpath(path))
                            if (directory_norm == backup_norm
                                    or directory_norm.startswith(backup_norm + os.sep)):
                                continue
                            if (is_scan_root
                                    and entry.name.lower() in STORAGE_PROTECTED_ROOTS):
                                continue
                            directories.append((path, False))
                            continue
                        if not stat.S_ISREG(stat_result.st_mode):
                            continue
                        identity = ((stat_result.st_dev, stat_result.st_ino)
                                    if stat_result.st_ino else
                                    ('path', os.path.normcase(os.path.normpath(path))))
                        if identity in seen_files:
                            continue
                        seen_files.add(identity)
                        size = stat_result.st_size
                        result['scanned_files'] += 1
                        result['scanned_size'] += size
                        is_large = find_large and size >= min_large_size
                        is_duplicate = find_duplicates and size >= min_duplicate_size
                        if progress_callback and result['scanned_files'] % 100 == 0:
                            progress_callback(path, result['scanned_files'])
                        if not is_large and not is_duplicate:
                            continue
                        record = {
                            'path': path,
                            'size': size,
                            'scan_root': root,
                            'modified': datetime.datetime.fromtimestamp(
                                stat_result.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                            'extension': os.path.splitext(entry.name)[1].lower(),
                        }
                        if is_large:
                            large_item = dict(record, type='storage_large_file')
                            record['large_item'] = large_item
                            result['large_files'].append(large_item)
                        if is_duplicate:
                            duplicate_by_size.setdefault(size, []).append(record)
                            result['duplicate_scan']['eligible_files'] += 1
                    except (PermissionError, FileNotFoundError):
                        continue
                    except OSError as e:
                        if len(result['errors']) < 100:
                            result['errors'].append({'path': path, 'error': str(e)})

        result['large_files'].sort(key=lambda item: item['size'], reverse=True)
        if not find_duplicates or result['aborted']:
            return result

        duplicate_groups = []
        result['duplicate_groups'] = duplicate_groups
        size_groups = [
            (size, records) for size, records in duplicate_by_size.items()
            if len(records) > 1
        ]
        stats = result['duplicate_scan']
        stats['size_matched_files'] = sum(
            len(records) for _, records in size_groups)
        unreadable_paths = set()

        def make_progress_callback(stage, total):
            completed = 0
            report_step = max(1, total // 100)

            def completed_one(record, digest):
                nonlocal completed
                completed += 1
                if digest is None:
                    unreadable_paths.add(record['path'])
                    if len(result['errors']) < 100:
                        result['errors'].append({
                            'path': record['path'],
                            'error': '无法读取文件内容，已跳过重复校验',
                        })
                if (phase_callback
                        and (completed == total or completed % report_step == 0)):
                    phase_callback(f'{stage}（{completed}/{total}）')

            return completed_one

        if phase_callback:
            phase_callback(
                f"正在快速筛选重复文件（0/{stats['size_matched_files']}）")
        quick_groups = []
        quick_executor = None
        exact_executor = None
        try:
            if size_groups:
                # 快速采样只读取文件首尾少量数据，适合稍高并发；完整哈希仍限制为 2 路。
                quick_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
            quick_progress = make_progress_callback(
                '正在快速筛选重复文件', stats['size_matched_files'])
            for size, same_size_records in size_groups:
                hashed, aborted = self._hash_storage_records(
                    quick_executor, same_size_records, size, True, abort_callback,
                    quick_progress,
                    max_pending=4,
                )
                stats['quick_hashed_files'] += len(hashed)
                if aborted:
                    result['aborted'] = True
                    return result
                by_quick_digest = {}
                for record, digest in hashed:
                    by_quick_digest.setdefault(digest, []).append(record)
                quick_groups.extend(
                    (size, records) for records in by_quick_digest.values()
                    if len(records) > 1
                )
                stats['quick_match_files'] = sum(
                    len(records) for _, records in quick_groups)
                stats['quick_match_groups'] = len(quick_groups)

            exact_total = sum(len(records) for _, records in quick_groups)
            stats['exact_candidates'] = exact_total
            if phase_callback:
                phase_callback(f'正在校验重复文件内容（0/{exact_total}）')
            exact_progress = make_progress_callback(
                '正在校验重复文件内容', exact_total)
            if quick_groups:
                exact_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            for size, quick_records in quick_groups:
                groups_before = len(duplicate_groups)
                hashed, aborted = self._hash_storage_records(
                    exact_executor,
                    quick_records, size, False, abort_callback,
                    exact_progress,
                )
                stats['exact_hashed_files'] += len(hashed)
                if aborted:
                    result['aborted'] = True
                    return result
                exact_groups = {}
                for record, digest in hashed:
                    exact_groups.setdefault(digest, []).append(record)
                for digest, exact_records in exact_groups.items():
                    if len(exact_records) < 2:
                        continue
                    exact_records.sort(
                        key=lambda item: (item['modified'], item['path']))
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
                group_count = len(duplicate_groups)
                if (phase_callback and group_count > groups_before
                        and (group_count <= 10 or group_count % 100 == 0)):
                    phase_callback(
                        f'正在校验重复文件内容（已确认 {group_count} 组）')
        finally:
            stats['unreadable_files'] = len(unreadable_paths)
            duplicate_groups.sort(
                key=lambda group: group['reclaimable_size'], reverse=True)
            for executor in (quick_executor, exact_executor):
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
        return result

    # ==================== 深度诊断（只读） ====================

    def scan_diagnostics(self, progress_callback=None, abort_callback=None,
                         probes=None, deadline_seconds=180, probe_budget=30):
        """只读诊断：统计清理工具覆盖不到的占用大户。

        返回 `list[dict]`，字段为
        group/name/path/size/file_count/exists/accessible/incomplete/suggestion/note。

        安全性：返回的条目类型是 `DIAGNOSTIC_ITEM_TYPE`，而该类型已加入
        ANALYSIS_ONLY_CATEGORIES，clean_selected() 会以「仅供查看」为由拒绝，
        因此这些结果天然无法进入删除路径。本方法不创建、不修改、不删除任何文件。

        每个探针有独立时间预算 probe_budget 秒（可用探针的 'budget' 覆盖），
        超预算的探针会被标记 incomplete=True 而不是静默给出偏小的数字。
        """
        probe_list = list(probes) if probes is not None else build_diagnostic_probes()
        global_deadline = (time.monotonic() + deadline_seconds) if deadline_seconds else None
        total = max(1, len(probe_list))
        results = []
        for index, probe in enumerate(probe_list):
            if abort_callback and abort_callback():
                break
            if progress_callback:
                progress_callback(probe.get('name', ''), int(index / total * 100))
            budget = probe.get('budget', probe_budget)
            probe_deadline = time.monotonic() + budget
            if global_deadline is not None:
                probe_deadline = min(probe_deadline, global_deadline)
            try:
                results.extend(self._run_diagnostic_probe(probe, probe_deadline, abort_callback))
            except Exception as exc:  # 单个探针异常不应中断整轮诊断
                logger.warning(f"诊断探针失败（{probe.get('name')}）: {exc}")
                results.append({
                    'type': DIAGNOSTIC_ITEM_TYPE,
                    'group': probe.get('group', ''),
                    'name': probe.get('name', ''),
                    'path': str(probe.get('path', '')),
                    'size': 0,
                    'file_count': 0,
                    'exists': True,
                    'accessible': False,
                    'incomplete': True,
                    'failed': True,
                    'suggestion': probe.get('suggestion', ''),
                    'note': f'诊断失败：{exc}',
                })
        if progress_callback:
            progress_callback('', 100)
        return results

    def _run_diagnostic_probe(self, probe, deadline, abort_callback):
        """执行单个探针，返回 0..N 个只读诊断条目。"""
        item = {
            'type': DIAGNOSTIC_ITEM_TYPE,
            'group': probe.get('group', ''),
            'name': probe.get('name', ''),
            'path': probe.get('path', ''),
            'size': 0,
            'file_count': 0,
            'exists': False,
            'accessible': True,
            'incomplete': False,
            'failed': False,
            'suggestion': probe.get('suggestion', ''),
            'note': self._diagnose_note(probe),
        }
        kind = probe.get('kind', 'dir')

        if kind == 'note_only':
            item['exists'] = True
            item['note'] = probe.get('note') or '需管理员权限查看'
            return [item]

        if kind == 'dynamic_top':
            return self._diagnose_dynamic_top(probe, item, deadline, abort_callback)

        if kind == 'files':
            existing = [p for p in (probe.get('paths') or []) if os.path.exists(p)]
            if not existing:
                return [item]
            item['exists'] = True
            item['size'], item['file_count'] = self._sum_file_sizes(existing)
            item['path'] = ' | '.join(existing)
            return [item]

        if kind == 'glob':
            directory = probe.get('path', '')
            pattern = os.path.join(directory, probe.get('pattern', '*'))
            if not directory or not os.path.isdir(directory):
                return [item]
            item['exists'] = True
            matches = glob.glob(pattern)
            item['size'], item['file_count'] = self._sum_file_sizes(matches)
            notes = [text for text in (item['note'], f'匹配 {len(matches)} 个文件') if text]
            item['note'] = '；'.join(notes)
            return [item]

        # kind == 'dir'
        path = probe.get('path', '')
        if not path or not os.path.isdir(path):
            return [item]
        item['exists'] = True
        info = self._measure_tree(path, deadline, abort_callback,
                                  max_dirs=probe.get('max_dirs'))
        item['size'] = info['size']
        item['file_count'] = info['file_count']
        item['accessible'] = info['root_accessible']
        item['incomplete'] = info['incomplete']
        if info['cloud_placeholder']:
            item['note'] = '含云端占位文件，实际更大（未触发下载）'
        return [item]

    def _diagnose_dynamic_top(self, probe, base_item, deadline, abort_callback):
        """枚举一级子目录，返回占用超过门槛、按大小降序的前 N 项。"""
        base = probe.get('base', '')
        if not base or not os.path.isdir(base):
            return []
        threshold = probe.get('threshold', _DIAG_TOP_THRESHOLD)
        limit = probe.get('limit', _DIAG_TOP_LIMIT)
        try:
            entries = list(os.scandir(base))
        except (PermissionError, FileNotFoundError, OSError):
            item = dict(base_item)
            item.update({'exists': True, 'path': base, 'accessible': False,
                         'note': '无权限读取'})
            return [item]

        found = []
        for entry in entries:
            if abort_callback and abort_callback():
                break
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            info = self._measure_tree(entry.path, deadline, abort_callback)
            if info['root_accessible'] and info['size'] >= threshold:
                found.append((entry.path, info))
        found.sort(key=lambda pair: pair[1]['size'], reverse=True)

        items = []
        for path, info in found[:limit]:
            item = dict(base_item)
            item.update({
                'name': f"{base_item['name']} · {os.path.basename(path)}",
                'path': path,
                'size': info['size'],
                'file_count': info['file_count'],
                'exists': True,
                'accessible': True,
                'incomplete': info['incomplete'],
            })
            if info['cloud_placeholder']:
                item['note'] = '含云端占位文件，实际更大（未触发下载）'
            items.append(item)
        if len(found) > limit:
            rest = found[limit:]
            item = dict(base_item)
            item.update({
                'name': f"{base_item['name']} · 其余 {len(rest)} 个子目录",
                'path': base,
                'size': sum(pair[1]['size'] for pair in rest),
                'file_count': sum(pair[1]['file_count'] for pair in rest),
                'exists': True,
                'accessible': True,
                'incomplete': True,
                'note': '仅列出占用最大的若干项',
            })
            items.append(item)
        return items

    def _measure_tree(self, root, deadline=None, abort_callback=None, max_dirs=None):
        """只读统计目录占用。

        跳过 reparse point（junction/symlink）并按 (st_dev, st_ino) 去重硬链接，
        否则 WinSxS 等硬链接会把大小虚报数倍。跳过云端占位文件以免触发下载。
        """
        info = {'size': 0, 'file_count': 0, 'root_accessible': True,
                'incomplete': False, 'cloud_placeholder': False}
        try:
            with os.scandir(root):
                pass
        except (PermissionError, FileNotFoundError, OSError):
            info['root_accessible'] = False
            return info

        seen = set()
        stack = [root]
        dir_count = 0
        while stack:
            if abort_callback and abort_callback():
                info['incomplete'] = True
                break
            if deadline is not None and time.monotonic() > deadline:
                info['incomplete'] = True
                break
            if max_dirs is not None and dir_count >= max_dirs:
                info['incomplete'] = True
                break
            current = stack.pop()
            dir_count += 1
            try:
                entries = os.scandir(current)
            except (PermissionError, FileNotFoundError, OSError):
                continue
            with entries:
                for entry in entries:
                    if abort_callback and abort_callback():
                        info['incomplete'] = True
                        return info
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
                    attributes = getattr(st, 'st_file_attributes', 0)
                    if stat.S_ISLNK(st.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    if attributes & _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
                        info['cloud_placeholder'] = True
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    identity = ((st.st_dev, st.st_ino) if st.st_ino
                                else ('path', os.path.normcase(os.path.normpath(entry.path))))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    info['size'] += st.st_size
                    info['file_count'] += 1
        return info

    @staticmethod
    def _sum_file_sizes(paths):
        """只读累加一组文件的字节数与文件数。"""
        total = 0
        count = 0
        for path in paths:
            try:
                st = os.stat(path)
            except (PermissionError, FileNotFoundError, OSError):
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
                count += 1
        return total, count

    @staticmethod
    def _diagnose_note(probe):
        """探针的动态备注（目前仅用于反映 my.ini 里 log-bin 的开关状态）。"""
        if probe.get('inspect') == 'mysql_log_bin':
            server_dir = probe.get('server_dir', '')
            if server_dir:
                return CleanerLogic._mysql_log_bin_note(server_dir)
        return probe.get('note', '')

    @staticmethod
    def _mysql_log_bin_note(server_dir):
        """读取 my.ini 判断二进制日志是否仍处于开启状态。"""
        ini = os.path.join(server_dir, 'my.ini')
        try:
            with open(ini, 'r', encoding='utf-8', errors='ignore') as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped or stripped[0] in '#;':
                        continue
                    if stripped.startswith('log-bin') or stripped.startswith('log_bin'):
                        return '检测到 my.ini 中 log-bin 仍开启，这些日志会持续增长。'
            return 'my.ini 中未发现开启的 log-bin，应该不会再增长。'
        except OSError:
            return ''

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

            # IDE / 开发工具
            'ide_cache': [],     # IDE缓存
            'dev_pkg_cache': [], # 开发包管理器缓存

            # AI / 大模型
            'ai_cache': [],      # AI应用缓存
            'ai_models': [],     # AI模型文件(仅分析)

            # 通讯社交
            'messaging_cache': [], # 通讯应用缓存

            # 浏览器补充 + 游戏娱乐
            'browser_extra': [], # 其它浏览器缓存
            'gaming_cache': [],  # 游戏娱乐缓存

            # 工具 / 办公
            'tool_cache': [],    # 办公工具缓存
            'docker_data': [],   # Docker数据(仅分析)
            'gpu_shader_cache': [],
            'patch_cache': [],
            'event_logs': [],
            'wxwork_cache': [],
            'electron_cache': [],
            'service_worker_cache': [],
            'dotnet_cache': [],

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
            self._scan_downloads,
            self._scan_installer_cache_safe,
            self._scan_delivery_optimization,
            self._scan_ide_cache,
            self._scan_dev_package_cache,
            self._scan_ai_app_cache,
            self._scan_ai_models,
            self._scan_messaging_cache,
            self._scan_browser_extra,
            self._scan_gaming_cache,
            self._scan_tool_cache,
            self._scan_docker_data,
            self._scan_gpu_shader_cache,
            self._scan_patch_cache,
            self._scan_event_logs,
            self._scan_wxwork_cache,
            self._scan_electron_cache,
            self._scan_service_worker_cache,
            self._scan_dotnet_cache,
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
            self._scan_downloads: "下载文件夹",
            self._scan_installer_cache_safe: "安装程序缓存",
            self._scan_delivery_optimization: "Windows传递优化缓存",
            self._scan_ide_cache: "IDE开发工具缓存",
            self._scan_dev_package_cache: "开发包管理器缓存",
            self._scan_ai_app_cache: "AI应用缓存",
            self._scan_ai_models: "AI模型文件",
            self._scan_messaging_cache: "通讯应用缓存",
            self._scan_browser_extra: "其它浏览器缓存",
            self._scan_gaming_cache: "游戏娱乐缓存",
            self._scan_tool_cache: "办公工具缓存",
            self._scan_docker_data: "Docker数据",
            self._scan_gpu_shader_cache: "GPU着色器缓存",
            self._scan_patch_cache: "Windows补丁缓存",
            self._scan_event_logs: "Windows事件日志",
            self._scan_wxwork_cache: "企业微信缓存",
            self._scan_electron_cache: "Electron应用缓存",
            self._scan_service_worker_cache: "浏览器ServiceWorker缓存",
            self._scan_dotnet_cache: ".NET缓存",
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
            self._scan_downloads: 'downloads',
            self._scan_installer_cache_safe: 'installer_cache',
            self._scan_delivery_optimization: 'delivery_opt',
            self._scan_ide_cache: 'ide_cache',
            self._scan_dev_package_cache: 'dev_pkg_cache',
            self._scan_ai_app_cache: 'ai_cache',
            self._scan_ai_models: 'ai_models',
            self._scan_messaging_cache: 'messaging_cache',
            self._scan_browser_extra: 'browser_extra',
            self._scan_gaming_cache: 'gaming_cache',
            self._scan_tool_cache: 'tool_cache',
            self._scan_docker_data: 'docker_data',
            self._scan_gpu_shader_cache: 'gpu_shader_cache',
            self._scan_patch_cache: 'patch_cache',
            self._scan_event_logs: 'event_logs',
            self._scan_wxwork_cache: 'wxwork_cache',
            self._scan_electron_cache: 'electron_cache',
            self._scan_service_worker_cache: 'service_worker_cache',
            self._scan_dotnet_cache: 'dotnet_cache',
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
        """扫描回收站 - 使用 Shell API 获取回收站信息。"""
        try:
            import ctypes
            from ctypes import wintypes

            class SHQUERYRBINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', wintypes.DWORD),
                    ('i64Size', ctypes.c_longlong),
                    ('i64NumItems', ctypes.c_longlong),
                ]

            info = SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
            # None = 查询所有驱动器的回收站
            result = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
            if result == 0 and info.i64Size > 0:
                results['recycle'].append({
                    'path': 'C:\\$Recycle.Bin',
                    'size': info.i64Size,
                    'type': 'recycle'
                })
        except Exception as e:
            logger.warning(f"无法查询回收站: {e}")
            # 回退到文件遍历方式
            self._scan_recycle_bin_fallback(results)

    def _scan_recycle_bin_fallback(self, results):
        """回退：通过遍历文件系统扫描回收站。"""
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
        """扫描浏览器缓存（Chrome/Edge 所有 Profile + Firefox 所有 Profile）"""
        local = os.environ.get('LOCALAPPDATA', '')
        appdata = os.environ.get('APPDATA', '')

        cache_dirs = []

        # Chrome — 扫描所有 Profile（Default, Profile 1, Profile 2, ...）
        chrome_ud = os.path.join(local, 'Google', 'Chrome', 'User Data')
        if os.path.isdir(chrome_ud):
            try:
                for entry in os.scandir(chrome_ud):
                    if not entry.is_dir():
                        continue
                    if entry.name == 'Default' or entry.name.startswith('Profile'):
                        for sub in ('Cache', 'Code Cache', 'GPUCache'):
                            p = os.path.join(entry.path, sub)
                            if os.path.isdir(p):
                                cache_dirs.append(p)
            except (PermissionError, OSError):
                pass

        # Edge — 扫描所有 Profile
        edge_ud = os.path.join(local, 'Microsoft', 'Edge', 'User Data')
        if os.path.isdir(edge_ud):
            try:
                for entry in os.scandir(edge_ud):
                    if not entry.is_dir():
                        continue
                    if entry.name == 'Default' or entry.name.startswith('Profile'):
                        for sub in ('Cache', 'Code Cache', 'GPUCache'):
                            p = os.path.join(entry.path, sub)
                            if os.path.isdir(p):
                                cache_dirs.append(p)
            except (PermissionError, OSError):
                pass

        # Firefox — 所有 profile 的 cache2
        firefox_profiles = os.path.join(appdata, 'Mozilla', 'Firefox', 'Profiles')
        if os.path.isdir(firefox_profiles):
            try:
                for profile in os.listdir(firefox_profiles):
                    profile_cache = os.path.join(firefox_profiles, profile, 'cache2')
                    if os.path.isdir(profile_cache):
                        cache_dirs.append(profile_cache)
            except (PermissionError, FileNotFoundError):
                pass

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

    # ==================== IDE / 开发工具缓存 ====================

    def _scan_ide_cache(self, results):
        """扫描 IDE 和开发工具缓存（JetBrains、VS Code、Eclipse、Visual Studio）"""
        local = os.environ.get('LOCALAPPDATA', '')
        appdata = os.environ.get('APPDATA', '')
        home = os.path.expanduser('~')

        dirs = []
        # JetBrains 全家桶 (IntelliJ IDEA, PyCharm, WebStorm, GoLand, CLion, etc.)
        jetbrains_base = os.path.join(local, 'JetBrains')
        if os.path.isdir(jetbrains_base):
            for product_dir in os.listdir(jetbrains_base):
                product_path = os.path.join(jetbrains_base, product_dir)
                if os.path.isdir(product_path):
                    for sub in ('caches', 'log', 'index', 'tmp'):
                        candidate = os.path.join(product_path, sub)
                        if os.path.isdir(candidate):
                            dirs.append(candidate)

        # VS Code
        for sub in ('Cache', 'CachedData', 'CachedExtensions', 'CachedExtensionVSIXs', 'logs'):
            dirs.append(os.path.join(appdata, 'Code', sub))
        # VS Code - Insiders
        for sub in ('Cache', 'CachedData', 'logs'):
            dirs.append(os.path.join(appdata, 'Code - Insiders', sub))

        # Eclipse
        dirs.append(os.path.join(home, '.eclipse'))

        # Visual Studio
        vs_base = os.path.join(local, 'Microsoft', 'VisualStudio')
        if os.path.isdir(vs_base):
            for ver_dir in os.listdir(vs_base):
                candidate = os.path.join(vs_base, ver_dir, 'ComponentModelCache')
                if os.path.isdir(candidate):
                    dirs.append(candidate)

        # Android Studio
        android_studio_base = os.path.join(local, 'Google')
        if os.path.isdir(android_studio_base):
            for d in os.listdir(android_studio_base):
                if d.startswith('AndroidStudio'):
                    for sub in ('caches', 'log'):
                        candidate = os.path.join(android_studio_base, d, sub)
                        if os.path.isdir(candidate):
                            dirs.append(candidate)

        self._scan_directories(results, 'ide_cache', dirs)

    def _scan_dev_package_cache(self, results):
        """扫描开发包管理器缓存（Gradle、npm、yarn、pnpm、pip、conda）"""
        local = os.environ.get('LOCALAPPDATA', '')
        appdata = os.environ.get('APPDATA', '')
        home = os.path.expanduser('~')

        dirs = [
            # Gradle
            os.path.join(home, '.gradle', 'caches'),
            # npm
            os.path.join(local, 'npm-cache'),
            os.path.join(appdata, 'npm-cache'),
            # yarn
            os.path.join(local, 'Yarn', 'Cache'),
            # pnpm
            os.path.join(local, 'pnpm-cache'),
            os.path.join(local, 'pnpm', 'store'),
            # pip
            os.path.join(local, 'pip', 'cache'),
            # conda
            os.path.join(home, '.conda', 'pkgs'),
            # Go modules
            os.path.join(home, 'go', 'pkg', 'mod', 'cache'),
            # Cargo (Rust)
            os.path.join(home, '.cargo', 'registry', 'cache'),
            # NuGet
            os.path.join(local, 'NuGet', 'v3-cache'),
            # Composer (PHP)
            os.path.join(local, 'Composer', 'cache'),
        ]
        self._scan_directories(results, 'dev_pkg_cache', dirs)

    # ==================== AI / 大模型应用 ====================

    def _scan_ai_app_cache(self, results):
        """扫描 AI/大模型应用缓存（ChatGPT、Claude、Cursor、GitHub Desktop）"""
        appdata = os.environ.get('APPDATA', '')
        local = os.environ.get('LOCALAPPDATA', '')

        dirs = [
            # ChatGPT Desktop
            os.path.join(appdata, 'ChatGPT', 'Cache'),
            os.path.join(appdata, 'ChatGPT', 'Code Cache'),
            os.path.join(appdata, 'ChatGPT', 'GPUCache'),
            # Claude Desktop
            os.path.join(appdata, 'Claude', 'Cache'),
            os.path.join(appdata, 'Claude', 'Code Cache'),
            os.path.join(appdata, 'Claude', 'GPUCache'),
            # Cursor (VS Code fork)
            os.path.join(appdata, 'Cursor', 'Cache'),
            os.path.join(appdata, 'Cursor', 'CachedData'),
            os.path.join(appdata, 'Cursor', 'CachedExtensionVSIXs'),
            os.path.join(appdata, 'Cursor', 'logs'),
            # GitHub Desktop
            os.path.join(appdata, 'GitHub Desktop', 'Cache'),
            os.path.join(local, 'GitHubDesktop', 'Cache'),
        ]
        self._scan_directories(results, 'ai_cache', dirs)

    def _scan_ai_models(self, results):
        """扫描 AI 模型文件（Ollama、HuggingFace）— 仅分析展示"""
        home = os.path.expanduser('~')
        dirs = [
            os.path.join(home, '.ollama', 'models'),
            os.path.join(home, '.cache', 'huggingface'),
        ]
        self._scan_directories(results, 'ai_models', dirs)

    # ==================== 通讯 / 社交 ====================

    def _scan_messaging_cache(self, results):
        """扫描通讯社交应用缓存（微信、QQ、钉钉、飞书、Telegram、Zoom）"""
        appdata = os.environ.get('APPDATA', '')
        local = os.environ.get('LOCALAPPDATA', '')
        home = os.path.expanduser('~')

        dirs = [
            # Telegram
            os.path.join(appdata, 'Telegram Desktop', 'tdata', 'user_data', 'cache'),
            # Zoom
            os.path.join(appdata, 'Zoom', 'data'),
            os.path.join(appdata, 'Zoom', 'data', 'Cache'),
        ]

        # 微信 - 深度扫描：Cache + 日志 + 图片缓存 + 视频缓存
        wechat_base = os.path.join(home, 'Documents', 'WeChat Files')
        if os.path.isdir(wechat_base):
            for user_dir in os.listdir(wechat_base):
                user_path = os.path.join(wechat_base, user_dir)
                if not os.path.isdir(user_path):
                    continue
                # FileStorage 各子目录
                for sub in ('Cache', 'Video', 'Image', 'File'):
                    p = os.path.join(user_path, 'FileStorage', sub)
                    if os.path.isdir(p):
                        dirs.append(p)
                # 日志
                log_path = os.path.join(user_path, 'Msg', 'FTSContact')
                if os.path.isdir(log_path):
                    dirs.append(log_path)
                # 微信 Applet/miniprogramappbrand (小程序缓存)
                applet_path = os.path.join(user_path, 'Applet')
                if os.path.isdir(applet_path):
                    dirs.append(applet_path)

        # 微信 LOCALAPPDATA 下的缓存
        wechat_local = os.path.join(local, 'WeChat', 'All Users')
        if os.path.isdir(wechat_local):
            dirs.append(wechat_local)

        # QQ/TIM — 深度扫描
        qq_base = os.path.join(home, 'Documents', 'Tencent Files')
        if os.path.isdir(qq_base):
            for user_dir in os.listdir(qq_base):
                user_path = os.path.join(qq_base, user_dir)
                if not os.path.isdir(user_path):
                    continue
                # 文件接收缓存
                for sub in ('FileRecv', 'Image', 'Video'):
                    p = os.path.join(user_path, sub)
                    if os.path.isdir(p):
                        dirs.append(p)
                # 日志
                msg_path = os.path.join(user_path, 'Msg')
                if os.path.isdir(msg_path):
                    dirs.append(msg_path)

        # QQ NT (新版) LOCALAPPDATA 路径
        qq_nt = os.path.join(local, 'Tencent', 'QQ')
        if os.path.isdir(qq_nt):
            try:
                for entry in os.scandir(qq_nt):
                    if entry.is_dir():
                        for sub in ('Cache', 'Code Cache', 'GPUCache', 'logs'):
                            p = os.path.join(entry.path, sub)
                            if os.path.isdir(p):
                                dirs.append(p)
            except (PermissionError, OSError):
                pass

        # 钉钉 — Cache + 日志
        dingtalk_base = os.path.join(appdata, 'DingTalk')
        if os.path.isdir(dingtalk_base):
            for sub in os.listdir(dingtalk_base):
                sub_path = os.path.join(dingtalk_base, sub)
                if not os.path.isdir(sub_path):
                    continue
                for cache_name in ('Cache', 'GPUCache', 'logs', 'Log'):
                    cache_path = os.path.join(sub_path, cache_name)
                    if os.path.isdir(cache_path):
                        dirs.append(cache_path)

        # 飞书
        lark_base = os.path.join(appdata, 'Lark')
        if os.path.isdir(lark_base):
            for sub in os.listdir(lark_base):
                sub_path = os.path.join(lark_base, sub)
                if not os.path.isdir(sub_path):
                    continue
                for cache_name in ('Cache', 'GPUCache', 'Code Cache'):
                    cache_path = os.path.join(sub_path, cache_name)
                    if os.path.isdir(cache_path):
                        dirs.append(cache_path)

        # Foxmail 缓存
        foxmail_paths = [
            os.path.join(local, 'Foxmail', 'Cache'),
            os.path.join(appdata, 'Foxmail7', 'Cache'),
        ]
        # Foxmail Storages 下各帐号的 Cache 目录
        for fm_base in (os.path.join(local, 'Foxmail'),
                        os.path.join(appdata, 'Foxmail7')):
            if os.path.isdir(fm_base):
                try:
                    for entry in os.scandir(fm_base):
                        if entry.is_dir():
                            cache_p = os.path.join(entry.path, 'Cache')
                            if os.path.isdir(cache_p):
                                foxmail_paths.append(cache_p)
                except (PermissionError, OSError):
                    pass
        dirs.extend(foxmail_paths)

        self._scan_directories(results, 'messaging_cache', dirs)

    # ==================== 浏览器补充 + 游戏娱乐 ====================

    def _scan_browser_extra(self, results):
        """扫描补充浏览器缓存（Firefox、Opera、Brave、Arc）"""
        local = os.environ.get('LOCALAPPDATA', '')
        appdata = os.environ.get('APPDATA', '')

        dirs = [
            # Opera
            os.path.join(appdata, 'Opera Software', 'Opera Stable', 'Cache'),
            os.path.join(appdata, 'Opera Software', 'Opera GX Stable', 'Cache'),
            # Brave
            os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data', 'Default', 'Cache'),
            os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data', 'Default', 'Code Cache'),
        ]

        # Firefox - 多 profile
        firefox_profiles = os.path.join(local, 'Mozilla', 'Firefox', 'Profiles')
        if os.path.isdir(firefox_profiles):
            for profile in os.listdir(firefox_profiles):
                cache_path = os.path.join(firefox_profiles, profile, 'cache2')
                if os.path.isdir(cache_path):
                    dirs.append(cache_path)

        self._scan_directories(results, 'browser_extra', dirs)

    def _scan_gaming_cache(self, results):
        """扫描游戏/娱乐应用缓存（Steam、Epic、网易云音乐、QQ音乐、哔哩哔哩）"""
        local = os.environ.get('LOCALAPPDATA', '')
        appdata = os.environ.get('APPDATA', '')

        dirs = [
            # Steam
            os.path.join(local, 'Steam', 'htmlcache'),
            # Epic Games
            os.path.join(local, 'EpicGamesLauncher', 'Saved', 'webcache'),
            os.path.join(local, 'EpicGamesLauncher', 'Saved', 'Logs'),
            # 网易云音乐
            os.path.join(local, 'Netease', 'CloudMusic', 'Cache'),
            # QQ音乐
            os.path.join(local, 'Tencent', 'QQMusic', 'Cache'),
            os.path.join(appdata, 'Tencent', 'QQMusic', 'Cache'),
            # 哔哩哔哩
            os.path.join(appdata, 'bilibili', 'Cache'),
            os.path.join(local, 'bilibili', 'Cache'),
        ]
        self._scan_directories(results, 'gaming_cache', dirs)

    # ==================== 工具 / 办公 + 运行时 ====================

    def _scan_tool_cache(self, results):
        """扫描工具/办公应用缓存（WPS、Notion、Obsidian、Figma、Postman）"""
        local = os.environ.get('LOCALAPPDATA', '')
        appdata = os.environ.get('APPDATA', '')

        dirs = [
            # WPS
            os.path.join(local, 'Kingsoft', 'WPS Cloud Files', 'cache'),
            # Notion
            os.path.join(appdata, 'Notion', 'Cache'),
            os.path.join(appdata, 'Notion', 'Code Cache'),
            # Obsidian
            os.path.join(appdata, 'obsidian', 'Cache'),
            os.path.join(appdata, 'obsidian', 'Code Cache'),
            # Figma
            os.path.join(appdata, 'Figma', 'Cache'),
            os.path.join(appdata, 'Figma', 'Code Cache'),
            # Postman
            os.path.join(appdata, 'Postman', 'Cache'),
            os.path.join(appdata, 'Postman', 'Code Cache'),
        ]
        self._scan_directories(results, 'tool_cache', dirs)

    def _scan_docker_data(self, results):
        """扫描 Docker Desktop 数据 — 仅分析展示"""
        local = os.environ.get('LOCALAPPDATA', '')
        dirs = [
            os.path.join(local, 'Docker', 'wsl', 'data'),
        ]
        self._scan_directories(results, 'docker_data', dirs)

    def _scan_dotnet_cache(self, results):
        """扫描 .NET Framework/Runtime 临时编译缓存和 NuGet 包缓存"""
        local = os.environ.get('LOCALAPPDATA', '')
        home = os.path.expanduser('~')
        dirs = []

        # .NET Framework — Temporary ASP.NET Files (32-bit + 64-bit)
        for fw_dir in ('Microsoft.NET\\Framework', 'Microsoft.NET\\Framework64'):
            fw_path = os.path.join('C:', os.sep, 'Windows', fw_dir)
            if os.path.isdir(fw_path):
                try:
                    for entry in os.scandir(fw_path):
                        if entry.is_dir() and entry.name.startswith('v'):
                            asp_temp = os.path.join(entry.path, 'Temporary ASP.NET Files')
                            if os.path.isdir(asp_temp):
                                dirs.append(asp_temp)
                except (PermissionError, OSError):
                    pass

        # NGen 本机映像缓存（assembly\NativeImages_*）
        for fw_dir in ('Microsoft.NET\\Framework', 'Microsoft.NET\\Framework64'):
            assembly_path = os.path.join('C:', os.sep, 'Windows', 'assembly')
            if os.path.isdir(assembly_path):
                try:
                    for entry in os.scandir(assembly_path):
                        if entry.is_dir() and entry.name.startswith('NativeImages'):
                            dirs.append(entry.path)
                except (PermissionError, OSError):
                    pass
                break  # assembly 目录只需扫一次

        # NuGet 全局包缓存
        nuget_cache = os.path.join(home, '.nuget', 'packages')
        if os.path.isdir(nuget_cache):
            dirs.append(nuget_cache)

        # NuGet HTTP 缓存
        nuget_http = os.path.join(local, 'NuGet', 'v3-cache')
        if os.path.isdir(nuget_http):
            dirs.append(nuget_http)

        # dotnet SDK workload/temp
        dotnet_temp = os.path.join(local, 'Temp', '.dotnet')
        if os.path.isdir(dotnet_temp):
            dirs.append(dotnet_temp)

        self._scan_directories(results, 'dotnet_cache', dirs)

    def _scan_gpu_shader_cache(self, results):
        """扫描 GPU 着色器缓存（NVIDIA/AMD/DirectX）"""
        local = os.environ.get('LOCALAPPDATA', '')
        dirs = [
            os.path.join(local, 'NVIDIA', 'DXCache'),
            os.path.join(local, 'NVIDIA', 'GLCache'),
            os.path.join(local, 'NVIDIA Corporation', 'NV_Cache'),
            os.path.join(local, 'AMD', 'DxCache'),
            os.path.join(local, 'AMD', 'GLCache'),
            os.path.join(local, 'D3DSCache'),
            os.path.join(local, 'Intel', 'ShaderCache'),
        ]
        self._scan_directories(results, 'gpu_shader_cache', dirs)

    def _scan_patch_cache(self, results):
        """扫描 Windows Installer 补丁缓存（$PatchCache$）"""
        dirs = [
            os.path.join('C:', os.sep, 'Windows', 'Installer', '$PatchCache$'),
        ]
        self._scan_directories(results, 'patch_cache', dirs)

    def _scan_event_logs(self, results):
        """扫描 Windows 事件日志（.evtx 文件，通常可安全清除旧日志）"""
        log_dir = os.path.join('C:', os.sep, 'Windows', 'System32', 'winevt', 'Logs')
        if not os.path.isdir(log_dir):
            return
        threshold = datetime.datetime.now() - datetime.timedelta(days=30)
        try:
            for entry in os.scandir(log_dir):
                if not entry.name.lower().endswith('.evtx'):
                    continue
                try:
                    stat = entry.stat()
                    mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
                    if mtime < threshold and stat.st_size > 0:
                        results['event_logs'].append({
                            'path': entry.path,
                            'size': stat.st_size,
                            'type': 'event_logs',
                        })
                except (PermissionError, OSError):
                    pass
        except (PermissionError, OSError):
            pass

    def _scan_wxwork_cache(self, results):
        """扫描企业微信缓存"""
        appdata = os.environ.get('APPDATA', '')
        local = os.environ.get('LOCALAPPDATA', '')
        dirs = [
            os.path.join(appdata, 'Tencent', 'WXWork', 'Cache'),
            os.path.join(appdata, 'Tencent', 'WXWork', 'GPUCache'),
        ]
        # 企业微信在 LOCALAPPDATA 下可能有用户子目录
        wxwork_local = os.path.join(local, 'Tencent', 'WXWork')
        if os.path.isdir(wxwork_local):
            try:
                for entry in os.scandir(wxwork_local):
                    if entry.is_dir():
                        cache_sub = os.path.join(entry.path, 'Cache')
                        if os.path.isdir(cache_sub):
                            dirs.append(cache_sub)
                        gpu_sub = os.path.join(entry.path, 'GPUCache')
                        if os.path.isdir(gpu_sub):
                            dirs.append(gpu_sub)
            except (PermissionError, OSError):
                pass
        self._scan_directories(results, 'wxwork_cache', dirs)

    def _scan_electron_cache(self, results):
        """扫描各 Electron 应用的通用缓存目录"""
        appdata = os.environ.get('APPDATA', '')
        local = os.environ.get('LOCALAPPDATA', '')
        dirs = []
        # 常见 Electron 应用（排除已在其它分类中覆盖的）
        known_covered = {'notion', 'obsidian', 'figma', 'postman',
                         'wechat', 'qq', 'discord', 'slack', 'telegram desktop',
                         'code', 'cursor'}
        for base in (appdata, local):
            if not base or not os.path.isdir(base):
                continue
            try:
                for entry in os.scandir(base):
                    if not entry.is_dir():
                        continue
                    if entry.name.lower() in known_covered:
                        continue
                    cache_path = os.path.join(entry.path, 'Cache')
                    code_cache = os.path.join(entry.path, 'Code Cache')
                    gpu_cache = os.path.join(entry.path, 'GPUCache')
                    # 只有同时存在 Cache 和某种 Electron 特征文件才纳入
                    if os.path.isdir(cache_path) and (
                            os.path.isdir(code_cache) or os.path.isdir(gpu_cache)):
                        dirs.append(cache_path)
                        if os.path.isdir(code_cache):
                            dirs.append(code_cache)
                        if os.path.isdir(gpu_cache):
                            dirs.append(gpu_cache)
            except (PermissionError, OSError):
                pass
        self._scan_directories(results, 'electron_cache', dirs)

    def _scan_service_worker_cache(self, results):
        """扫描浏览器 Service Worker / CacheStorage 缓存"""
        local = os.environ.get('LOCALAPPDATA', '')
        dirs = []
        browser_paths = [
            os.path.join(local, 'Google', 'Chrome', 'User Data'),
            os.path.join(local, 'Microsoft', 'Edge', 'User Data'),
            os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data'),
        ]
        for browser in browser_paths:
            if not os.path.isdir(browser):
                continue
            try:
                for entry in os.scandir(browser):
                    if not entry.is_dir():
                        continue
                    # Default, Profile 1, Profile 2, etc.
                    if entry.name == 'Default' or entry.name.startswith('Profile'):
                        sw = os.path.join(entry.path, 'Service Worker', 'CacheStorage')
                        if os.path.isdir(sw):
                            dirs.append(sw)
            except (PermissionError, OSError):
                pass
        self._scan_directories(results, 'service_worker_cache', dirs)
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

            # 已备份或其它磁盘清理：直接永久删除，立即释放空间。
            # 旧逻辑移到回收站会导致磁盘空间不变。
            os.remove(file_path)
            logger.info(f"已永久删除文件: {file_path}")

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
