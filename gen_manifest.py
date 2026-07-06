#!/usr/bin/env python3
"""thirdparty / lib 依赖库版本指纹工具。

L1 指纹基线:
  python tools/gen_manifest.py            扫描 .so 生成/更新 manifest.lock
  python tools/gen_manifest.py --check    对比磁盘与现有 lock，报告变更（不改文件）
  python tools/gen_manifest.py --toml     打印 manifest.toml 元数据骨架（供人工填充）

L2 部署包清单:
  python tools/gen_manifest.py --release <pkg_dir> [--config low|high]
          扫描 <pkg_dir>/ 全部文件（bin/lib/etc/logOutput/根目录），按目录规则
          分类来源（编译产物/依赖库/HMI/配置/脚本/文档），每个文件算 sha256，
          生成 BUILD_MANIFEST.toml 放到 <pkg_dir>/，供 build.sh 打包时调用。

扫描范围: thirdparty/**/*.so* 与 lib/**/*.so*，含 .so / .so.1 / .so.1.0.3 等
版本化文件（Linux SONAME 链接也一并记录）。
"""

import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# 极简 TOML 解析器（零外部依赖，自给自足）
# ---------------------------------------------------------------------------
# 本工程用到的 TOML 语法子集（manifest.lock + manifest.toml）：
#   - `# 注释`
#   - `[section]` 单表头（section 名可含 `-`，如 `[protobuf-lite]`）
#   - `[[entry]]` 数组表头
#   - key = "string"        字符串
#   - key = 123             整数
#   - key = [ "a", "b" ]    字符串数组（可跨行）
# 不支持 datetime / 浮点 / inline table / 多行字符串等复杂特性，
# 因为 manifest.lock/toml 没用到。保持极简是为了零依赖、换任何机器都能跑。


def _mini_toml_loads(text):
    """解析 TOML 子集，返回 {section: value | [value]} 字典。

    section 值为 dict（单表）或 list[dict]（数组表，如 [[entry]]）。
    """
    result = {}
    cur_section = None          # 当前表名（str | None）
    cur_is_array = False        # 当前表是否是 [[array]]
    cur_array_key = None        # 正在收集的数组 key（跨行模式）
    cur_array_items = []        # 正在收集的数组元素

    # 表头/键值行的正则（section 名允许字母数字下划线连字符）
    RE_ARRAY_TBL = re.compile(r"^\[\[([\w-]+)\]\]$")
    RE_TBL = re.compile(r"^\[([\w-]+)\]$")
    RE_KV = re.compile(r"^(\w+)\s*=\s*(.*)$")

    def _strip_comment(line):
        idx = line.find("#")
        return (line[:idx] if idx >= 0 else line).strip()

    def _parse_scalar(raw):
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            return raw[1:-1]
        if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
            return raw[1:-1]
        try:
            return int(raw)
        except ValueError:
            return raw

    def _set(key, val):
        """把 key=value 写入当前 section（区分单表/数组表）。"""
        if cur_section is None:
            return
        if cur_is_array:
            result[cur_section][-1][key] = val
        else:
            result[cur_section][key] = val

    def _flush_array():
        nonlocal cur_array_key, cur_array_items
        if cur_array_key is not None:
            _set(cur_array_key, list(cur_array_items))
        cur_array_key = None
        cur_array_items = []

    # 从一行里提取所有 "..." 字符串（用于数组元素收集）
    RE_STR = re.compile(r'"([^"]*)"')

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        if not line:
            continue

        # 正在收集跨行数组？
        if cur_array_key is not None:
            items_here = RE_STR.findall(line)
            cur_array_items.extend(items_here)
            if "]" in line:
                _flush_array()
            continue

        # 数组表头 [[entry]]
        m = RE_ARRAY_TBL.match(line)
        if m:
            name = m.group(1)
            cur_section = name
            cur_is_array = True
            result.setdefault(name, []).append({})
            continue

        # 单表头 [section]
        m = RE_TBL.match(line)
        if m:
            name = m.group(1)
            cur_section = name
            cur_is_array = False
            result.setdefault(name, {})
            continue

        # key = value
        m = RE_KV.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith("["):
                if val.endswith("]"):
                    # 单行数组
                    items = [
                        _parse_scalar(s.strip())
                        for s in val[1:-1].split(",")
                        if s.strip()
                    ]
                    _set(key, items)
                else:
                    # 跨行数组开始
                    cur_array_key = key
                    cur_array_items = RE_STR.findall(val)
                    if "]" in val:
                        _flush_array()
            else:
                _set(key, _parse_scalar(val))

    # 文件结束时若数组未闭合（异常情况），兜底 flush
    _flush_array()
    return result


