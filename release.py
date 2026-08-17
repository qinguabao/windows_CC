#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""编译并发布到 GitHub Releases。

用法:
    python release.py              # 使用 version.py 中的当前版本号
    python release.py 1.2.0        # 指定新版本号（会自动更新 version.py）

前置条件:
    - 已安装 gh CLI 并登录 (gh auth login)
    - 已安装 PyInstaller
"""
import os
import re
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

VERSION_FILE = os.path.join(HERE, 'version.py')
APP_NAME = "C盘清理工具Pro"
EXE_PATH = os.path.join(HERE, 'dist', f'{APP_NAME}.exe')
GITHUB_REPO = 'qinguabao/windows_CC'


def read_version():
    with open(VERSION_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        print('错误：无法从 version.py 读取版本号')
        sys.exit(1)
    return match.group(1)


def remote_latest_version():
    """查询 GitHub 上当前 latest release 的版本号；查询失败返回 ''。"""
    url = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
    req = urllib.request.Request(url, headers={'User-Agent': 'release-script'})
    try:
        import json
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return (data.get('tag_name') or '').lstrip('vV').strip()
    except Exception as e:
        print(f'警告：无法查询远端最新版本（{e}），跳过版本倒退检查')
        return ''


def ver_tuple(v):
    parts = []
    for p in v.split('.'):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def update_version(new_ver):
    with open(VERSION_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    content = re.sub(
        r'(APP_VERSION\s*=\s*["\'])([^"\']+)(["\'])',
        f'\\g<1>{new_ver}\\g<3>',
        content,
    )
    with open(VERSION_FILE, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'已更新 version.py: {new_ver}')


def build():
    print('\n=== 开始编译 ===')
    rc = subprocess.call([sys.executable, 'build_pro.py'])
    if rc != 0:
        print('编译失败！')
        sys.exit(rc)
    if not os.path.isfile(EXE_PATH):
        print(f'错误：找不到编译产物 {EXE_PATH}')
        sys.exit(1)
    print(f'编译成功：{EXE_PATH}')


def git_commit_and_tag(version):
    tag = f'v{version}'
    print(f'\n=== Git 提交并创建 tag: {tag} ===')
    # 提交全部源码修改，保证 Release tag 对应的源码与发布的 EXE 一致
    # （此前只提交 version.py，导致修复代码从未进入 Release）
    subprocess.check_call(['git', 'add', '-A'])
    rc = subprocess.call(['git', 'diff', '--cached', '--quiet'])
    if rc != 0:
        subprocess.check_call([
            'git', 'commit', '-m', f'release: v{version}',
        ])
    # 创建 tag
    subprocess.check_call(['git', 'tag', '-f', tag])
    # 推送
    subprocess.check_call(['git', 'push'])
    subprocess.check_call(['git', 'push', '--tags', '-f'])


def create_release(version):
    tag = f'v{version}'
    print(f'\n=== 发布 GitHub Release: {tag} ===')
    # 用 file#label 指定 ASCII 资产名：中文文件名上传后会被改写成 "C.Pro.exe"，
    # 带版本号的 ASCII 名也方便用户分辨下载的文件
    # GitHub 会把中文资产名清洗成 "C.Pro.exe"，带版本号的 ASCII 名方便用户
    # 分辨下载的文件；#label 语法实测未生效，改为复制一份 ASCII 文件名再上传
    asset_name = f'CCleaner-Pro-v{version}.exe'
    asset_path = os.path.join(HERE, 'dist', asset_name)
    shutil.copyfile(EXE_PATH, asset_path)
    # gh release create 不支持 --clobber（那是 upload 的参数）；
    # 重复发布时先删除同名 Release 再重建，达到同样的覆盖效果
    if subprocess.call(['gh', 'release', 'view', tag]) == 0:
        print(f'已存在 {tag} 的 Release，先删除再重建')
        subprocess.check_call(['gh', 'release', 'delete', tag, '--yes'])
    cmd = [
        'gh', 'release', 'create', tag,
        asset_path,
        '--title', f'C盘清理工具 Pro v{version}',
        '--notes', f'## C盘清理工具 Pro v{version}\n\n下载 `{asset_name}` 即可使用。',
        '--latest',
    ]
    rc = subprocess.call(cmd)
    if rc != 0:
        print('发布失败！请检查 gh CLI 是否已登录（gh auth login）。')
        sys.exit(rc)
    print(f'\n发布成功！https://github.com/{GITHUB_REPO}/releases/tag/{tag}')


def main():
    # 确定版本号
    if len(sys.argv) > 1:
        new_ver = sys.argv[1].lstrip('vV')
        update_version(new_ver)
        version = new_ver
    else:
        version = read_version()

    # 防止版本倒退：latest release 不能被更旧的版本覆盖，
    # 否则所有客户端的更新检查都会误判"已是最新"
    remote_ver = remote_latest_version()
    if remote_ver and ver_tuple(version) <= ver_tuple(remote_ver):
        print(f'错误：待发布版本 v{version} 不高于远端 latest v{remote_ver}。')
        print(f'如需发布请使用更大的版本号，例如: python release.py 1.{int(remote_ver.split(".")[1]) + 1}.0')
        sys.exit(1)

    print(f'当前版本: v{version}')

    # 编译
    build()

    # Git 提交 + tag + 推送
    git_commit_and_tag(version)

    # 发布到 GitHub Releases
    create_release(version)


if __name__ == '__main__':
    main()
