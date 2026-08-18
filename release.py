#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""编译并部署更新到自建服务器（scp 上传 EXE + latest.json 清单）。

用法:
    python release.py                 # 使用 version.py 中的当前版本号
    python release.py 1.5.1           # 指定新版本号（会自动更新 version.py）
    python release.py 1.5.2 --no-github    # 跳过 GitHub Release

更新源是自建服务器（deploy.json 配置，国内可直连）。GitHub Release 仅作为
存量旧版本客户端（≤v1.5.0，仍查 GitHub API）的过渡入口，全部用户迁移到
v1.5.1+ 后可用 --no-github 停发。

前置条件:
    - 复制 deploy.example.json 为 deploy.json 并填好服务器信息
    - SSH 密钥免密登录（Windows 自带 OpenSSH 的 ssh/scp）
    - 已安装 PyInstaller
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

VERSION_FILE = os.path.join(HERE, 'version.py')
APP_NAME = "C盘清理工具Pro"
EXE_PATH = os.path.join(HERE, 'dist', f'{APP_NAME}.exe')
DEPLOY_FILE = os.path.join(HERE, 'deploy.json')
GITHUB_REPO = 'qinguabao/windows_CC'
KEEP_REMOTE_VERSIONS = 5  # 服务器保留最近 N 个版本包，更早的自动清理


