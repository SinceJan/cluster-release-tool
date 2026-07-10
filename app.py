"""cluster 发版工具 — 网页后端

提供 API：
  GET  /api/deps              依赖库 .so 变更状态
  POST /api/release           先 git pull 再触发 build.sh + package.py（全局互斥）
  GET  /api/release/<task_id> 查询打包进度和日志
  POST /api/release/<id>/cancel  取消正在运行的打包
  GET  /api/packaging-status  全局打包状态（多用户同步）
  GET  /api/history           发版历史
  GET  /api/pull              手动触发 git pull
  GET  /api/branches          分支列表
  POST /api/checkout          切换分支
  GET  /api/download/<name>   下载部署包
  DELETE /api/delete/<name>   删除部署包
  POST /api/restart           重启服务（管理员）
  GET  /api/me                当前用户状态
  POST /api/login             登录
  POST /api/logout            登出
  GET  /api/users             用户列表（管理员）
  POST /api/users/create      创建用户（管理员）
  POST /api/users/delete      删除用户（管理员）
"""

import argparse
import json
import os
import select
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, make_response, render_template, request, send_file, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

_REPO_ROOT = None
_REPO_ROOT_LOGIC = None
_TEMPLATE_DIR = None
_BJT = timezone(timedelta(hours=8))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "cluster-release-tool-secret-2024")

USERS_FILE = Path(__file__).parent / "users.json"
DOWNLOAD_TOKEN_FILE = Path(__file__).parent / "download_token.txt"


def _ensure_download_token():
    """首次运行自动生成 32 位 hex token。"""
    if not DOWNLOAD_TOKEN_FILE.exists():
        DOWNLOAD_TOKEN_FILE.write_text(uuid.uuid4().hex, encoding="utf-8")
    return DOWNLOAD_TOKEN_FILE.read_text(encoding="utf-8").strip()


def _reset_download_token():
    """重新生成 token（管理员手动触发）。"""
    new_token = uuid.uuid4().hex
    DOWNLOAD_TOKEN_FILE.write_text(new_token, encoding="utf-8")
    return new_token


# ==================== 全局状态 ====================

_tasks = {}
_packaging_lock = threading.Lock()
_current_proc = None
_current_proc_lock = threading.Lock()
_MAX_TASKS = 20
_MAX_ARCHIVE_PACKAGES = 5


def get_repo_root():
    return _REPO_ROOT


def get_repo_root_logic():
    return _REPO_ROOT_LOGIC


def get_repo_root_for_project(project):
    if project == "logic" and _REPO_ROOT_LOGIC:
        return _REPO_ROOT_LOGIC
    return _REPO_ROOT


def get_output_dir():
    return _REPO_ROOT / "output"


def _prune_tasks():
    if len(_tasks) <= _MAX_TASKS:
        return
    excess = len(_tasks) - _MAX_TASKS
    for k in sorted(_tasks, key=lambda k: _tasks[k]["started_at"])[:excess]:
        del _tasks[k]


def _archive_dirs():
    """返回所有需要搜索的 archive 目录列表。"""
    dirs = [get_output_dir() / "archive"]
    if _REPO_ROOT_LOGIC:
        dirs.append(_REPO_ROOT_LOGIC / "output" / "archive")
    return dirs


