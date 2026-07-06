#!/usr/bin/env python3
"""部署包打包工具 — 从编译产物 + 依赖库 + 模板组装完整部署包。

与 build.sh 解耦：build.sh 只负责编译（cmake + make），本脚本负责打包。
所有文件路径在 package_config.toml 中声明，改文件名/加减文件只改配置不改代码。

用法:
  python package.py --config low --repo-root /path/to/cluster_framework

流程:
  1. 组装打包目录（模板 + 编译产物注入）
  2. 拷依赖库 .so（sha256 缓存 + 硬链接 + 去重为符号链接）
  3. 生成 BUILD_MANIFEST（调用 gen_manifest.py）
  4. 打 zip（保留符号链接）
  5. 归档到 output/archive + 旧包轮转（保留最近 5 个）
"""

import argparse
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_BJT = timezone(timedelta(hours=8))
SO_CACHE_DIR = Path.home() / ".cache" / "cluster_so_cache"
MAX_ARCHIVE_PACKAGES = 5


# ==================== 配置加载 ====================

def _parse_toml_val(val):
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    if val == "true":
        return True
    if val == "false":
        return False
    if val.startswith("["):
        return re.findall(r'"([^"]*)"', val)
    try:
        return int(val)
    except ValueError:
        return val


def load_config(config_path):
    """加载 package_config.toml，返回 dict。"""
    config = {}
    cur_array = None
    cur_table = None
    for raw in Path(config_path).read_text(encoding="utf-8").splitlines():
        line = re.sub(r"#.*$", "", raw).strip()
        if not line:
            continue
        m = re.match(r"^\[\[(\w+)\]\]$", line)
        if m:
            cur_array = m.group(1)
            cur_table = None
            config.setdefault(cur_array, []).append({})
            continue
        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            cur_table = m.group(1)
            cur_array = None
            config[cur_table] = {}
            continue
        m = re.match(r"^(\w+)\s*=\s*(.*)$", line)
        if m:
            key, val = m.group(1), _parse_toml_val(m.group(2).strip())
            if cur_array:
                config[cur_array][-1][key] = val
            elif cur_table:
                config[cur_table][key] = val
            else:
                config[key] = val
    return config


