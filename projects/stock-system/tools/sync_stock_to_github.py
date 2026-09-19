#!/usr/bin/env python3
"""
sync_stock_to_github.py
将整个 stock_system/ 目录同步到 GitHub，保持两边一致。
用法：python3 sync_stock_to_github.py [--force]
"""
import subprocess
import sys
import hashlib
from pathlib import Path

# 配置
REPO_DIR = Path.home() / ".hermes" / "github-repo"
REMOTE = "origin"
BRANCH = "main"
SRC_DIR = Path.home() / ".hermes" / "scripts" / "stock_system"
DST_DIR = REPO_DIR / "projects" / "stock-system"
HASH_FILE = Path.home() / ".hermes" / "cache" / ".stock_sync_hash"


def file_hash(path):
    if not path.exists():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


def dir_hashes(src_dir):
    """递归计算目录下所有文件的hash"""
    hashes = {}
    for f in sorted(src_dir.rglob("*")):
        if f.is_file() and "__pycache__" not in str(f):
            rel = str(f.relative_to(src_dir))
            hashes[rel] = file_hash(f)
    return hashes


def load_hashes():
    if not HASH_FILE.exists():
        return {}
    hashes = {}
    for line in HASH_FILE.read_text().strip().split("\n"):
        if "|" in line:
            name, h = line.split("|", 1)
            hashes[name] = h
    return hashes


def save_hashes(hashes):
    HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"{k}|{v}" for k, v in hashes.items())
    HASH_FILE.write_text(content)


def run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_DIR), **kwargs)
    return result.stdout.strip(), result.returncode


def sync_dir(src, dst):
    """递归同步目录"""
    dst.mkdir(parents=True, exist_ok=True)
    # 先删除目标中多余的文件
    src_files = {str(f.relative_to(src)) for f in src.rglob("*") if f.is_file() and "__pycache__" not in str(f)}
    for f in dst.rglob("*"):
        if f.is_file() and str(f.relative_to(dst)) not in src_files:
            f.unlink()
    # 复制所有文件
    for f in src.rglob("*"):
        if f.is_file() and "__pycache__" not in str(f):
            rel = f.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read_bytes())


def main():
    force = "--force" in sys.argv

    # 检查仓库
    if not REPO_DIR.exists() or not (REPO_DIR / ".git").exists():
        print("首次运行，克隆仓库...")
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "git@github.com:wm20119/my-github-repo.git", str(REPO_DIR)], check=True)

    # 对比hash
    old_hashes = load_hashes()
    new_hashes = dir_hashes(SRC_DIR)
    changed = [f for f in new_hashes if force or old_hashes.get(f) != new_hashes[f]]
    deleted = [f for f in old_hashes if f not in new_hashes]

    if not changed and not deleted and not force:
        print("无变更，跳过同步")
        return

    # 同步目录
    DST_DIR.mkdir(parents=True, exist_ok=True)
    sync_dir(SRC_DIR, DST_DIR)
    print(f"更新 {len(changed)} 个文件, 删除 {len(deleted)} 个文件")

    # 提交推送
    run(["git", "add", "projects/stock-system/"])
    stdout, rc = run(["git", "diff", "--cached", "--quiet"])
    if rc == 0 and not force:
        print("无变更需要提交")
        return

    commit_msg = f"🔄 同步股票系统 ({len(changed)}改 {len(deleted)}删)"
    run(["git", "commit", "-m", commit_msg])
    stdout, rc = run(["git", "push", REMOTE, BRANCH])

    if rc == 0:
        save_hashes(new_hashes)
        print(f"✅ 推送成功")
    else:
        print(f"❌ 推送失败: {stdout}")


if __name__ == "__main__":
    main()