def _prune_archive():
    for archive_dir in _archive_dirs():
        if not archive_dir.exists():
            continue
        zips = sorted(archive_dir.glob("cluster_*.zip"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old_zip in zips[_MAX_ARCHIVE_PACKAGES:]:
            old_zip.unlink()
            for ext in (".manifest.toml", ".builder"):
                f = old_zip.with_suffix(ext)
                if f.exists():
                    f.unlink()


# ==================== 用户管理 ====================

def _load_users():
    if not USERS_FILE.exists():
        return {}
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return data.get("users", data)
    except Exception:
        return {}


def _save_users(users):
    USERS_FILE.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ensure_default_admin():
    users = _load_users()
    if not users:
        users = {"admin": {"password_hash": generate_password_hash("admin"), "is_admin": True}}
        _save_users(users)


# ==================== 鉴权装饰器 ====================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return jsonify({"error": "请先登录"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return jsonify({"error": "请先登录"}), 401
        if not session.get("is_admin"):
            return jsonify({"error": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return decorated


# ==================== git 操作 ====================

def _git_pull(repo_root, log_lines):
    """git fetch + reset --hard origin/<branch>"""
    if not (repo_root / ".git").exists() and not subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(repo_root), capture_output=True
    ).returncode == 0:
        msg = f"错误: {repo_root} 不在 git 仓库内"
        log_lines.append(msg + "\n")
        return False, None

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True
    ).stdout.strip()
    if not branch or branch == "HEAD":
        log_lines.append("错误: 当前处于 detached HEAD 状态\n")
        return False, None

    old = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True
    ).stdout.strip()

    log_lines.append(f"$ git fetch origin  (当前: {branch} @ {old})\n")
    fetch = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(repo_root), capture_output=True, text=True
    )
    if fetch.returncode != 0:
        log_lines.append("错误: git fetch 失败\n")
        if fetch.stderr:
            log_lines.append(fetch.stderr)
        return False, None

    log_lines.append(f"$ git reset --hard origin/{branch}\n")
    reset = subprocess.run(
        ["git", "reset", "--hard", f"origin/{branch}"],
        cwd=str(repo_root), capture_output=True, text=True
    )
    log_lines.append(reset.stdout)
    if reset.returncode != 0:
        log_lines.append("错误: git reset 失败\n")
        if reset.stderr:
            log_lines.append(reset.stderr)
        return False, None

    new = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True
    ).stdout.strip()

    if old == new:
        log_lines.append(f"  已是最新 ({new})\n")
    else:
        log_lines.append(f"  更新: {old} -> {new}\n")
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{old}..{new}"],
            cwd=str(repo_root), capture_output=True, text=True
        ).stdout
        if diff.strip():
            log_lines.append("  变更文件:\n")
            for line in diff.strip().splitlines()[:10]:
                log_lines.append(f"    {line}\n")

    return True, new


# ==================== 认证路由 ====================

@app.route("/api/me")
def api_me():
    if "username" not in session:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "username": session["username"],
        "is_admin": session.get("is_admin", False),
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    users = _load_users()
    user = users.get(username)
    pw_hash = user.get("password_hash", "") if user else ""
    if not pw_hash:
        return jsonify({"error": "用户名或密码错误"}), 401
    try:
        pw_ok = check_password_hash(pw_hash, password)
    except Exception:
        pw_ok = False
    if not user or not pw_ok:
        return jsonify({"error": "用户名或密码错误"}), 401
    session["username"] = username
    session["is_admin"] = user.get("is_admin", False)
    return jsonify({"ok": True, "username": username, "is_admin": session["is_admin"]})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ==================== 用户管理路由（管理员） ====================

