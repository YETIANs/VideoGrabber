#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键发版：版本号→编译→提交→打 tag→推送→建 Release 并上传安装包。

用法：
    python release.py                 # 用 installer.iss 里现有的版本号
    python release.py 1.4.0           # 顺便把版本号改成 1.4.0
    python release.py 1.4.0 "标题"    # 再指定 Release 标题

做的事（有一步失败就停，不会半途留下错状态）：
    1. 改 installer.iss 的 MyAppVersion
    2. 编译 exe（PyInstaller）和安装包（Inno Setup）
    3. git commit + push
    4. git tag + push
    5. gh release create（带安装包附件）
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ISS = os.path.join(ROOT, "installer.iss")
PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"
ISCC = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")


def run(cmd, **kw):
    print("›", " ".join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8",
                       errors="replace", **kw)
    if r.returncode != 0:
        print(f"✗ 失败（退出码 {r.returncode}）")
        sys.exit(1)
    return r


def read_version():
    with open(ISS, encoding="utf-8") as f:
        s = f.read()
    m = re.search(r'#define MyAppVersion "([^"]+)"', s)
    if not m:
        sys.exit("✗ installer.iss 里找不到 MyAppVersion")
    return m.group(1)


def write_version(ver):
    with open(ISS, encoding="utf-8") as f:
        s = f.read()
    s = re.sub(r'(#define MyAppVersion ")[^"]+(")', rf"\g<1>{ver}\g<2>", s)
    with open(ISS, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)
    print(f"✓ 版本号改为 {ver}")


def main():
    ver = sys.argv[1] if len(sys.argv) > 1 else None
    title = sys.argv[2] if len(sys.argv) > 2 else None

    if ver:
        write_version(ver)
    ver = read_version()
    title = title or f"v{ver}"
    tag = f"v{ver}"
    setup = os.path.join(ROOT, "installer", f"VideoGrabber-Setup-{ver}.exe")

    print(f"\n=== 发版 {tag} ===\n")

    # 1. 检查有没有未提交改动（提醒但不阻断，后面会一起提交）
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    if r.stdout.strip():
        print("有未提交改动，将一并提交：")
        print(r.stdout.strip()[:500], "\n")

    # 2. 打 exe
    run([PY, "-m", "PyInstaller", "--onefile", "--name", "VideoGrabber",
         "--add-data", "index.html;.", "--add-data", "extension;extension",
         "--add-binary", r"E:\ffmpeg\bin\ffmpeg.exe;.", "--collect-all", "yt_dlp",
         "--noconfirm", "server.py"])
    print("✓ exe 打包完成")

    # 3. 编译安装包
    run([ISCC, "installer.iss"])
    if not os.path.isfile(setup):
        sys.exit(f"✗ 没生成 {setup}")
    size_mb = os.path.getsize(setup) / 1024 / 1024
    print(f"✓ 安装包 {os.path.basename(setup)}（{size_mb:.1f} MB）")

    # 4. 提交推送
    run(["git", "add", "-A"])
    msg = f"release: {tag}"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    if "nothing to commit" not in (r.stdout or "") + (r.stderr or ""):
        print("✓ 已提交")
    run(["git", "push", "origin", "main"])
    print("✓ 代码已推送")

    # 5. tag
    subprocess.run(["git", "tag", "-d", tag], cwd=ROOT,
                   capture_output=True)          # 本地同名 tag 先删掉，可重跑
    run(["git", "tag", "-a", tag, "-m", f"{tag}"])
    run(["git", "push", "origin", tag])
    print(f"✓ tag {tag} 已推送")

    # 6. Release
    subprocess.run(["gh", "release", "delete", tag, "-y"], cwd=ROOT,
                   capture_output=True)          # 已存在就先删，保证幂等
    notes = f"版本 {ver}，详见 commit 记录。\n\n安装：下载 `{os.path.basename(setup)}` 双击安装，覆盖安装保留配置和数据。"
    run(["gh", "release", "create", tag, "--title", title,
         "--notes", notes, setup])

    print(f"\n✅ 发布完成")
    print(f"   https://github.com/YETIANs/VideoGrabber/releases/tag/{tag}")


if __name__ == "__main__":
    main()
