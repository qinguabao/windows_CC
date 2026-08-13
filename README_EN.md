<div align="center">

# C Drive Cleaner

<img src="https://img.shields.io/badge/platform-Windows-blue" alt="Platform">
<img src="https://img.shields.io/badge/language-Python-yellow" alt="Language">
<img src="https://img.shields.io/badge/license-MIT-green" alt="License">

**A safe, efficient PySide6-based Windows C drive cleaning tool**

[English](README_EN.md) | [简体中文](README.md)

</div>

## 🔥 Introduction

C Drive Cleaner is an open-source cleaning software designed specifically for Windows systems. It requires no installation and can quickly and safely clean various temporary files, caches, and junk files on your C drive, helping users free up valuable disk space.

Unlike other cleaning tools, this tool places special emphasis on safety, employing multiple safety check mechanisms to ensure that files necessary for the normal operation of the system and applications are not deleted. At the same time, it also provides comprehensive backup and recovery functions, allowing users to clean with peace of mind.

## ✨ Key Features

### 👍 User-Friendly Interface

- **One-Click Cleaning**: Directly clean all cleanable items without selection
- **Select All Function**: Select all cleanable items with one click
- **Deselect All**: Quickly deselect all selected items
- **Single PySide6 Interface**: The Tkinter and PyQt5 interfaces have been removed
- **Backup Management Interface**: Manage and restore manifest-backed backups
- **Intuitive Cleaning Results**: Clearly displays freed space and number of files cleaned

### 💥 Powerful Cleaning Capabilities

#### Basic Cleaning
- Scan and clean temporary files
- Empty the recycle bin
- Clean browser cache
- Clean system logs
- Clean thumbnail cache

#### Extended Cleaning
- Clean prefetch files
- Clean error reports
- Clean memory dump files
- Clean font cache

#### Advanced Safe Cleaning
- Clean application cache (Adobe, Office, etc.)
- Clean media player cache (Windows Media Player, VLC, Spotify, etc.)
- Clean search index temporary files
- Clean application crash dumps
- Clean application logs
- Clean recent files list
- Clean Windows notification cache
- Clean DNS cache
- Clean printer temporary files
- Clean device temporary files
- Clean Windows Store cache
- Clean OneDrive cache

#### User-Requested Cleaning
- Clean Downloads folder (immediate) - Clean all files in the downloads folder
- Clean Windows Delivery Optimization cache (immediate) - Clean all Windows delivery optimization cache files

### 🔒 Safety Features

- **Simulation Mode**: Preview files to be deleted without actually deleting them
- **File Backup**: Preserve each original absolute path in an atomic restore manifest
- **Fail-Safe Deletion**: A file is not deleted if its backup or manifest write fails
- **Safe Path Check**: Prevent deletion of important system files
- **User Confirmation**: User confirmation required before important operations
- **Protected Maintenance Areas**: Windows Update, WinSxS, Defender, and Installer cleanup are not recursively deleted; use official Windows maintenance tools

### 🗃️ Backup Management

- **Custom Backup Location**: Choose to store backup files in a non-C drive location
- **Backup Management Interface**: View, restore, or delete backups
- **Auto-Clean Old Backups**: Automatically delete the oldest backups to keep backup space within reasonable limits
- **Backup Limit Settings**: Set maximum number of backups and total size

### 🔍 Large File Scanning

- Large files are display-only and are excluded from one-click cleaning and the core deletion API

- **Quick Locate Large Files**: Scan and display large files (over 100MB) on C drive
- **Detailed Information Display**: Show file size, modification time, and file type

### Other Drive Cleanup

- Select a non-system fixed or removable drive and configure large-file thresholds
- Confirm duplicates with size grouping, sampled content, and full SHA-256 verification
- Keep every result unchecked until the user explicitly selects files
- Require at least one file to remain in each duplicate group
- Permanently delete selected files to free the target drive, with optional backup to another drive

## 💻 System Requirements

- Windows 7/8/10/11
- Python 3.9+
- PySide6 6.5+

## 🚀 Quick Start

### Method 1: Download Executable

1. Go to the [Releases](https://github.com/JIEKE66633/One-click-cleaning-of-C-drive/releases) page
2. Download the latest version
3. Extract to any location
4. Double-click "Start C Drive Cleaner.bat" to run the program

### Method 2: Run from Source Code

1. Clone the repository:
```bash
git clone https://github.com/JIEKE66633/One-click-cleaning-of-C-drive.git
```

2. Navigate to the project directory:
```bash
cd One-click-cleaning-of-C-drive
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Run the unified PySide6 application:
```bash
python main.py
```

## 📚 Usage Guide

### Basic Usage

1. After starting the program, click the "Scan System" button
2. The program will scan for cleanable files in the system and display them in the list
3. Select the items you want to clean, or click the "Select All" button to select all items
4. Click the "Clean Selected" button to start cleaning

### One-Click Cleaning

If you want to quickly clean your system, you can directly click the "One-Click Clean" button, and the program will automatically scan and clean all cleanable items.

### Other Drive Cleanup

Choose "Other Drive Cleanup", select a target drive and scan thresholds, then review and select individual large or duplicate files. Actual cleanup permanently deletes selected files; when backup is enabled, its directory must be on a different drive.

### Backup Management

1. Click the "Backup Management" button to open the backup management interface
2. In the backup management interface, you can view, restore, or delete backups with valid manifests
3. You can also set the backup directory, maximum number of backups, and total size

## 💬 FAQ

### Do backup files affect system operation?

Backup files are just copies of deleted files, stored in a separate location, and do not affect system operation. If the system is running normally, you can safely delete the backup files.

### Will cleaning affect the system or applications?

This tool is carefully designed to only clean safely deletable files, such as temporary files, caches, and junk files. Under normal circumstances, it will not affect the normal operation of the system or applications.

### Why is simulation mode needed?

Simulation mode allows you to preview files to be deleted without actually deleting them, which is very useful for first-time users and helps you understand how the tool works.

## 📝 Contribution Guidelines

We welcome and appreciate all forms of contribution! Here are some ways to participate in this project:

1. **Submit Issues and Suggestions**: If you find a problem or have improvement suggestions, please submit an Issue on GitHub
2. **Submit Code**: If you want to add new features or fix issues, please Fork the repository and submit a Pull Request
3. **Improve Documentation**: Help us improve the documentation to make it clearer and more complete

Please ensure your code complies with the project's code style and quality standards.

## 💯 Project Plans

We plan to add the following features in future versions:

- [ ] Scheduled cleaning function
- [ ] More detailed cleaning reports
- [ ] More custom cleaning options
- [ ] Multi-language support

## 👬 Contributors

Thanks to all who have contributed to this project!

## 🔒 License

This project is licensed under the [MIT License](LICENSE).

## ❗ Notes

- It is recommended to enable simulation mode for first-time use
- Make sure you understand the purpose of system files before cleaning them
- Regularly backup important data
- If the system runs normally after cleaning, you can safely delete backup files
