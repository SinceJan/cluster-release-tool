# cluster_framework 发版系统完整说明文档

> **用途**：将此文档完整粘贴给新会话，即可获得发版系统的全部上下文，无需重新探索代码。
> **最后更新**：2026-07-03
> **维护者**：小毅（郭晓）

---

## 一、系统概述

cluster_framework 是车载仪表系统的 C++ 中间件工程。发版系统负责将编译产物 + 依赖库 + HMI 资源 + 配置脚本打包成可部署的 zip，并通过网页工具一键操作。

### 支持的硬件配置

| 配置 | 代号 | 芯片 | 工具链 | DDS |
|------|------|------|--------|-----|
| 低配 | 8675 | aarch64 | `/data/cross-tools/oecore-x86_64/` | 不启用 |
| 高配 | 8676 | aarch64 | `/opt/poky/5.0/` | 启用（长安 DDS + Cyclone DDS） |

### 发版系统能力

- **编译与打包解耦**：build.sh 纯编译，package.py 纯打包，app.py 编排
- **依赖库版本追踪**：71 个 .so 的 sha256 指纹基线，`git diff manifest.lock` 看变更
- **.so 增量缓存**：sha256 缓存 + 硬链接（VM 本地 ext4），二次打包 .so 拷贝瞬间完成
- **.so 去重**：相同内容的 .so 转为符号链接（zip 体积减少 ~50%）
- **一键编译打包**：网页点按钮，自动同步代码 → 编译 → 打包 zip
- **多用户认证**：登录/登出/用户管理（管理员可增删用户）
- **多用户排队**：打包时所有用户按钮实时置灰，打包完成自动恢复
- **分支切换**：网页切换 git 分支（fetch + reset --hard，VM 无本地状态）
- **网页操作台**：部署包内容清单 + 依赖变更追踪 + 编译打包 + 发版历史
- **发版历史**：保留最近 5 个包自动轮转，显示打包人，支持下载/删除
- **北京时间显示**：所有时间均为北京时间 (UTC+8)
- **服务重启按钮**：管理员可在网页上重启 Flask 服务（不用 SSH 进 VM）

---

## 二、架构设计

### 三层解耦

```
build.sh          纯编译：cmake + make → output/target/usr/bin/cluster
                  （代码工程仓库内，进 git）

package.py        纯打包：编译产物 + 模板 + .so → zip
                  （发版工具目录内，不进中间件 git）

app.py            编排层 + Web 服务：同步代码 → 调 build.sh → 调 package.py
                  （发版工具目录内，不进中间件 git）
```

### 依赖分层

```
┌─────────────────────────────────────────────────────────────┐
│  VMware 共享目录（Windows E:\code ↔ VM /mnt/hgfs/code）     │
│                                                             │
│  E:\code\cluster-release-tool\     ← 发版工具源（不进git）   │
│  ├── app.py                        ← Flask 后端 + 编排       │
│  ├── package.py                    ← 打包工具（独立可运行）   │
│  ├── requirements.txt              ← pip 依赖声明             │
│  ├── run_web.sh                    ← 手动启动辅助脚本          │
│  ├── users.json                    ← 用户数据（Werkzeug 哈希） │
│  ├── DEPLOY.md                     ← 部署指南                 │
│  ├── RELEASE_SYSTEM_GUIDE.md       ← 本说明文档               │
│  └── templates/index.html          ← 前端单页                 │
│                                                             │
│  E:\code\cluster_framework\        ← 代码工程（进 git）       │
│  ├── build.sh                      ← 纯编译（不含打包）        │
│  ├── tools/gen_manifest.py         ← 依赖指纹 + 清单生成      │
│  ├── thirdparty/manifest.lock      ← .so 指纹基线（自动）     │
│  ├── thirdparty/manifest.toml      ← 依赖元数据（人工维护）    │
│  └── prompt/部署/新项目/           ← 打包模板（不进git,960M）  │
├─────────────────────────────────────────────────────────────┤
│  VM 本地 ext4                                                │
│  /home/heyi/code/cluster_framework/  ← 代码工程（git clone）  │
│  /home/heyi/cluster_web_venv/        ← Flask venv             │
│  /home/heyi/.cache/cluster_so_cache/ ← .so 增量缓存（持久）   │
│  output/archive/                     ← 发版归档（最近5个）     │
│    ├── cluster_867X_<commit>_<dirty>_<timestamp>.zip          │
│    ├── cluster_..._.manifest.toml    ← 每个包配套清单          │
│    └── cluster_..._.builder          ← 打包人（文本文件）      │
└─────────────────────────────────────────────────────────────┘
```

