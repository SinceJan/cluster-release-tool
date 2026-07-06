# cluster 发版工具 — 部署指南

> 用途：按本文档可在一台全新的 Ubuntu 虚拟机上从零部署 cluster 发版工具。
> 维护者：小毅（郭晓）｜ 最后更新：2026-07-02

---

## 一、架构与依赖总览

发版工具的依赖分四类，**全部不依赖任何 Windows 临时目录**：

| 依赖 | 位置 | 获取方式 | 进 git？ |
|------|------|---------|---------|
| **发版工具**（app.py/index.html/requirements.txt/run_web.sh） | `/mnt/hgfs/code/cluster-release-tool/` | VMware 共享目录（Windows `E:\code\cluster-release-tool\`） | 否（后续单独建 git） |
| **打包模板**（HMI 大文件 cluster-hmi/data.zip/libcocos.so 等，约 960M） | `/mnt/hgfs/code/cluster_framework/prompt/部署/新项目/` | VMware 共享目录（Windows 工程目录内） | 否（大文件） |
| **代码工程**（build.sh/gen_manifest.py/源码/.so/dlt-daemon） | `/home/heyi/code/cluster_framework/`（VM 本地） | `git clone` gitlab 仓库 | 是 |
| **Flask 运行环境** | `/home/heyi/cluster_web_venv/`（VM 本地） | `pip install -r requirements.txt` | — |
| **交叉编译 toolchain**（低配/高配） | `/data/cross-tools/...`、`/opt/poky/5.0/...` | SDK 安装包（见 §6.2） | — |

### 数据流

```
Windows E:\code\                              VM /mnt/hgfs/code/  (VMware 共享目录, 实时同步)
├── cluster_framework\      ←─共享─→         ├── cluster_framework/   (代码工程源 + 打包模板 prompt/部署/新项目/)
└── cluster-release-tool\   ←─共享─→         └── cluster-release-tool/ (发版工具源: app.py + templates/index.html)
                                                       │
                                                       │ systemd ExecStart 从此加载
                                                       ▼
                                       /home/heyi/cluster_web_venv/bin/python (Flask 运行)
                                                       │
                                       /home/heyi/code/cluster_framework/ (git clone, 编译+打包用)
                                                       │
                                                       ▼
                                       output/archive/cluster_867X_<commit>.zip (+ .manifest.toml)
```

### 关键纪律

1. **发版工具的源在共享目录**（`/mnt/hgfs/code/cluster-release-tool/`），不在 VM home，也不在 Windows 临时目录。Windows 改 `E:\code\cluster-release-tool\` 下文件 → 共享同步 → `systemctl restart cluster-web` 生效。
2. **打包模板直接读共享目录**（`PACKAGING_TEMPLATE_DIR=/mnt/hgfs/code/cluster_framework/prompt/部署/新项目`），VM 不维护副本（曾因副本与源不一致出 bug，见 §6.1）。
3. **代码工程走 git**（`/home/heyi/code/cluster_framework` 是 git clone 副本，不用共享目录编译——共享目录所有权/性能不适合编译，且 git 受控）。

---

## 二、前置条件

| 项 | 要求 |
|----|------|
| 操作系统 | Ubuntu 22.04 LTS（x86_64） |
| VMware | 已配置共享文件夹：Windows `E:\code` → VM `/mnt/hgfs/code`（vmhgfs-fuse 挂载） |
| 网络 | 能访问 `gitlab.chukong-inc.com`（clone 代码工程）+ 局域网访问端口 8080 |
| 账号 | gitlab 凭证（http clone 用） |
| toolchain | 低配 `/data/cross-tools/oecore-x86_64/`、高配 `/opt/poky/5.0/`（见 §6.2） |

> 共享目录是核心前提：发版工具源和打包模板都依赖 `/mnt/hgfs/code` 实时同步。Windows host 需开机且共享文件夹已启用。

---

## 三、部署步骤

### 3.1 系统包

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
```

### 3.2 确认共享目录挂载

