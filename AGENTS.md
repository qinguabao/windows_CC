# Repository Guidelines

## Project Structure

This is a Windows Python desktop cleaner using one PySide6 interface. `cleaner_logic.py` contains scanning, safety checks, manifest-backed backup, restore, and deletion behavior. `app_modern.py` is the primary interface; `main.py` is a compatibility launcher for the same PySide6 application. `backup_manager.py` contains the PySide6 backup management dialog, and `storage_cleaner.py` contains the other-drive large-file and duplicate-file workflow. Supporting modules include `elevate.py` and `config.py`. Icons live in `icons/`; PyInstaller specifications and build scripts are at the repository root. Generated `build/` and `dist/` artifacts should not be edited by hand.

## Build, Test, and Development Commands

- `python main.py` or `python app_modern.py` runs the PySide6 application locally.
- `python verify.py` runs the committed backup and safety regression tests.
- `python -m py_compile cleaner_logic.py app_modern.py backup_manager.py storage_cleaner.py main.py` performs a fast syntax check.
- `python build_pro.py` packages `app_modern.py` as the administrator-enabled Pro executable; it requires PyInstaller and PySide6.
- `python build_exe.py` packages the same PySide6 application under the standard executable name.
- `python verify.py` runs safety and cleaner smoke checks, but update its machine-specific `BASE` path before use.

## Coding Style & Naming

Use Python 3-compatible, UTF-8 source with four-space indentation. Keep functions and classes in `snake_case` and `PascalCase`, respectively; use descriptive Chinese UI text consistently with existing screens. Prefer standard-library modules and the existing `CleanerLogic` APIs. Add short comments only for non-obvious Windows API or safety behavior. Run `py_compile` before packaging.

## Testing Guidelines

Tests use the standard-library `unittest` framework under `tests/`. Exercise filesystem changes through temporary directories and assert file existence, backup manifests, restored paths, freed bytes, and reported errors. Test locked files, missing files, and insufficient permissions on Windows where possible.

## Commits & Pull Requests

Git metadata is not present in this checkout, so no repository-specific history convention is available. Use concise imperative commit subjects (for example, `Fix download cleanup selection`) and keep each commit focused. Pull requests should explain user-visible behavior, safety implications, and validation commands; include screenshots for UI changes and link the relevant issue when one exists.

## Safety & Configuration

Never broaden deletion paths without updating `_is_safe_path` and adding a regression check. Preserve simulation mode and backup defaults while changing cleanup behavior. Treat download folders and other user-owned paths as high risk: require an explicit confirmation warning before deletion.