### 关键纪律

1. **VM 是纯编译服务器**：本地代码永远跟远程同步，`git fetch + reset --hard`，无本地状态
2. **发版工具在共享目录**：Windows 改 → 共享同步 → 网页点"重启服务"生效
3. **打包模板在共享目录**：`PACKAGING_TEMPLATE_DIR` 环境变量指向，不维护 VM 副本
4. **编译与打包解耦**：build.sh 只编译，package.py 只打包，互不依赖
5. **发版包自动轮转**：archive 保留最近 5 个，超出自动删除最旧的

---

## 三、核心组件详解

### 3.1 build.sh — 纯编译

**位置**：工程根目录（代码工程内，进 git）
**职责**：cmake 配置 + 编译 → 部署 cluster 二进制到 `output/target/usr/bin/cluster`

```bash
./build.sh                  # x86_64 模拟编译
./build.sh low              # 编译 8675 低配
./build.sh high             # 编译 8676 高配
```

不包含任何打包逻辑。打包由 `package.py` 独立完成。

### 3.2 package.py — 打包工具

**位置**：发版工具目录（共享目录内，不进中间件 git）
**职责**：从编译产物 + 依赖库 + 模板 → 组装部署包 zip

```bash
python package.py --config low --repo-root /path/to/cluster_framework
```

**打包流程（5 步）**：

```
Step 1: 组装目录 — 拷模板 + cluster 二进制 + dlt-daemon 到 PKG_DIR
        PKG_DIR 在 VM 本地 ext4（$HOME/.cache/cluster_pkg_<pid>/）
Step 2: 拷 .so — sha256 缓存 + 硬链接增量拷贝 + 低配排除 libz
        缓存目录: $HOME/.cache/cluster_so_cache/（持久化，跨 build 复用）
Step 3: .so 去重 — sha256 相同的文件保留实体，其余转符号链接
        （源库 .so / .so.N / .so.N.M.P 是三个独立实体但内容相同，不去重 zip 暴增 3 倍）
Step 4: 生成 BUILD_MANIFEST — 调 gen_manifest.py --release
Step 5: 打 zip — Python zipfile，保留符号链接（external_attr = S_IFLNK）
        → 归档到 output/archive/，写 .builder（打包人），5 个包轮转
        → 清理临时 PKG_DIR
```

**关键设计**：
- PKG_DIR 和 SO_CACHE 都在 `$HOME/.cache/`（ext4），硬链接可生效
- .so 去重按文件名长度降序处理（最长 = 最具体版本 = 保留为实体）
- 模板路径优先级：`--template-dir` > `PACKAGING_TEMPLATE_DIR` 环境变量 > `<repo-root>/prompt/部署/新项目`

### 3.3 gen_manifest.py — 依赖指纹工具

**位置**：`tools/gen_manifest.py`（代码工程内，进 git）
**特性**：纯 Python 标准库，零外部依赖（内置极简 TOML parser）

```bash
python tools/gen_manifest.py              # 扫描 .so，生成/更新 manifest.lock
python tools/gen_manifest.py --check      # 对比磁盘与基线，报告变更
python tools/gen_manifest.py --toml       # 打印 manifest.toml 骨架
python tools/gen_manifest.py --release <pkg_dir> --config low|high
```

package.py 调用 `--release` 生成 BUILD_MANIFEST.toml，记录每个文件的 sha256/size/category/source/desc。

### 3.4 app.py — Flask 后端 + 编排

**位置**：`/mnt/hgfs/code/cluster-release-tool/app.py`（共享目录）
**运行**：Flask `threaded=True`，systemd 托管

#### 打包流程（3 步）

```
[Step 1/3] git fetch + reset --hard origin/<branch>
           VM 是纯编译服务器，强制同步远程，消除分叉问题
[Step 2/3] bash build.sh <config>
           纯编译，输出到 output/target/usr/bin/cluster
[Step 3/3] python package.py --config <config> --repo-root <repo>
           打包，输出到 output/archive/
```

#### API 清单