```bash
# 应输出 vmhgfs-fuse 挂载行
mount | grep hgfs

# 应能看到发版工具和代码工程
ls /mnt/hgfs/code/cluster-release-tool/app.py
ls /mnt/hgfs/code/cluster_framework/prompt/部署/新项目/cluster_8675
```

若未挂载，在 VMware 客户机设置 → 选项 → 共享文件夹 → 启用，添加 Windows `E:\code`。Ubuntu 自动挂载到 `/mnt/hgfs/<name>`。

### 3.3 克隆代码工程（VM 本地，用于编译）

```bash
cd /home/heyi/code    # 或任意工作目录
git clone http://gitlab.chukong-inc.com/car-hmi/cluster_framework.git
cd cluster_framework
git checkout heyi/dev
```

> 代码工程用 git clone 副本（非共享目录），原因：共享目录是 Windows 的 working tree，VM 无法在其上跑 git；且编译产物在 `output/`（gitignore），共享目录不适合频繁读写。

### 3.4 创建 Python venv 并安装依赖

```bash
python3 -m venv /home/heyi/cluster_web_venv
/home/heyi/cluster_web_venv/bin/pip install -r /mnt/hgfs/code/cluster-release-tool/requirements.txt
```

`requirements.txt` 内容仅 `flask>=3.0`（gen_manifest 的 TOML 解析零依赖，无需 tomli）。

验证：

```bash
/home/heyi/cluster_web_venv/bin/python -c "import flask; print(flask.__version__)"
```

### 3.5 配置 systemd 服务

创建 `/etc/systemd/system/cluster-web.service`：

```ini
[Unit]
Description=Cluster Release Web Tool
After=network.target
Requires=network.target

[Service]
Type=simple
User=heyi
Group=heyi
WorkingDirectory=/mnt/hgfs/code/cluster-release-tool
Environment=PYTHON=python3
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=CLUSTER_REPO_ROOT=/home/heyi/code/cluster_framework
Environment=PACKAGING_TEMPLATE_DIR=/mnt/hgfs/code/cluster_framework/prompt/部署/新项目
ExecStart=/home/heyi/cluster_web_venv/bin/python /mnt/hgfs/code/cluster-release-tool/app.py --repo-root /home/heyi/code/cluster_framework --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3
KillMode=mixed

[Install]
WantedBy=multi-user.target
```

> **路径说明**（按实际部署调整）：
> - `User` / `Group`：运行账号
> - `--repo-root`：§3.3 克隆的代码工程路径
> - `PACKAGING_TEMPLATE_DIR`：指向共享目录的打包模板（含 `cluster_8675/`、`cluster_8676/` 子目录）
> - `PYTHONDONTWRITEBYTECODE=1`：避免 `.pyc` 写入共享目录污染 Windows 侧

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cluster-web
sudo systemctl status cluster-web    # 应 active (running)
```

### 3.6 验证

```bash
# 服务状态
systemctl is-active cluster-web    # active
systemctl is-enabled cluster-web   # enabled

# API 可用性
curl -s http://localhost:8080/api/deps | python3 -m json.tool | head -5
curl -s http://localhost:8080/api/package-files | python3 -m json.tool | head -5

# 浏览器访问
# http://<VM_IP>:8080
```

---

## 四、当前已验证环境快照（参考）

| 项 | 值 |
|----|-----|
| VM IP | 192.168.0.38 |
| 系统 | Ubuntu 22.04.5 LTS (x86_64) |
| Python | 3.10.12（系统自带） |
| 共享目录 | `/mnt/hgfs/code` (vmhgfs-fuse, rw) |
| 代码工程 | `/home/heyi/code/cluster_framework` (git, branch `heyi/dev`) |
| 发版工具源 | `/mnt/hgfs/code/cluster-release-tool/` |
| 打包模板 | `/mnt/hgfs/code/cluster_framework/prompt/部署/新项目/` |
| venv | `/home/heyi/cluster_web_venv/` |
| 服务 | `cluster-web` (port 8080) |
| toolchain 低配 | `/data/cross-tools/oecore-x86_64/environment-setup-aarch64-poky-linux` |
| toolchain 高配 | `/opt/poky/5.0/environment-setup-aarch64-poky-linux` |

---

## 五、日常运维

### 5.1 修改发版工具（app.py / index.html）

发版工具源在共享目录，Windows 端直接改 `E:\code\cluster-release-tool\` 下文件 → 共享目录自动同步 → 重启服务生效：

```bash
# VM 上重启（Windows 改完后）
sudo systemctl restart cluster-web
```

> app.py 启动时从 `templates/` 子目录加载 `index.html`（Flask `template_folder`），所以 index.html 必须在 `cluster-release-tool/templates/index.html`，不要放根目录。

### 5.2 修改代码工程（build.sh / gen_manifest.py / manifest）

走标准 git 流程：Windows `git push` → VM `git pull`。网页"编译打包"按钮会自动先 `git pull` 再打包。

### 5.3 触发打包

- 网页：访问 `http://<VM_IP>:8080`，选 8675/8676，点"编译并打包"
- API：`curl -X POST -H 'Content-Type: application/json' -d '{"config":"low"}' http://localhost:8080/api/release`