# ==================== 工具函数 ====================

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_info(repo_root):
    def _run(*args):
        r = subprocess.run(["git", *args], cwd=str(repo_root),
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    return _run("rev-parse", "--abbrev-ref", "HEAD"), \
           _run("rev-parse", "--short", "HEAD"), \
           ("dirty" if _run("status", "--porcelain") else "clean")


# ==================== Step 1: 组装打包目录 ====================

def assemble_pkg_dir(pkg_dir, template_dir, config, cfg_num, repo_root):
    print("[1/5] 组装打包目录")
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True)
    shutil.copytree(template_dir, pkg_dir, dirs_exist_ok=True)
    template_map_path = pkg_dir / ".template_map.tsv"
    with open(template_map_path, "w", encoding="utf-8") as f:
        for tpl_file in sorted(template_dir.rglob("*")):
            if tpl_file.is_file():
                f.write(f"{tpl_file.relative_to(template_dir).as_posix()}\n")
    for sub in ("bin", "lib", "logOutput"):
        (pkg_dir / sub).mkdir(exist_ok=True)

    for binary in config.get("binary", []):
        src = repo_root / binary["src"].replace("{cfg}", cfg_num)
        dst = pkg_dir / binary["dst"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not src.exists():
            if binary.get("optional"):
                print(f"  警告: {binary['dst']} 源不存在: {src}")
                continue
            print(f"  错误: {binary['dst']} 源不存在: {src}", file=sys.stderr)
            sys.exit(1)
        shutil.copy2(src, dst)
        if "chmod" in binary:
            dst.chmod(int(str(binary["chmod"]), 8))
        print(f"  拷 {binary['dst']}")

    preserve_patterns = config.get("cleanup", {}).get("preserve", [])
    for f in pkg_dir.glob("lib/*.so*"):
        if any(fnmatch.fnmatch(f.name, p) for p in preserve_patterns):
            continue
        f.unlink()

    inject_map_path = pkg_dir / ".inject_map.tsv"
    with open(inject_map_path, "w", encoding="utf-8") as f:
        for inj in config.get("inject", []):
            src = repo_root / inj["src"]
            dst = pkg_dir / inj["dst"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not src.exists():
                print(f"  跳过注入 {inj['dst']}: 源文件不存在，保留模板版本")
                continue
            shutil.copy2(src, dst)
            if "chmod" in inj:
                dst.chmod(int(str(inj["chmod"]), 8))
            f.write(f"{inj['dst']}\t源码: {inj['src']}\n")
            print(f"  注入 {inj['dst']} (来自源码)")

    return pkg_dir


# ==================== Step 2: 拷依赖库 .so ====================

def copy_so_files(pkg_dir, config_name, config, repo_root, source_map_path):
    print("\n[2/5] 拷依赖库 .so")
    SO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    source_map_path.write_text("", encoding="utf-8")
    cfg_num = "8675" if config_name == "low" else "8676"

    sha_map = {}
    for so_src in config.get("so_source", []):
        valid_configs = so_src.get("configs")
        if valid_configs and config_name not in valid_configs:
            continue
        src_dir = repo_root / so_src["dir"].replace("{cfg}", cfg_num)
        sha_map.update(_copy_so_from(src_dir, so_src["label"], pkg_dir / "lib", repo_root, source_map_path))

    exclude_libs = config.get("exclude_libs", {}).get(config_name, [])
    for pattern in exclude_libs:
        for f in (pkg_dir / "lib").glob(pattern):
            f.unlink()
        print(f"  ({config_name} 已排除 {pattern})")

    for f in (pkg_dir / "lib").glob("*.so*"):
        if f.is_symlink():
            continue
        f.chmod(0o755)

    return sha_map


def _copy_so_from(src_dir, label, dest_dir, repo_root, source_map_path):
    """拷贝 .so 文件，返回 {sha256: filename} 供去重步骤复用，避免重复 sha256。"""
    if not src_dir.is_dir():
        print(f"  跳过 {label}: 源目录不存在")
        return {}
    src_rel = str(src_dir.relative_to(repo_root))
    copied = cached = 0
    sha_map = {}
    with open(source_map_path, "a", encoding="utf-8") as sm:
        for f in sorted(src_dir.glob("*.so*")):
            if f.suffix in (".a", ".la"):
                continue
            if not f.is_file():
                continue
            fname = f.name
            f_sha = sha256_file(f)
            cache_file = SO_CACHE_DIR / f"{f_sha}.so"
            if cache_file.exists():
                try:
                    os.link(cache_file, dest_dir / fname)
                except OSError:
                    shutil.copy2(cache_file, dest_dir / fname)
                cached += 1
            else:
                shutil.copy2(f, dest_dir / fname)
                tmp = SO_CACHE_DIR / f"{f_sha}.so.tmp.{os.getpid()}"
                shutil.copy2(f, tmp)
                tmp.rename(cache_file)
                copied += 1
            sm.write(f"{fname}\t{src_rel}\t{label}\n")
            sha_map[fname] = f_sha
    if cached:
        print(f"  {label}: {copied} 新增, {cached} 缓存命中 (源: {src_rel})")
    else:
        print(f"  {label}: {copied} 个 .so (源: {src_rel})")
    return sha_map


def dedup_so_files(pkg_dir, sha_map=None):
    print("  去重: 相同内容的 .so -> 符号链接")
    lib_dir = pkg_dir / "lib"
    files = sorted(lib_dir.glob("*.so*"), key=lambda p: len(p.name), reverse=True)
    sha_kept = {}
    dedup_count = 0
    for f in files:
        if f.is_symlink():
            continue
        f_sha = sha_map.get(f.name) if sha_map else None
        if not f_sha:
            f_sha = sha256_file(f)
        if f_sha in sha_kept:
            f.unlink()
            os.symlink(sha_kept[f_sha].name, f)
            dedup_count += 1
        else:
            sha_kept[f_sha] = f
    print(f"  去重完成: {dedup_count} 个文件转为符号链接")


# ==================== Step 3: BUILD_MANIFEST ====================

def generate_manifest(pkg_dir, config, repo_root):
    print("\n[3/5] 生成 BUILD_MANIFEST")
    gen_manifest = Path(__file__).parent / "gen_manifest.py"
    if not gen_manifest.exists():
        print(f"  警告: gen_manifest.py 不存在，跳过清单生成")
        return
    r = subprocess.run(
        [sys.executable, str(gen_manifest), "--release", str(pkg_dir),
         "--config", config, "--repo-root", str(repo_root)],
        capture_output=True, text=True,
    )
    if r.stdout:
        print(f"  {r.stdout.strip()}")
    if r.returncode != 0 and r.stderr:
        print(f"  错误: {r.stderr.strip()}")
    for tmpf in (".source_map.tsv", ".template_map.tsv", ".inject_map.tsv"):
        tmp_path = pkg_dir / tmpf
        if tmp_path.exists():
            tmp_path.unlink()


# ==================== Step 4: 打 zip ====================

def create_zip(pkg_dir, pkg_base, zip_path):
    print("\n[4/5] 打包 zip")
    if zip_path.exists():
        zip_path.unlink()
    import zipfile
    import stat
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(pkg_dir):
            for f in sorted(files):
                fp = Path(root) / f
                arc = fp.relative_to(pkg_base)
                if fp.is_symlink():
                    zi = zipfile.ZipInfo(str(arc))
                    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                    zf.writestr(zi, os.readlink(fp))
                else:
                    zf.write(fp, str(arc))
    print(f"  已生成: {zip_path}")
    print(f"  包大小: {zip_path.stat().st_size / (1024*1024):.1f}MB")


# ==================== Step 5: 归档 + 轮转 ====================

def archive_and_rotate(zip_path, pkg_dir, archive_dir):
    print("\n[5/5] 归档 + 轮转")
    manifest_src = pkg_dir / "BUILD_MANIFEST.toml"
    manifest_dst = archive_dir / (zip_path.stem + ".manifest.toml")
    if manifest_src.exists():
        shutil.copy2(manifest_src, manifest_dst)
        print(f"  清单归档: {manifest_dst.name}")
    zips = sorted(archive_dir.glob("cluster_*.zip"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for old_zip in zips[MAX_ARCHIVE_PACKAGES:]:
        old_zip.unlink()
        for ext in (".manifest.toml", ".builder"):
            f = old_zip.with_suffix(ext)
            if f.exists():
                f.unlink()
        print(f"  轮转删除: {old_zip.name}")


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser(description="部署包打包工具")
    parser.add_argument("--config", required=True, choices=["low", "high"])
    parser.add_argument("--repo-root", required=True, help="cluster_framework 工程根目录")
    parser.add_argument("--template-dir",
                        help="打包模板目录（默认 PACKAGING_TEMPLATE_DIR 环境变量）")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    cfg_num = "8675" if args.config == "low" else "8676"

    config_path = Path(__file__).parent / "package_config.toml"
    config = load_config(config_path)

    template_base = args.template_dir or os.environ.get("PACKAGING_TEMPLATE_DIR") or str(repo_root / "prompt/部署/新项目")
    template_dir = Path(template_base) / f"cluster_{cfg_num}"
    archive_dir = repo_root / "output" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    if not template_dir.is_dir():
        print(f"错误: 打包模板目录不存在: {template_dir}", file=sys.stderr)
        sys.exit(1)

    _, commit, dirty = git_info(repo_root)
    ts = datetime.now(tz=_BJT).strftime("%Y%m%d_%H%M%S")
    zip_name = f"cluster_{cfg_num}_{commit}_{dirty}_{ts}"

    pkg_base = Path.home() / ".cache" / f"cluster_pkg_{os.getpid()}"
    pkg_dir = pkg_base / f"cluster_{cfg_num}"
    zip_path = archive_dir / f"{zip_name}.zip"
    source_map_path = pkg_dir / ".source_map.tsv"

    print(f"{'='*42}")
    print(f"打包: {zip_name}")
    print(f"{'='*42}")

    try:
        assemble_pkg_dir(pkg_dir, template_dir, config, cfg_num, repo_root)
        source_map_path = pkg_dir / ".source_map.tsv"
        sha_map = copy_so_files(pkg_dir, args.config, config, repo_root, source_map_path)
        dedup_so_files(pkg_dir, sha_map)
        generate_manifest(pkg_dir, args.config, repo_root)
        create_zip(pkg_dir, pkg_base, zip_path)
        archive_and_rotate(zip_path, pkg_dir, archive_dir)

        manifest_dst = archive_dir / (zip_path.stem + ".manifest.toml")
        print(f"\n{'='*42}")
        print("打包完成")
        print(f"  发布包: {zip_path}")
        print(f"  清单:   {manifest_dst}")
        print(f"{'='*42}")
    finally:
        if pkg_base.exists():
            shutil.rmtree(pkg_base, ignore_errors=True)


if __name__ == "__main__":
    main()
