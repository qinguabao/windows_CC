# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Windows C盘清理工具 (C Drive Cleaning Tool) — a PySide6 desktop application that scans and cleans temporary/cache files from the Windows system drive, with additional support for analyzing large files and duplicates on other drives.

## Commands

### Run the application
```bash
python main.py
# or directly:
python app_modern.py
```
Requires admin elevation (UAC prompt) for full functionality. Runs in reduced mode if denied.

### Run tests
```bash
python -m pytest tests/
# Single test file:
python -m pytest tests/test_cleanup_safety.py
# Single test:
python -m pytest tests/test_cleanup_safety.py::CleanupSafetyTests::test_analysis_only_large_file_cannot_be_deleted_by_core_api
```

### Build EXE (PyInstaller)
```bash
python build_pro.py
# or the simpler variant:
python build_exe.py
```
Outputs a single-file EXE to `dist/` with embedded admin manifest and icons.

### Install dependencies
```bash
pip install PySide6
# For building:
pip install pyinstaller
```

## Architecture

```
main.py              → Entry point, delegates to app_modern.main()
app_modern.py        → PySide6 GUI (ModernCleanerWindow), scan/clean orchestration
cleaner_logic.py     → Core engine: scanning 30+ categories, backup/restore, file deletion
                       Also handles storage scan (large files + duplicate detection on non-system drives)
backup_manager.py    → Dialog for viewing/restoring/deleting backup sets
storage_cleaner.py   → Dialog for analyzing and cleaning other (non-C) drives
elevate.py           → UAC admin elevation helper (ShellExecuteW runas)
config.py            → Static config constants (paths, file types, defaults)
build_pro.py         → PyInstaller packaging script
```

### Key Design Decisions

- **Safety tiers**: Categories are split into cleanable, `ANALYSIS_ONLY_CATEGORIES` (display only, e.g. `large_files`), and `DISABLED_CLEANUP_CATEGORIES` (system maintenance items that need official Windows APIs). The `clean_selected()` method enforces these at the core level — even if UI bugs send them, they are rejected.
- **Backup-before-delete**: Each cleanup batch creates a timestamped backup set with an atomic `manifest.json`. Files are only deleted after their backup is confirmed on disk (`BackupError` aborts deletion). Backups default to the first non-C drive.
- **Concurrent scanning**: `scan_system()` uses a ThreadPoolExecutor (10 workers) with weighted progress (heavy tasks like `large_files` get 45% of progress bar weight).
- **Pause/resume**: Scanning supports pause — completed categories are tracked and skipped on resume.
- **High-risk categories**: `downloads` is marked dangerous and unchecked by default in the UI.
- **Storage scanner** (other drives): Detects duplicates via two-pass hashing (quick first/last 64KB sampling → full SHA-256 for candidates). Prevents deleting all copies in a duplicate group.

### Threading Model

- `ScanThread` (QThread) runs `CleanerLogic.scan_system()` off the main thread
- `CleanThread` (QThread) runs `CleanerLogic.clean_selected()`
- `StorageScanThread` / `StorageCleanThread` for the other-drives dialog
- `_abort_event` (threading.Event) allows cooperative cancellation across the thread pool

## Platform

Windows-only (uses ctypes windll, Windows paths, UAC elevation). Python 3.9+.