产物：`/home/heyi/code/cluster_framework/output/archive/cluster_<cfg>_<commit>_<dirty>.zip`（+ 同名 `.manifest.toml`）。历史归档不被打包清空。

### 5.4 看日志

```bash
sudo journalctl -u cluster-web -f
```

---

## 六、注意事项

### 6.1 打包模板的 libcocos.so 命名（重要）

打包模板 `cluster_8675/lib/` 下的 HMI 引擎库**必须命名为 `libcocos.so`**（不能是 `libcocos.so1` 或其他）。

原因：`build.sh` 打包时清理旧 .so，按 `libcocos.so|libcocos.so.*` 模式跳过保留引擎库。若命名为 `libcocos.so1`，不匹配该模式，会被**误删**，导致最终包缺 HMI 引擎库。

> 历史：共享目录曾存在 `libcocos.so1`（拼写错误），已于 2026-07-02 修正为 `libcocos.so`。若发现模板里再次出现错误命名，参考 build.sh 的清理逻辑（`case libcocos.so|libcocos.so.*`）。

### 6.2 交叉编译 toolchain 来源

低配（8675）和高配（8676）的 toolchain 是厂商提供的 SDK 安装包，需手动安装到：

- 低配：`/data/cross-tools/oecore-x86_64/`（environment-setup 脚本在此）
- 高配：`/opt/poky/5.0/`

> SDK 安装包来源：联系芯片/平台供应商获取。新 VM 部署时需从旧 VM 或原始安装介质拷贝这两个目录。

### 6.3 共享目录依赖

发版工具运行依赖共享目录 `/mnt/hgfs/code` 已挂载。若 Windows host 未开机或共享文件夹未启用，`cluster-web` 服务会启动失败（找不到 app.py）。systemd 的 `Restart=always` 会持续重试，共享目录恢复后自动起来。

### 6.4 后续：发版工具纳入 git

当前发版工具（`/mnt/hgfs/code/cluster-release-tool/`）未进 git，依赖共享目录同步、无版本历史。**待发版流程稳定后**，建议为其建立独立 git 仓库（不进中间件工程，保持解耦），届时本 §3 的"发版工具从共享目录获取"可改为"git clone 独立仓库"。

---

## 七、快速排查

| 现象 | 排查 |
|------|------|
| 服务起不来 / 反复重启 | `journalctl -u cluster-web -n 30`；检查共享目录是否挂载、venv 是否存在、app.py 路径 |
| 打包后包里缺 libcocos.so | 检查模板 `cluster_8675/lib/libcocos.so` 命名（见 §6.1）|
| 打包失败 "toolchain not found" | 检查 `/data/cross-tools/...` 或 `/opt/poky/5.0/...` 是否安装（见 §6.2）|
| `/api/deps` 500 | gen_manifest 解析 manifest.toml 失败，检查 `thirdparty/manifest.toml` 语法 |
| 打包时 git pull 失败 | VM 代码工程工作区有未提交改动，按网页日志提示 `git checkout` 或 `git stash` |

---

> 文档结束。将此文档 + 共享目录（`E:\code\cluster-release-tool\` + `E:\code\cluster_framework\`）提供给新 VM，即可完成部署。