@app.route("/api/users", methods=["GET", "POST"])
@admin_required
def api_users():
    if request.method == "POST":
        # 创建用户（前端 POST /api/users body={username,password,is_admin}）
        data = request.get_json(silent=True) or {}
        username = data.get("username", "").strip()
        password = data.get("password", "")
        is_admin = bool(data.get("is_admin", False))
        if not username or not password:
            return jsonify({"error": "用户名和密码不能为空"}), 400
        users = _load_users()
        if username in users:
            return jsonify({"error": "用户已存在"}), 409
        users[username] = {
            "password_hash": generate_password_hash(password),
            "is_admin": is_admin,
            "created_at": datetime.now(tz=_BJT).strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_users(users)
        return jsonify({"ok": True, "message": f"用户 {username} 已创建"})
    # 列出用户
    users = _load_users()
    return jsonify({"users": [
        {
            "username": u,
            "is_admin": info.get("is_admin", False),
            "created_at": info.get("created_at", "-"),
        }
        for u, info in users.items()
    ]})


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
def api_users_delete(username):
    # 删除用户（前端 DELETE /api/users/<name>）
    username = username.strip()
    if not username:
        return jsonify({"error": "用户名不能为空"}), 400
    users = _load_users()
    if username not in users:
        return jsonify({"error": "用户不存在"}), 404
    if username == session.get("username"):
        return jsonify({"error": "不能删除自己"}), 400
    if users[username].get("is_admin"):
        admin_count = sum(1 for u in users.values() if u.get("is_admin"))
        if admin_count <= 1:
            return jsonify({"error": "不能删除最后一个管理员"}), 400
    del users[username]
    _save_users(users)
    return jsonify({"ok": True, "message": f"用户 {username} 已删除"})


# ==================== 分支路由 ====================

@app.route("/api/projects")
@login_required
def api_projects():
    return jsonify({
        "framework": True,
        "logic": bool(_REPO_ROOT_LOGIC),
    })

@app.route("/api/branches")
@login_required
def api_branches():
    project = request.args.get("project", "framework")
    repo_root = get_repo_root_for_project(project)
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True
    ).stdout.strip()
    remote_raw = subprocess.run(
        ["git", "branch", "-r"],
        cwd=str(repo_root), capture_output=True, text=True
    ).stdout
    branches = sorted(set(
        line.strip().split(" -> ")[0].replace("origin/", "").strip()
        for line in remote_raw.splitlines()
        if line.strip() and "HEAD" not in line and "->" not in line
    ))
    return jsonify({"current": current, "branches": branches})


@app.route("/api/checkout", methods=["POST"])
@login_required
def api_checkout():
    data = request.get_json(silent=True) or {}
    branch = data.get("branch", "").strip()
    project = data.get("project", "framework")
    if not branch:
        return jsonify({"error": "分支名不能为空"}), 400
    repo_root = get_repo_root_for_project(project)
    fetch = subprocess.run(["git", "fetch", "origin"],
                           cwd=str(repo_root), capture_output=True, text=True)
    if fetch.returncode != 0:
        return jsonify({"error": "git fetch 失败"}), 500
    checkout = subprocess.run(
        ["git", "checkout", branch],
        cwd=str(repo_root), capture_output=True, text=True
    )
    if checkout.returncode != 0:
        return jsonify({"error": checkout.stderr.strip() or "切换失败"}), 500
    return jsonify({"ok": True, "branch": branch})


# ==================== 打包发布 ====================

@app.route("/api/release", methods=["POST"])
@login_required
def api_release():
    data = request.get_json(silent=True) or {}
    config = data.get("config", "low")
    project = data.get("project", "framework")
    if config not in ("low", "high"):
        return jsonify({"error": "config 必须是 low 或 high"}), 400
    if project not in ("framework", "logic"):
        return jsonify({"error": "project 必须是 framework 或 logic"}), 400
    if project == "logic" and not _REPO_ROOT_LOGIC:
        return jsonify({"error": "cluster_logic 工程目录未配置，请联系管理员"}), 400
    if project == "logic" and not session.get("is_admin"):
        return jsonify({"error": "业务工程打包仅限管理员"}), 403

    running = next((t for t in _tasks.values() if t["status"] == "running"), None)
    if running:
        return jsonify({
            "error": "busy",
            "message": f"已有打包任务在运行（{running['config']}），请等它完成",
            "running_task_id": running["id"],
            "started_at": running["started_at"],
        }), 409

    if not _packaging_lock.acquire(blocking=False):
        return jsonify({"error": "busy", "message": "打包锁被占用"}), 409

    task_id = str(uuid.uuid4())[:8]
    builder = session.get("username", "unknown")
    _tasks[task_id] = {
        "id": task_id,
        "config": config,
        "project": project,
        "status": "running",
        "builder": builder,
        "started_at": datetime.now(tz=_BJT).strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "log": [],
        "zip_path": None,
        "exit_code": None,
        "proc": None,
    }

    thread = threading.Thread(
        target=_run_release,
        args=(task_id, config, builder, project),
        daemon=True,
    )
    try:
        thread.start()
    except Exception as e:
        _packaging_lock.release()
        del _tasks[task_id]
        return jsonify({"error": f"启动打包线程失败: {e}"}), 500
    _prune_tasks()

    return jsonify({"task_id": task_id})