| API | 方法 | 功能 |
|-----|------|------|
| `/` | GET | 返回 index.html |
| `/api/login` | POST | 登录（用户名+密码） |
| `/api/logout` | POST | 登出 |
| `/api/me` | GET | 当前登录状态 |
| `/api/deps` | GET | 依赖库 .so 变更状态 |
| `/api/package-files` | GET | 最新部署包文件清单 |
| `/api/release` | POST | 触发打包（3 步：同步→编译→打包） |
| `/api/release/<id>` | GET | 轮询打包进度和实时日志 |
| `/api/packaging-status` | GET | 全局打包状态（多用户实时同步按钮） |
| `/api/branches` | GET | 列出本地+远程 git 分支 |
| `/api/checkout` | POST | 切换分支（fetch + checkout + reset --hard） |
| `/api/history` | GET | 发版历史（含打包人 builder 字段） |
| `/api/download/<name>` | GET | 下载指定 zip |
| `/api/delete/<name>` | DELETE | 删除 zip + manifest + builder |
| `/api/pull` | POST | 手动触发 git 同步 |
| `/api/restart` | POST | 重启 Flask 服务（仅管理员） |
| `/api/users` | GET/POST/DELETE | 用户管理 CRUD（仅管理员） |

#### 关键设计

- **零外部 TOML 依赖**：复用 `gen_manifest._mini_toml_loads()`
- **_tasks 内存泄漏防护**：最多保留 20 条任务记录
- **北京时间**：所有显示时间用 `timezone(timedelta(hours=8))`
- **打包互斥**：`threading.Lock` 保证同时只一个打包任务
- **git fetch + reset --hard**：取代 `pull --ff-only`，消除分叉问题
- **服务重启**：API 返回后延迟自杀，systemd `Restart=always` 自动拉起

### 3.5 index.html — 前端

**位置**：`/mnt/hgfs/code/cluster-release-tool/templates/index.html`

#### 页面功能

| 区域 | 功能 |
|------|------|
| **登录页** | 用户名/密码登录 |
| **编译打包** | 8675/8676 切换 + 分支切换 + "编译并打包"按钮 + 实时日志 |
| **发版历史** | 历史包列表（zip名/commit/大小/北京时间/打包人），下载+删除，点击展开 manifest |
| **部署包内容清单** | 最新包的完整文件表格 + 分类摘要 |
| **依赖库状态** | .so 变更状态（未变更/已变更/新增/删除） |
| **用户管理** | 管理员弹窗：增删用户、设置管理员标志 |
| **重启服务** | 管理员按钮：重启 Flask 服务（不用 SSH） |

#### 多用户实时同步

- `busyTimer` 每 2 秒轮询 `/api/packaging-status`
- 有人打包时：所有用户按钮置灰 + 状态栏显示"xxx 正在打包"
- 打包完成：所有用户按钮自动恢复

---

## 四、产物目录结构

### output/ 布局

```
output/
├── archive/                              ← 发版历史归档（保留最近 5 个）
│   ├── cluster_8675_<commit>_<dirty>_<timestamp>.zip
│   ├── cluster_...<timestamp>.manifest.toml   ← 每个包配套清单
│   └── cluster_...<timestamp>.builder         ← 打包人（文本文件）
└── target/                               ← 编译产物（每次 build 清空重建）
    └── usr/bin/cluster
```

> 打包中间目录 `PKG_DIR` 在 `$HOME/.cache/cluster_pkg_<pid>/`（VM 本地 ext4），
> 打包完成后自动清理。不再使用 `output/release/`。

### zip 文件名格式

```
cluster_<配置>_<commit>_<dirty>_<时间戳>.zip
例: cluster_8675_76722cb_clean_20260703_150022.zip
```

时间戳保证同一 commit 连续打包不覆盖。

### .so 缓存

```
$HOME/.cache/cluster_so_cache/           ← sha256 缓存（持久化，跨 build 复用）
├── <sha256>.so                          ← 缓存实体文件
└── ...
```

首次打包全量 cp + 写缓存，后续打包命中缓存 → 硬链接（毫秒级）。

---

## 五、VM 部署环境

### 基本信息

| 项 | 值 |
|----|-----|
| IP | 192.168.0.38 |
| 系统 | Ubuntu 22.04.5 LTS (x86_64) |
| 用户/密码 | heyi / 1127 |
| Python | 3.10.12 |
| Flask venv | `/home/heyi/cluster_web_venv/` |
| 共享目录 | `/mnt/hgfs/code` (= Windows `E:\code`，vmhgfs-fuse) |

### systemd 服务

**服务名**：`cluster-web`，active + enabled

```ini
[Unit]
Description=Cluster Release Web Tool
After=network.target
Requires=network.target

[Service]
Type=simple
User=heyi / Group=heyi
WorkingDirectory=/mnt/hgfs/code/cluster-release-tool
Environment=PYTHON=python3
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=CLUSTER_REPO_ROOT=/home/heyi/code/cluster_framework
Environment=PACKAGING_TEMPLATE_DIR=/mnt/hgfs/code/cluster_framework/prompt/部署/新项目
ExecStart=/home/heyi/cluster_web_venv/bin/python /mnt/hgfs/code/cluster-release-tool/app.py \
          --repo-root /home/heyi/code/cluster_framework --host 0.0.0.0 --port 8080
Restart=always / RestartSec=3 / KillMode=mixed
```

