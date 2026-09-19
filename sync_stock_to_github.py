#!/usr/bin/env python3
"""
sync_stock_to_github.py
监控股票系统文件变更，自动同步到GitHub。
用法：python3 sync_stock_to_github.py [--force]
"""
import subprocess
import sys
import hashlib
from pathlib import Path

# 配置
REPO_DIR = Path.home() / ".hermes" / "github-repo"  # 本地仓库副本
REMOTE = "origin"
BRANCH = "main"
SRC_DIR = Path.home() / ".hermes" / "scripts"
DST_DIR = REPO_DIR / "projects" / "stock-system"

# 需要同步的文件
SYNC_FILES = [
    "chanlun_strategy.py",
    "chanlun_engine.py",
    "stock_screening.py",
    "sim_portfolio.py",
    "stock_scorer.py",
    "stock_config.json",
    "kline_db.py",
    "update_kline_db.py",
    "stock_whitelist.py",
    "stock_portfolio_task.py",
    "stock_log.py",
]

HASH_FILE = Path.home() / ".hermes" / "cache" / ".stock_sync_hash"


def file_hash(path):
    if not path.exists():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


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


def main():
    force = "--force" in sys.argv

    # 检查仓库是否存在
    if not REPO_DIR.exists() or not (REPO_DIR / ".git").exists():
        # 克隆仓库
        print("首次运行，克隆仓库...")
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "git@github.com:wm20119/my-github-repo.git", str(REPO_DIR)], check=True)

    # 检查是否有变更
    old_hashes = load_hashes()
    new_hashes = {}
    changed = []

    for fname in SYNC_FILES:
        src = SRC_DIR / fname
        h = file_hash(src)
        new_hashes[fname] = h
        if force or old_hashes.get(fname) != h:
            changed.append(fname)

    if not changed and not force:
        print("无变更，跳过同步")
        return

    # 复制变更文件
    DST_DIR.mkdir(parents=True, exist_ok=True)
    for fname in changed:
        src = SRC_DIR / fname
        dst = DST_DIR / fname
        if src.exists():
            dst.write_bytes(src.read_bytes())
            print(f"  更新: {fname}")

    # 提交并推送
    run(["git", "add", "projects/stock-system/"])
    stdout, rc = run(["git", "diff", "--cached", "--quiet"])
    if rc == 0 and not force:
        print("无变更需要提交")
        return

    commit_msg = f"🔄 自动同步股票系统 {len(changed)}个文件"
    run(["git", "commit", "-m", commit_msg])
    stdout, rc = run(["git", "push", REMOTE, BRANCH])

    if rc == 0:
        save_hashes(new_hashes)
        print(f"✅ 同步成功: {len(changed)}个文件")
    else:
        print(f"❌ 推送失败: {stdout}")


if __name__ == "__main__":
    main()