def _toml_load(path):
    """读取 TOML 文件并用内置 mini parser 解析。"""
    return _mini_toml_loads(Path(path).read_text(encoding="utf-8"))

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["thirdparty", "lib"]

# 匹配 .so 及其版本化变体: libfoo.so / libfoo.so.1 / libfoo.so.1.0.3
SO_RE = re.compile(r"\.so(\.\d+)*$")


def find_so_files():
    """递归扫描 SCAN_DIRS 下的所有 .so 文件（含版本号），按路径排序。"""
    files = []
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and SO_RE.search(p.name):
                files.append(p)
    files.sort()
    return files


def fingerprint(path):
    """计算单个 .so 的指纹: sha256 / size / mtime(UTC)。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    st = path.stat()
    rel = path.relative_to(REPO_ROOT).as_posix()
    return {
        "path": rel,
        "sha256": h.hexdigest(),
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


def render_lock(entries):
    """把指纹列表渲染成 TOML 文本。"""
    now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# 由 tools/gen_manifest.py 自动生成，请勿手改",
        f"# 生成时间(UTC): {now_utc}",
        "# 扫描范围: thirdparty/ + lib/ 下的 .so 及版本化文件(.so.N)",
        "#",
        "# 用途: 重新跑 gen_manifest.py 后 git diff 此文件，即可看到哪些",
        "# 依赖库有更新（sha256 变化）。人工元数据见 manifest.toml。",
        "",
    ]
    for e in entries:
        lines.append("[[entry]]")
        lines.append(f'path = "{e["path"]}"')
        lines.append(f'sha256 = "{e["sha256"]}"')
        lines.append(f'size = {e["size"]}')
        lines.append(f'mtime = "{e["mtime"]}"')
        lines.append("")
    return "\n".join(lines) + "\n"


def load_lock():
    """读取现有 manifest.lock，返回 {path: entry} 字典。"""
    lock_path = REPO_ROOT / "thirdparty" / "manifest.lock"
    if not lock_path.exists():
        return {}
    data = _toml_load(lock_path)
    return {e["path"]: e for e in data.get("entry", [])}


def short(sha):
    return sha[:12]


def cmd_generate(_args):
    files = find_so_files()
    entries = [fingerprint(p) for p in files]
    lock_path = REPO_ROOT / "thirdparty" / "manifest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(render_lock(entries), encoding="utf-8")
    print(f"已生成 {lock_path.relative_to(REPO_ROOT)}: {len(entries)} 个 .so")


def compute_diff():
    """对比磁盘实际指纹与现有 lock，返回结构化变更数据。

    供网页工具 import 使用（不走子进程）。返回 dict:
      {locked_count, disk_count, unchanged[], changed[], new[], removed[]}
    changed 元素: {path, old_sha, new_sha, old_mtime, new_mtime}
    其余为 path 字符串列表。
    """
    disk = {e["path"]: e for e in (fingerprint(p) for p in find_so_files())}
    locked = load_lock()

    unchanged, changed, new, removed = [], [], [], []
    for path, de in disk.items():
        if path not in locked:
            new.append(path)
        elif de["sha256"] == locked[path]["sha256"]:
            unchanged.append(path)
        else:
            changed.append({
                "path": path,
                "old_sha": locked[path]["sha256"],
                "new_sha": de["sha256"],
                "old_mtime": locked[path].get("mtime", ""),
                "new_mtime": de["mtime"],
            })
    for path in locked:
        if path not in disk:
            removed.append(path)

    return {
        "locked_count": len(locked),
        "disk_count": len(disk),
        "unchanged": unchanged,
        "changed": changed,
        "new": new,
        "removed": removed,
    }


def cmd_check(_args):
    """对比磁盘实际指纹与现有 lock，报告 unchanged/changed/new/removed。"""
    diff = compute_diff()
    locked_count = diff["locked_count"]
    if not locked_count:
        print("未找到 manifest.lock，请先运行: python tools/gen_manifest.py")
        sys.exit(1)

    unchanged = diff["unchanged"]
    changed = diff["changed"]
    new = diff["new"]
    removed = diff["removed"]

    print(f"基线 lock: {locked_count} 个 | 磁盘: {diff['disk_count']} 个")
    print(f"未变更: {len(unchanged)}")
    if changed:
        print(f"已变更: {len(changed)}")
        for c in changed:
            print(f"  [CHG] {c['path']}")
            print(f"        {short(c['old_sha'])} -> {short(c['new_sha'])}")
    if new:
        print(f"新增: {len(new)}")
        for path in new:
            print(f"  [NEW] {path}")
    if removed:
        print(f"删除: {len(removed)}")
        for path in removed:
            print(f"  [DEL] {path}")

    if changed or new or removed:
        sys.exit(2)  # 有变更，退出码 2 便于脚本/CI 判断


# --- manifest.toml 骨架生成 -------------------------------------------------

def _lib_base(name):
    """libfoo.so.1.0.3 -> foo；libfoo.so -> foo。"""
    m = re.match(r"^lib(.+?)\.so(\.\d+)*$", name)
    return m.group(1) if m else name


def _version_from_name(name):
    """libfoo.so.1.0.3 -> 1.0.3；libfoo.so -> None。"""
    m = re.search(r"\.so\.([\d.]+)$", name)
    return m.group(1) if m else None


def cmd_toml(_args):
    """扫描 .so 后按库基础名分组，打印 manifest.toml 骨架供人工填充。"""
    entries = [fingerprint(p) for p in find_so_files()]
    groups = {}
    for e in entries:
        base = _lib_base(Path(e["path"]).name)
        groups.setdefault(base, {"paths": [], "ver": None})
        groups[base]["paths"].append(e["path"])
        ver = _version_from_name(Path(e["path"]).name)
        if ver and not groups[base]["ver"]:
            groups[base]["ver"] = ver

    lines = [
        "# 第三方依赖库元数据声明（人工维护）",
        "# 每个库声明来源与版本意图；实际文件指纹见 manifest.lock（自动生成）。",
        "# 重新生成骨架: python tools/gen_manifest.py --toml",
        "",
    ]
    for base in sorted(groups):
        info = groups[base]
        ver = info["ver"] or "TODO"
        lines.append(f"[{base}]")
        lines.append(f'source  = "TODO"   # 来源: repo@commit 或 包名@版本（已检测版本号: {ver}）')
        lines.append(f'role    = "TODO"   # 用途')
        lines.append("configs = [")
        for p in info["paths"]:
            lines.append(f'  "{p}",')
        lines.append("]")
        lines.append("")
    print("\n".join(lines))


# --- L2: 部署包 BUILD_MANIFEST 生成 ----------------------------------------

def _git_info():
    """获取当前 git 仓库信息: branch / commit / dirty。

    dirty 判断用 git status --porcelain（含未跟踪文件），与 release.sh 一致。
    """
    def _run(*args):
        r = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"

    branch = _run("rev-parse", "--abbrev-ref", "HEAD")
    commit = _run("rev-parse", "--short", "HEAD")
    status = _run("status", "--porcelain")
    dirty = "dirty" if status else "clean"
    return branch, commit, dirty


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _classify_file(rel_path, name, source_map=None, template_map=None, inject_map=None):
    """根据文件在部署包中的相对路径和文件名，推断来源类型和用途说明。

    返回 (category, source, desc) 三元组：
      category: bin / lib / hmi / config / script / doc / other
      source:   来源说明（从哪来的）
      desc:     用途说明（这个文件干什么用的）

    source_map:   build.sh 记录的 {文件名: 相对源目录} 映射，优先用于 .so 精确标注来源
                  （区分 thirdparty/.../8675 与 8676，部署包目录已扁平化丢失这层信息）。
    template_map: 模板文件路径集合（部署包中各文件的相对路径），标注"来自模板里的哪个文件"。
    inject_map:   源码注入映射 {部署包路径: 来源标签}，优先级高于 template_map：
                  文件在 inject_map → 来自源码；否则查看 template_map → 来自模板。
    """
    rel = rel_path.as_posix() if hasattr(rel_path, "as_posix") else str(rel_path)
    parts = rel.split("/")

    def _tmpl(k):
        """统一来源解析：inject_map > template_map > 回退到通用标签 k。"""
        if inject_map and rel in inject_map:
            return inject_map[rel]
        if template_map and rel in template_map:
            return f"模板: {rel}"
        return k

    # lib/ 下的 .so → 依赖库
    if parts[0] == "lib":
        base = _lib_base(name)
        # 用途描述（与来源无关）
        desc_info = {
            "cocos":                "HMI 引擎渲染库（Cocos2d-x）",
            "dlt":                  "DLT 日志通信库（COVESA DLT）",
            "log":                  "自研日志库",
            "protobuf":             "Google Protobuf 序列化库",
            "protobuf-lite":        "Google Protobuf 精简版",
            "protoc":               "Google Protobuf 编译器运行时",
            "wtcommon":             "WtCommon 公共工具库",
            "wtclusterdataservice": "WtClusterDataService 数据服务库",
            "acdds_adaptor":        "长安 DDS 适配层（仅高配）",
            "acddscxx":             "长安 DDS C++ 接口",
            "ca_com":               "DDS 通信库",
            "dds_cadiag":           "DDS 诊断接口",
            "ddsc":                 "Cyclone DDS C 接口",
            "ddscxx":               "Cyclone DDS C++ 接口",
            "soacm_log_imp":        "DDS 日志实现",
            "z":                    "zlib 压缩库（仅高配）",
        }
        desc = desc_info.get(base, f"依赖库 ({base})")
        # 优先用 build.sh 记录的真实源目录（含 8675/8676 区分）
        if source_map and name in source_map:
            return ("lib", source_map[name], desc)
        # 模板自带（如 libcocos.so，不从 copy_so_from 拷贝）
        if base == "cocos":
            return ("lib", _tmpl("模板自带"), desc)
        return ("lib", "thirdparty", desc)

    # bin/ 下的文件
    if parts[0] == "bin":
        bin_info = {
            "cluster":      ("CMake 编译产物",   "Cluster 中间件主程序"),
            "dlt-daemon":   ("DLT 预编译程序",   "DLT 日志守护进程（COVESA dlt-daemon）"),
            "hmi-launcher": ("HMI 预编译程序",   "HMI 渲染进程（Cocos 引擎）"),
            "data.zip":     ("HMI 资源",        "HMI 资源包（Cocos 图片/UI 资源）"),
        }
        if name in bin_info:
            src, desc = bin_info[name]
            return ("hmi" if name in ("hmi-launcher", "data.zip") else "bin", src, desc)
        if name.endswith(".json"):
            return ("config", _tmpl("模板自带"), "应用配置文件")
        return ("bin", _tmpl("模板自带"), "bin/ 其他文件")

    # etc/ 下
    if parts[0] == "etc":
        if len(parts) > 1 and parts[1] == "systemd":
            svc_info = {
                "cluster.service":     "Cluster 中间件 systemd 服务单元",
                "cluster-dlt.service": "DLT 守护进程 systemd 服务单元",
                "cluster-hmi.service": "HMI 进程 systemd 服务单元",
                "hmi-launcher.service": "HMI 进程 systemd 服务单元",
            }
            return ("config", _tmpl("模板自带"), svc_info.get(name, "systemd 服务配置"))
        if name == "feature_config.json":
            return ("config", _tmpl("模板自带"), "车辆功能配置（语言/区域/车型/功能开关）")
        if name.endswith(".json"):
            return ("config", _tmpl("模板自带"), "配置文件")
        if name.endswith(".conf"):
            return ("config", _tmpl("模板自带"), "DLT 日志配置")
        return ("config", _tmpl("模板自带"), "etc/ 配置文件")

    # logOutput/ 下
    if parts[0] == "logOutput":
        if name == "dlt.conf":
            return ("config", _tmpl("模板自带"), "DLT 守护进程配置文件")
        if name == "dlt_logstorage.conf":
            return ("config", _tmpl("模板自带"), "DLT 日志存储过滤配置")
        return ("config", _tmpl("模板自带"), "logOutput/ 配置文件")

    # 根目录脚本
    script_info = {
        "deploy.sh":       (_tmpl("模板自带"), "PC 端一键部署脚本（推送+安装+健康检查）"),
        "install.sh":      (_tmpl("模板自带"), "主机端 systemd 服务安装脚本"),
        "run_aarch64.sh":  (_tmpl("模板自带"), "aarch64 前台调试启动脚本（含录制开关）"),
        "run_x86_64.sh":   (_tmpl("模板自带"), "x86_64 前台调试启动脚本"),
        "stop_aarch64.sh": (_tmpl("模板自带"), "aarch64 停止脚本"),
    }
    if name in script_info:
        src, desc = script_info[name]
        return ("script", src, desc)
    if name.endswith(".sh"):
        return ("script", _tmpl("模板自带"), "部署脚本")

    # 文档
    doc_info = {
        "readme.txt": (_tmpl("模板自带"), "部署说明文档（8675 低配）"),
        "说明.txt":   (_tmpl("模板自带"), "部署说明文档（8676 高配）"),
    }
    if name in doc_info:
        src, desc = doc_info[name]
        return ("doc", src, desc)
    if name.endswith(".md") or name.endswith(".txt"):
        return ("doc", _tmpl("模板自带"), "说明文档")

    return ("other", "未知", "其他文件")


def cmd_release(args):
    """扫描 pkg_dir 全部文件，生成 BUILD_MANIFEST.toml 放到 pkg_dir/。

    分类规则见 _classify_file()。每个文件记录 path / sha256 / size / category / source / desc。
    BUILD_MANIFEST.toml 自身不计入（生成时还不存在）。
    """
    pkg_dir = Path(args.release).resolve()

    if not pkg_dir.is_dir():
        print(f"错误: 打包目录不存在: {pkg_dir}", file=sys.stderr)
        sys.exit(1)

    # 读取 build.sh 生成的源路径映射（文件名 → 相对源目录），精确标注 .so 来源(8675/8676)
    source_map = {}
    sm_path = pkg_dir / ".source_map.tsv"
    if sm_path.exists():
        for line in sm_path.read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and cols[0]:
                source_map[cols[0]] = cols[1]

    # 读取模板文件来源映射（部署包路径 → 模板内路径），标记部署包中哪些文件来自模板
    template_map = set()
    tm_path = pkg_dir / ".template_map.tsv"
    if tm_path.exists():
        template_map = set(line.strip() for line in tm_path.read_text(encoding="utf-8").splitlines() if line.strip())

    # 读取源码注入映射（部署包路径 → 来源标签），标记从源码注入的文件（优先级高于模板）
    inject_map = {}
    im_path = pkg_dir / ".inject_map.tsv"
    if im_path.exists():
        for line in im_path.read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and cols[0]:
                inject_map[cols[0]] = cols[1]

    # 递归扫描所有文件（排除 BUILD_MANIFEST.toml 自身与中间文件）
    all_files = sorted(
        p for p in pkg_dir.rglob("*")
        if p.is_file() and p.name not in ("BUILD_MANIFEST.toml", ".source_map.tsv", ".template_map.tsv", ".inject_map.tsv")
    )
    if not all_files:
        print(f"错误: {pkg_dir} 下未找到任何文件", file=sys.stderr)
        sys.exit(1)

    # git 信息 + 打包时间（生成时间用 UTC 元数据，pack_time 用北京时间显示）
    branch, commit, dirty = _git_info()
    now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_bjt = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    config = args.config or "unknown"

    # 分类统计
    entries = []
    cat_counts = {}
    for f in all_files:
        rel = f.relative_to(pkg_dir)
        sha = _sha256(f)
        size = f.stat().st_size
        category, source, desc = _classify_file(rel, f.name, source_map, template_map, inject_map)
        entries.append({
            "path": rel.as_posix(),
            "sha256": sha,
            "size": size,
            "category": category,
            "source": source,
            "desc": desc,
        })
        cat_counts[category] = cat_counts.get(category, 0) + 1

    # 渲染 TOML
    lines = [
        "# 部署包构建清单（自动生成，请勿手改）",
        f"# 生成时间(UTC): {now_utc}",
        f"# 文件总数: {len(entries)}",
        "",
        "[build]",
        f'config      = "{config}"',
        f'git_branch  = "{branch}"',
        f'git_commit  = "{commit}"',
        f'git_dirty   = "{dirty}"',
        f'pack_time   = "{now_bjt} (北京时间)"',
        f'total_files = {len(entries)}',
        "",
    ]

    # 按 category 分组输出
    cat_labels = {
        "bin": "编译产物 (bin)",
        "lib": "依赖库 (.so)",
        "hmi": "HMI 资源",
        "config": "配置文件",
        "script": "部署脚本",
        "doc": "说明文档",
        "other": "其他",
    }
    cat_order = ["bin", "lib", "hmi", "config", "script", "doc", "other"]

    for cat in cat_order:
        cat_entries = [e for e in entries if e["category"] == cat]
        if not cat_entries:
            continue
        label = cat_labels.get(cat, cat)
        lines.append(f"# {label} ({len(cat_entries)} 个)")
        for e in cat_entries:
            lines.append("[[file]]")
            lines.append(f'path     = "{e["path"]}"')
            lines.append(f'sha256   = "{e["sha256"]}"')
            lines.append(f'size     = {e["size"]}')
            lines.append(f'category = "{e["category"]}"')
            lines.append(f'source   = "{e["source"]}"')
            lines.append(f'desc     = "{e["desc"]}"')
            lines.append("")

    manifest_path = pkg_dir / "BUILD_MANIFEST.toml"
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 控制台摘要
    summary = " ".join(f"{cat}={cat_counts.get(cat, 0)}" for cat in cat_order if cat_counts.get(cat))
    print(f"已生成 {manifest_path.name}: {len(entries)} 个文件 ({summary})")


def main():
    parser = argparse.ArgumentParser(
        description="thirdparty/lib 依赖库版本指纹工具"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="对比报告变更，不改文件")
    group.add_argument("--toml", action="store_true", help="打印 manifest.toml 骨架")
    group.add_argument("--release", metavar="PKG_DIR", help="为部署包生成 BUILD_MANIFEST.toml")
    parser.add_argument("--config", choices=["low", "high"], help="配置标识（仅 --release 时有效）")
    parser.add_argument("--repo-root", help="代码工程根目录（供 git 信息获取，默认按 __file__ 推算）")
    args = parser.parse_args()

    global REPO_ROOT
    if args.repo_root:
        REPO_ROOT = Path(args.repo_root).resolve()

    if args.check:
        cmd_check(args)
    elif args.toml:
        cmd_toml(args)
    elif args.release:
        cmd_release(args)
    else:
        cmd_generate(args)


if __name__ == "__main__":
    main()