### 访问

- 网页：`http://192.168.0.38:8080`
- 默认管理员：xiaoyi / 1127

---

## 六、运维操作规范

### 6.1 发版工具修改（app.py / package.py / index.html）

发版工具源在共享目录，Windows 改 → 共享同步 → **网页点"重启服务"按钮**生效。

不再需要 SSH 进 VM（管理员右上角"重启服务"按钮 → 进程自杀 → systemd 自动拉起）。

### 6.2 代码工程修改（build.sh / gen_manifest.py / manifest）

走 git 流程：

```
Windows: git commit + git push
VM: 网页"编译并打包"时自动 git fetch + reset --hard（强制同步远程）
```

VM 也可通过网页"切换分支"按钮更新代码（fetch + checkout + reset --hard）。

### 6.3 打包模板更新

模板在共享目录 `E:\code\cluster_framework\prompt\部署/新项目\`，Windows 更新 → 同步 → 生效。

> **重要**：8675 模板的 HMI 引擎库必须命名为 `libcocos.so`（不能是 `libcocos.so1`），
> 否则 package.py 清理旧 .so 时会误删。

### 6.4 常用诊断

```bash
# VM 服务（也可在网页管理员按钮重启）
sudo systemctl restart cluster-web
sudo systemctl status cluster-web
journalctl -u cluster-web -f

# .so 缓存
ls -la /home/heyi/.cache/cluster_so_cache/ | wc -l    # 缓存条目数

# 历史归档一览
ls -la /home/heyi/code/cluster_framework/output/archive/
```

---

## 七、设计决策记录

### 7.1 为什么编译和打包解耦？

build.sh 曾同时负责编译+打包（493行），职责混乱、难以维护。拆分后：
- build.sh 纯编译（~180行），只产出 cluster 二进制
- package.py 纯打包（~250行），Python 比 bash 更适合文件操作
- 两者可独立运行：`./build.sh low` 或 `python package.py --config low`

### 7.2 为什么 git fetch + reset --hard 而非 pull --ff-only？

VM 是纯编译服务器，本地不该有任何独立状态。`pull --ff-only` 在 rebase/squash 后会分叉报错。
`fetch + reset --hard` 强制同步远程，彻底消除分叉问题。

### 7.3 为什么 .so 缓存在 $HOME/.cache/ 而非 output/？

VM 的代码工程在 hgfs 共享文件夹上，hgfs 不支持硬链接。
`$HOME/.cache/` 是 ext4 本地磁盘，硬链接可生效。PKG_DIR 也放此处，保证 `cp -l` 跨缓存→打包目录可用。

### 7.4 为什么 .so 要去重为符号链接？

源库里 `.so` / `.so.30` / `.so.30.0.4` 是三个独立实体（git 100644），但内容完全相同。
不去重的话 zip 里存三份 49MB，去重后一份实体 + 两个符号链接（几十字节），zip 体积减少 ~50%。

### 7.5 为什么 git 同步在 app.py 而非 build.sh？

bash 自更新悖论：build.sh git pull 自己 → 内存里跑旧版 → 看到"已 pull"但执行旧逻辑。
app.py 先同步再调 build.sh，build.sh 启动时磁盘上必然最新。

### 7.6 为什么发版工具不进中间件 git？

发版系统和中间件是不同关注点，应解耦。发版工具暂放共享目录，后续建立独立 git 仓库。

---

## 八、已知待办与优化方向

### 已完成（✓）

- ✓ 编译与打包解耦（build.sh + package.py）
- ✓ .so 增量缓存（sha256 + 硬链接）
- ✓ .so 去重（符号链接）
- ✓ 多用户认证 + 用户管理
- ✓ 多用户打包排队（实时按钮置灰）
- ✓ 分支切换（fetch + reset --hard）
- ✓ 发版历史显示打包人
- ✓ 5 个包自动轮转
- ✓ 服务重启按钮（不用 SSH）
- ✓ 北京时间显示
- ✓ zip 文件名加时间戳（防覆盖）

### 后续方向

1. **发版工具独立 git**：当前在共享目录，可建独立 git 仓库
2. **版本对比**：发版历史两包间文件级 diff
3. **通知**：打包完成后通知相关人员
4. **HMI 产物自动获取**：从 CI 自动拉取，代替手动放模板

---

> **文档结束**。另参见 `DEPLOY.md`（新 VM 部署指南）。
