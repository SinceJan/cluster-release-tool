#!/usr/bin/env python3
"""一键部署：通过 SCP 直接传文件到 VM，绕过共享文件夹缓存问题。

用法: python deploy.py
"""
import sys
import os
import paramiko
import time

VM_HOST = "192.168.0.38"
VM_USER = "heyi"
VM_PASS = "1127"
VM_DIR  = "/mnt/hgfs/code/cluster-release-tool"

# 要同步的文件（相对当前目录）
FILES = [
    "app.py",
    "package.py",
    "gen_manifest.py",
    "package_config.toml",
    "templates/index.html",
]

def run(ssh, cmd):
    """执行远程命令，返回 stdout"""
    _, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    return out, err

def main():
    local_dir = os.path.dirname(os.path.abspath(__file__))

    print(f"连接 {VM_HOST}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(VM_HOST, username=VM_USER, password=VM_PASS, timeout=10)
    sftp = ssh.open_sftp()

    # 1. 先传到 VM 本地 /tmp/（避开 hgfs 的 SFTP 兼容性问题）
    tmp_dir = "/tmp/release_deploy"
    run(ssh, f"mkdir -p {tmp_dir}/templates")
    print(f"传文件到 {tmp_dir}/ ...")

    for f in FILES:
        local = os.path.join(local_dir, f.replace("/", os.sep))
        remote = f"{tmp_dir}/{f}"
        if not os.path.exists(local):
            print(f"  跳过(本地不存在): {f}")
            continue
        # 确保远程子目录存在
        remote_dir = os.path.dirname(remote)
        run(ssh, f"mkdir -p {remote_dir}")
        sftp.put(local, remote)
        local_size = os.path.getsize(local)
        print(f"  {f}: {local_size} bytes -> {remote}")

    sftp.close()

    # 2. 用 cp 覆盖共享目录（cp 走内核 VFS，比 SFTP 更可靠）
    print(f"复制到 {VM_DIR}/ ...")
    out, err = run(ssh, f"cp -f {tmp_dir}/app.py {tmp_dir}/package.py {tmp_dir}/gen_manifest.py {tmp_dir}/package_config.toml {VM_DIR}/ 2>&1")
    out, err = run(ssh, f"cp -f {tmp_dir}/templates/index.html {VM_DIR}/templates/ 2>&1")

    # 3. 刷新内核页缓存（解决 hgfs 缓存不一致）
    run(ssh, "sync")
    print("已刷新内核缓存")

    # 4. 验证关键标记
    out, _ = run(ssh, f"grep -c 'v2' {VM_DIR}/templates/index.html")
    print(f"验证 v2 标记: {out.strip()} (应该=1)")

    # 5. 重启服务（sudo -S 从 stdin 读密码）
    print("重启 cluster-web 服务...")
    run(ssh, "echo '1127' | sudo -S systemctl restart cluster-web 2>&1")
    time.sleep(3)

    out, _ = run(ssh, "systemctl is-active cluster-web")
    print(f"服务状态: {out}")

    run(ssh, f"rm -rf {tmp_dir}")
    ssh.close()
    print("\n部署完成！刷新页面即可。")

if __name__ == "__main__":
    main()