def read_version():
    with open(VERSION_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        print('错误：无法从 version.py 读取版本号')
        sys.exit(1)
    return match.group(1)


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


def load_deploy_config():
    """读取 deploy.json；缺失或字段不全直接退出。"""
    if not os.path.isfile(DEPLOY_FILE):
        print('错误：缺少 deploy.json。请复制 deploy.example.json 为 deploy.json 并填写服务器信息。')
        sys.exit(1)
    try:
        with open(DEPLOY_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f'错误：deploy.json 解析失败: {e}')
        sys.exit(1)
    for key in ('host', 'user', 'remote_dir', 'public_base_url'):
        if not str(cfg.get(key) or '').strip():
            print(f'错误：deploy.json 缺少字段 {key}')
            sys.exit(1)
    cfg['port'] = int(cfg.get('port') or 22)
    return cfg


def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'release-script'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


def remote_latest_version(cfg):
    """当前最新发布版本：服务器 latest.json 是事实源；服务器还没有清单时
    （首次部署）回退看 GitHub Release，两边都拿不到则放行。"""
    base_url = cfg['public_base_url'].rstrip('/')
    try:
        data = fetch_json(f'{base_url}/latest.json?t={int(time.time())}')
        return str(data.get('version') or '').strip()
    except Exception as e:
        print(f'提示：无法读取服务器清单（{e}），尝试 GitHub')
    try:
        data = fetch_json(f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest')
        return (data.get('tag_name') or '').lstrip('vV').strip()
    except Exception as e:
        print(f'警告：无法查询远端最新版本（{e}），跳过版本倒退检查')
        return ''


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


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
    # 提交全部源码修改，保证 tag 对应的源码与发布的 EXE 一致
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


def _ssh_base(cfg):
    # BatchMode: 只用密钥认证，杜绝交互式密码提示卡住发布
    # accept-new: 首次连接自动信任主机指纹（否则会交互确认）
    return ['ssh', '-p', str(cfg['port']),
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
            f"{cfg['user']}@{cfg['host']}"]


def _scp_base(cfg):
    # 注意 scp 用大写 -P 指定端口
    return ['scp', '-P', str(cfg['port']),
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new']


def deploy_to_server(cfg, version):
    """上传新版 EXE 与 latest.json（先传包、最后翻转清单）。

    文件名带版本号：旧清单指向的旧包不会被新发布覆盖，避免"旧清单 +
    新文件"的组合让旧客户端 SHA-256 校验失败。
    """
    base_url = cfg['public_base_url'].rstrip('/')
    remote_dir = cfg['remote_dir'].rstrip('/')
    target = f"{cfg['user']}@{cfg['host']}"

    # GitHub 会把中文资产名清洗成 "C.Pro.exe"，带版本号的 ASCII 名也方便
    # 用户分辨下载的文件；#label 语法实测未生效，因此直接用 ASCII 文件名
    asset_name = f'CCleaner-Pro-v{version}.exe'
    asset_path = os.path.join(HERE, 'dist', asset_name)
    shutil.copyfile(EXE_PATH, asset_path)
    digest = sha256_file(asset_path)

    manifest = {
        'version': version,
        'url': f'{base_url}/{asset_name}',
        'sha256': digest,
        'notes': f'C盘清理工具 Pro v{version}',
        'date': datetime.date.today().isoformat(),
    }
    manifest_path = os.path.join(HERE, 'dist', 'latest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f'\n=== 部署到更新服务器: {base_url} ===')

    def run(cmd, desc):
        print(f'{desc}…')
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f'错误：{desc}失败（exit {rc}）。请检查 SSH 密钥免密登录与 deploy.json 配置。')
            sys.exit(1)

    run(_ssh_base(cfg) + [f'mkdir -p {remote_dir}'], '确认服务器目录')
    run(_scp_base(cfg) + [asset_path, f'{target}:{remote_dir}/'], f'上传 {asset_name}')
    run(_scp_base(cfg) + [manifest_path, f'{target}:{remote_dir}/'], '上传 latest.json（翻转清单）')
    run(_ssh_base(cfg) + [
        f"cd {remote_dir} && ls CCleaner-Pro-v*.exe 2>/dev/null | sort -V "
        f"| head -n -{KEEP_REMOTE_VERSIONS} | xargs -r rm -f --"],
        f'清理旧版本包（保留最近 {KEEP_REMOTE_VERSIONS} 个）')

    # 回读校验：服务器上的清单必须已生效且指向本次的包
    try:
        data = fetch_json(f'{base_url}/latest.json?t={int(time.time())}')
        if str(data.get('version')) != version or data.get('sha256') != digest:
            print(f'错误：服务器清单回读不一致（version={data.get("version")}），请人工检查！')
            sys.exit(1)
        print('服务器清单回读校验通过')
    except Exception as e:
        print(f'警告：无法回读服务器清单（{e}），请手动访问 {base_url}/latest.json 确认')

    print(f'部署成功：{manifest["url"]}')


def create_github_release(version):
    """尽力发布 GitHub Release（存量旧客户端的过渡入口）；失败不影响服务器更新。"""
    tag = f'v{version}'
    print(f'\n=== 发布 GitHub Release（过渡入口，--no-github 可跳过）: {tag} ===')
    asset_path = os.path.join(HERE, 'dist', f'CCleaner-Pro-v{version}.exe')
    # gh release create 不支持 --clobber（那是 upload 的参数）；
    # 重复发布时先删除同名 Release 再重建
    if subprocess.call(['gh', 'release', 'view', tag]) == 0:
        print(f'已存在 {tag} 的 Release，先删除再重建')
        subprocess.check_call(['gh', 'release', 'delete', tag, '--yes'])
    cmd = [
        'gh', 'release', 'create', tag,
        asset_path,
        '--title', f'C盘清理工具 Pro v{version}',
        '--notes', f'## C盘清理工具 Pro v{version}\n\n下载 `CCleaner-Pro-v{version}.exe` 即可使用。',
        '--latest',
    ]
    rc = subprocess.call(cmd)
    if rc != 0:
        print('警告：GitHub Release 发布失败（服务器更新不受影响），可稍后手动补发。')
        return
    print(f'GitHub Release：https://github.com/{GITHUB_REPO}/releases/tag/{tag}')


def main():
    argv = sys.argv[1:]
    no_github = '--no-github' in argv
    args = [a for a in argv if a != '--no-github']

    # 确定版本号（防止 --help 之类的参数被误当成版本号写进 version.py）
    if args:
        new_ver = args[0].lstrip('vV')
        if not re.fullmatch(r'\d+(\.\d+)*', new_ver):
            print(f'错误：无效版本号 "{args[0]}"。用法: python release.py [版本号] [--no-github]')
            sys.exit(1)
        update_version(new_ver)
        version = new_ver
    else:
        version = read_version()

    cfg = load_deploy_config()

    # 防止版本倒退：latest 版本被旧版本覆盖后，所有客户端的更新检查
    # 都会误判"已是最新"
    remote_ver = remote_latest_version(cfg)
    if remote_ver and ver_tuple(version) <= ver_tuple(remote_ver):
        print(f'错误：待发布版本 v{version} 不高于远端最新 v{remote_ver}。')
        print(f'如需发布请使用更大的版本号，例如: python release.py 1.{int(remote_ver.split(".")[1]) + 1}.0')
        sys.exit(1)

    print(f'当前版本: v{version}')

    # 编译
    build()

    # Git 提交 + tag + 推送
    git_commit_and_tag(version)

    # 部署到更新服务器（更新源没发成功比不发更糟，失败即退出）
    deploy_to_server(cfg, version)

    # GitHub Release：仅服务存量旧客户端
    if not no_github:
        create_github_release(version)

    print(f'\n发布完成：v{version}')
    print(f'更新清单：{cfg["public_base_url"].rstrip("/")}/latest.json')


if __name__ == '__main__':
    main()
