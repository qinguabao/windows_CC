#!/usr/bin/env python
# -*- coding: utf-8 -*-

from setuptools import setup, find_packages

setup(
    name="c-drive-cleaner",
    version="1.0.0",
    description="一个安全高效的C盘文件清理软件",
    author="Your Name",
    author_email="your.email@example.com",
    url="https://github.com/yourusername/c-drive-cleaner",
    packages=find_packages(),
    install_requires=[
        "PySide6>=6.5,<7",
    ],
    entry_points={
        "console_scripts": [
            "c-drive-cleaner=main:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Win32 (MS Windows)",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: System :: Systems Administration",
        "Topic :: Utilities",
    ],
    python_requires=">=3.9",
)