def _read_proc_output_with_cancel(proc, task):
    """读取子进程输出，每秒检测取消标志。检测到取消时杀进程组。返回 True=被取消。"""
    fd = proc.stdout.fileno()
    while True:
        if task.get("_cancel_requested"):
            task["log"].append(f"\n[取消] 检测到取消，正在终止 pid={proc.pid}\n")
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, 9)
                task["log"].append(f"[取消] killpg(pgid={pgid}) OK\n")
            except Exception as e:
                task["log"].append(f"[取消] killpg 失败: {e}\n")
                try:
                    proc.kill()
                except Exception:
                    pass
            return True

        try:
            ready, _, _ = select.select([fd], [], [], 1.0)
        except (OSError, ValueError):
            ready = []

        if ready:
            line = proc.stdout.readline()
            if not line:
                break
            task["log"].append(line)

    return False


def _run_release(task_id, config, builder="unknown", project="framework"):
    """后台线程：git pull → build.sh → package.py。锁在 finally 释放。"""
    global _current_proc
    task = _tasks[task_id]
    repo_root = get_repo_root_for_project(project)
    is_logic = (project == "logic")

    try:
        # Step 1: git pull
        task["log"].append("=" * 42 + "\n")
        task["log"].append("[Step 1/3] git fetch + reset --hard 同步代码\n")
        task["log"].append("=" * 42 + "\n")
        pull_ok, new_commit = _git_pull(repo_root, task["log"])
        if task.get("_cancel_requested"):
            task["log"].append("\n[已取消] 用户取消了打包\n")
            task["exit_code"] = -1
            task["status"] = "cancelled"
            return
        if not pull_ok:
            task["log"].append("\n已中止打包（同步代码失败）\n")
            task["exit_code"] = -1
            task["status"] = "failed"
            return

        # Step 2: build.sh
        task["log"].append("\n" + "=" * 42 + "\n")
        task["log"].append("[Step 2/3] build.sh 编译\n")
        task["log"].append("=" * 42 + "\n")
        cmd = ["bash", str(repo_root / "build.sh"), config]
        task["log"].append(f"$ {' '.join(cmd)}\n")
        proc = subprocess.Popen(
            cmd, cwd=str(repo_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,
        )
        with _current_proc_lock:
            _current_proc = proc
        task["proc"] = proc
        cancelled = _read_proc_output_with_cancel(proc, task)
        proc.wait()
        with _current_proc_lock:
            _current_proc = None
        task["proc"] = None
        if cancelled or task.get("_cancel_requested"):
            task["log"].append("\n[已取消] 用户取消了打包\n")
            task["exit_code"] = -1
            task["status"] = "cancelled"
            return
        if proc.returncode != 0:
            task["exit_code"] = proc.returncode
            task["status"] = "failed"
            return

        # Step 3: package.py
        task["log"].append("\n" + "=" * 42 + "\n")
        task["log"].append("[Step 3/3] package.py 打包\n")
        task["log"].append("=" * 42 + "\n")
        package_script = Path(__file__).parent / "package.py"
        cmd = [sys.executable, str(package_script),
               "--config", config, "--repo-root", str(repo_root)]
        if is_logic:
            cmd.extend(["--package-config", "package_config_logic.toml"])
        if _TEMPLATE_DIR:
            cmd.extend(["--template-dir", str(_TEMPLATE_DIR)])
        task["log"].append(f"$ {' '.join(cmd)}\n")
        proc = subprocess.Popen(
            cmd, cwd=str(repo_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,
        )
        with _current_proc_lock:
            _current_proc = proc
        task["proc"] = proc
        cancelled = _read_proc_output_with_cancel(proc, task)
        proc.wait()
        with _current_proc_lock:
            _current_proc = None
        task["proc"] = None
        if cancelled or task.get("_cancel_requested"):
            task["log"].append("\n[已取消] 用户取消了打包\n")
            task["exit_code"] = -1
            task["status"] = "cancelled"
            return
        task["exit_code"] = proc.returncode
        task["status"] = "success" if proc.returncode == 0 else "failed"
    except Exception as e:
        task["log"].append(f"\n[ERROR] {e}\n")
        task["exit_code"] = -1
        task["status"] = "failed"
    finally:
        with _current_proc_lock:
            _current_proc = None
        task["finished_at"] = datetime.now(tz=_BJT).strftime("%Y-%m-%d %H:%M:%S")
        if not task.get("_cancel_requested"):
            try:
                cfg_num = "8675" if config == "low" else "8676"
                archive_dir = repo_root / "output" / "archive"
                zips = sorted(archive_dir.glob(f"cluster_{cfg_num}_*.zip"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
                if zips:
                    latest = zips[0]
                    task["zip_path"] = str(latest.relative_to(repo_root))
                    latest.with_suffix(".builder").write_text(builder, encoding="utf-8")
                    _prune_archive()
            except Exception as e:
                task["log"].append(f"\n[WARNING] 归档清理失败: {e}\n")
        _packaging_lock.release()


@app.route("/api/release/<task_id>")
@login_required
def api_release_status(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "id": task["id"],
        "config": task["config"],
        "project": task.get("project", "framework"),
        "status": task["status"],
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "exit_code": task["exit_code"],
        "zip_path": task["zip_path"],
        "log": "".join(task["log"]),
    })


@app.route("/api/packaging-status")
@login_required
def api_packaging_status():
    running = next((t for t in _tasks.values() if t["status"] == "running"), None)
    if running:
        return jsonify({
            "busy": True,
            "builder": running.get("builder", ""),
            "config": running["config"],
            "project": running.get("project", "framework"),
            "started_at": running["started_at"],
            "task_id": running["id"],
        })
    return jsonify({"busy": False})


@app.route("/api/release/<task_id>/cancel", methods=["POST"])
@login_required
def api_release_cancel(task_id):
    """取消正在运行的打包任务。

    编译进程（cmake/make）可能无法从外部强制终止，
    但取消标志会让 _run_release 的 select 循环在 1 秒内检测到并杀掉进程组。
    """
    global _current_proc
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] != "running":
        return jsonify({"error": "任务不在运行中", "status": task["status"]}), 409

    task["_cancel_requested"] = True
    task["log"].append("\n[取消] 已收到取消请求\n")

    # 尝试终止子进程（select 循环也会在 1 秒内检测到标志并杀进程组）
    with _current_proc_lock:
        proc = _current_proc
    if proc:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, 9)
            task["log"].append(f"[取消] killpg(pgid={pgid}) 已执行\n")
        except Exception:
            pass

    _cleanup_packaging_tmp()
    return jsonify({"ok": True, "message": "已请求取消"})


def _cleanup_packaging_tmp():
    """清理打包临时目录。"""
    import shutil
    pkg_cache = Path.home() / ".cache"
    if pkg_cache.exists():
        for d in pkg_cache.glob("cluster_pkg_*"):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


# ==================== 手动 git pull ====================

@app.route("/api/pull")
@login_required
def api_pull():
    project = request.args.get("project", "framework")
    repo_root = get_repo_root_for_project(project)
    log = []
    ok, commit = _git_pull(repo_root, log)
    return jsonify({"ok": ok, "commit": commit, "log": "".join(log)})


# ==================== 发版历史 ====================

def _parse_manifest(manifest_path):
    """解析 BUILD_MANIFEST.toml，返回 {build, files, summary, total_files}。"""
    if not manifest_path.exists():
        return None
    text = manifest_path.read_text(encoding="utf-8")
    build = {}
    files = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if line == "[build]":
            continue
        if line == "[[file]]":
            files.append({})
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"')
            if files:
                f = files[-1]
                if key == "path":
                    f["path"] = val
                elif key == "sha256":
                    f["sha256"] = val
                    f["sha_short"] = val[:12]
                elif key == "size":
                    try:
                        size = int(val)
                        f["size"] = size
                        if size > 1024 * 1024:
                            f["size_human"] = f"{size / 1024 / 1024:.1f}MB"
                        elif size > 1024:
                            f["size_human"] = f"{size / 1024:.0f}KB"
                        else:
                            f["size_human"] = f"{size}B"
                    except ValueError:
                        f["size"] = 0
                        f["size_human"] = "?"
                elif key == "category":
                    f["category"] = val
                elif key == "source":
                    f["source"] = val
                elif key == "desc":
                    f["desc"] = val
            else:
                if key in ("config", "git_branch", "git_commit", "git_dirty", "pack_time"):
                    build[key] = val
                elif key == "total_files":
                    try:
                        build["total_files"] = int(val)
                    except ValueError:
                        pass

    summary = {}
    for f in files:
        cat = f.get("category", "other")
        summary[cat] = summary.get(cat, 0) + 1

    return {"build": build, "files": files, "summary": summary, "total_files": len(files)}


@app.route("/api/history")
@login_required
def api_history():
    zips = []
    for ad in _archive_dirs():
        if ad.exists():
            zips.extend(ad.glob("cluster_*.zip"))
    zips = sorted(zips, key=lambda p: p.stat().st_mtime, reverse=True)
    history = []
    for z in zips:
        manifest = z.with_suffix(".manifest.toml")
        builder_file = z.with_suffix(".builder")
        builder = builder_file.read_text(encoding="utf-8").strip() if builder_file.exists() else ""
        name = z.name
        parts = name.replace(".zip", "").split("_")
        cfg_num = parts[1] if len(parts) > 1 else "?"
        commit = parts[2] if len(parts) > 2 else "?"
        dirty = parts[3] if len(parts) > 3 else "?"
        size_mb = z.stat().st_size / (1024 * 1024)
        mtime = datetime.fromtimestamp(z.stat().st_mtime, tz=_BJT).strftime("%Y-%m-%d %H:%M")
        entry = {
            "name": name,
            "cfg_num": cfg_num,
            "commit": commit,
            "dirty": dirty,
            "size_mb": round(size_mb, 1),
            "mtime": mtime,
            "builder": builder,
        }
        if manifest.exists():
            entry["manifest"] = _parse_manifest(manifest)
        history.append(entry)
    return jsonify({"history": history})


# ==================== 下载/删除 ====================

def _safe_archive_path(name):
    """安全拼接 archive 路径，防止路径穿越。返回 Path 或 None。"""
    for archive_dir in _archive_dirs():
        archive_dir = archive_dir.resolve()
        f = (archive_dir / name).resolve()
        try:
            f.relative_to(archive_dir)
        except ValueError:
            continue
        if f.exists():
            return f
    return None


@app.route("/api/download/<path:name>")
@login_required
def api_download(name):
    f = _safe_archive_path(name)
    if not f or not f.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(str(f), as_attachment=True, download_name=f.name)


@app.route("/api/delete/<path:name>", methods=["DELETE"])
@admin_required
def api_delete(name):
    f = _safe_archive_path(name)
    if not f or not f.exists():
        return jsonify({"error": "文件不存在"}), 404
    f.unlink()
    for ext in (".manifest.toml", ".builder"):
        sidecar = f.with_suffix(ext)
        if sidecar.exists():
            sidecar.unlink()
    return jsonify({"ok": True, "message": f"已删除 {name}"})


# ==================== 直链下载（免登录 token） ====================

@app.route("/latest")
def download_latest():
    """免登录直链下载最新包。

    用法：
      /latest?token=xxx               → 最新包（任意配置）
      /latest?token=xxx&config=8675   → 最新 8675 低配包
      /latest?token=xxx&config=8676   → 最新 8676 高配包
    """
    token = request.args.get("token", "").strip()
    expected = _ensure_download_token()
    if not token or token != expected:
        return jsonify({"error": "token 无效或缺失"}), 401

    def _all_zips():
        zips = []
        for ad in _archive_dirs():
            if ad.exists():
                zips.extend(ad.glob("cluster_*.zip"))
        return sorted(zips, key=lambda p: p.stat().st_mtime, reverse=True)

    zips = _all_zips()
    if not zips:
        return jsonify({"error": "暂无可用包"}), 404

    config_filter = request.args.get("config", "").strip()
    if config_filter:
        config_map = {"low": "8675", "high": "8676", "8675": "8675", "8676": "8676"}
        cfg_num = config_map.get(config_filter, config_filter)
        zips = [z for z in zips if f"_{cfg_num}_" in z.name]

    if not zips:
        return jsonify({"error": f"暂无可用包{f' (config={config_filter})' if config_filter else ''}"}), 404

    latest = zips[0]
    return send_file(str(latest), as_attachment=True, download_name=latest.name)


@app.route("/api/download-token", methods=["GET", "POST"])
@login_required
def api_download_token():
    """查看/重置直链下载 token。GET 所有用户可看，POST 仅管理员。"""
    if request.method == "POST":
        if not session.get("is_admin"):
            return jsonify({"error": "仅管理员可重置 token"}), 403
        new_token = _reset_download_token()
        return jsonify({"ok": True, "token": new_token})
    token = _ensure_download_token()
    return jsonify({"token": token})


# ==================== 依赖库状态 ====================

def _parse_manifest_lock(text):
    """解析 manifest.lock → {path: sha256}"""
    entries = {}
    cur = {}
    for line in text.splitlines():
        s = line.strip()
        if s == "[[entry]]":
            if cur.get("path"):
                entries[cur["path"]] = cur.get("sha256", "")
            cur = {}
        elif "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            cur[k.strip()] = v.strip().strip('"')
    if cur.get("path"):
        entries[cur["path"]] = cur.get("sha256", "")
    return entries


def _parse_manifest_toml(text):
    """解析 manifest.toml → {lib_name: {source, role, configs: [paths]}}"""
    import re
    libs = {}
    cur_lib = None
    in_configs = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and not s.startswith("[[") and s.endswith("]"):
            cur_lib = s[1:-1]
            libs[cur_lib] = {"configs": [], "source": "", "role": ""}
            in_configs = False
            continue
        if cur_lib is None:
            continue
        if s.startswith("configs") and "[" in s:
            in_configs = True
            if "]" in s:
                in_configs = False
                libs[cur_lib]["configs"].extend(re.findall(r'"([^"]+)"', s))
            continue
        if in_configs:
            if "]" in s:
                in_configs = False
            m = re.match(r'\s*"([^"]+)"', line)
            if m:
                libs[cur_lib]["configs"].append(m.group(1))
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"')
            if k in ("source", "role"):
                libs[cur_lib][k] = v
    return libs


def _sha256_file(path):
    """计算文件 SHA256"""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@app.route("/api/deps")
@login_required
def api_deps():
    repo_root = get_repo_root()
    manifest_lock = repo_root / "thirdparty" / "manifest.lock"
    manifest_toml = repo_root / "thirdparty" / "manifest.toml"

    if not manifest_lock.exists():
        return jsonify({"error": "manifest.lock 不存在", "entries": [],
                        "locked_count": 0, "disk_count": 0,
                        "unchanged_count": 0, "changed_count": 0,
                        "new_count": 0, "removed_count": 0})

    # 1. 解析基线 SHA
    lock_shas = _parse_manifest_lock(manifest_lock.read_text(encoding="utf-8"))

    # 2. 解析库元数据
    toml_libs = _parse_manifest_toml(
        manifest_toml.read_text(encoding="utf-8") if manifest_toml.exists() else "")

    # path → lib_name 映射
    path_to_lib = {}
    for lib_name, info in toml_libs.items():
        for p in info.get("configs", []):
            path_to_lib[p] = lib_name

    # 3. 收集所有涉及的路径（基线 + 配置声明）
    all_paths = set(lock_shas.keys()) | set(path_to_lib.keys())

    # 4. 逐个对比
    entries = []
    unchanged = changed = new_count = removed = 0

    for path in sorted(all_paths):
        lib_name = path_to_lib.get(path, path.split("/")[-1].replace(".so", "").replace("lib", "", 1))
        lib_info = toml_libs.get(lib_name, {})
        full_path = repo_root / path
        in_lock = path in lock_shas
        in_disk = full_path.exists()

        if in_lock and in_disk:
            old_sha = lock_shas[path]
            new_sha = _sha256_file(full_path)
            if old_sha == new_sha:
                status = "unchanged"
                unchanged += 1
            else:
                status = "changed"
                changed += 1
        elif in_disk and not in_lock:
            status = "new"
            new_count += 1
            old_sha = ""
            new_sha = _sha256_file(full_path)
        else:
            status = "removed"
            removed += 1
            old_sha = lock_shas.get(path, "")
            new_sha = ""

        entry = {
            "status": status,
            "lib": lib_name,
            "path": path,
            "source": lib_info.get("source", "-"),
            "role": lib_info.get("role", "-"),
        }
        if status == "changed":
            entry["old_sha_short"] = old_sha[:12]
            entry["new_sha_short"] = new_sha[:12]
        entries.append(entry)

    return jsonify({
        "locked_count": len(lock_shas),
        "disk_count": sum(1 for p in all_paths if (repo_root / p).exists()),
        "unchanged_count": unchanged,
        "changed_count": changed,
        "new_count": new_count,
        "removed_count": removed,
        "entries": entries,
    })


# ==================== 重启服务 ====================

@app.route("/api/restart", methods=["POST"])
@admin_required
def api_restart():
    import signal as _signal
    def _delayed_kill():
        time.sleep(1.5)
        os.kill(os.getpid(), _signal.SIGTERM)
    threading.Thread(target=_delayed_kill, daemon=True).start()
    return jsonify({"ok": True, "message": "服务正在重启，请等待 5 秒后刷新页面"})


# ==================== 首页 ====================

@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ==================== main ====================

def main():
    global _REPO_ROOT, _REPO_ROOT_LOGIC, _TEMPLATE_DIR
    parser = argparse.ArgumentParser(description="cluster 发版工具")
    parser.add_argument("--repo-root", default="/home/heyi/code/cluster_framework",
                        help="cluster_framework 工程根目录")
    parser.add_argument("--repo-root-logic", default="",
                        help="cluster_logic 工程根目录（默认 ../cluster_logic）")
    parser.add_argument("--template-dir", default="",
                        help="公用部署模板目录（默认 ../部署/新项目）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    _REPO_ROOT = Path(args.repo_root).resolve()
    if args.repo_root_logic:
        _REPO_ROOT_LOGIC = Path(args.repo_root_logic).resolve()
    else:
        _REPO_ROOT_LOGIC = _REPO_ROOT.parent / "cluster_logic"
    if not _REPO_ROOT_LOGIC.is_dir():
        _REPO_ROOT_LOGIC = None

    if args.template_dir:
        _TEMPLATE_DIR = Path(args.template_dir).resolve()
    else:
        _TEMPLATE_DIR = _REPO_ROOT.parent / "部署" / "新项目"
    if not _TEMPLATE_DIR.is_dir():
        _TEMPLATE_DIR = None

    _ensure_default_admin()

    print(f"repo_root         = {_REPO_ROOT}")
    print(f"repo_root_logic   = {_REPO_ROOT_LOGIC}")
    print(f"template_dir      = {_TEMPLATE_DIR}")
    print(f"listen            = {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
