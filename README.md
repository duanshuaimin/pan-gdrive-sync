# Pan-GDrive-Sync (百度网盘 ⇄ Google Drive 跨云同步与互传工具)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.8+](https://img.shields.io/badge/Python-3.8+-green.svg)](https://www.python.org/)

**Pan-GDrive-Sync** 是一个现代、高效、高可靠的跨云存储数据同步与互传命令行工具，专为解决**百度网盘（Baidu Netdisk）**与 **Google Drive** 之间的数据无缝迁移与日常双向同步需求而设计。

---

## 🌟 核心特性 (Key Features)

- 🔄 **双向跨云互传 (Bidirectional Transfer)**：
  - 百度网盘 ➔ Google Drive：`pan-gdrive-sync copy baidu:/文件 gdrive:/目标/`
  - Google Drive ➔ 百度网盘：`pan-gdrive-sync copy gdrive:/文件 baidu:/目标/`
- ⚡ **零磁盘积压流式管道 (Zero-Disk Streaming Pipeline)**：
  - 默认采用内存流式管道直接转发（边下载边上传），**不占用本地磁盘空间**，特别适合 VPS、云服务器或磁盘空间有限的设备。
  - 同时支持 `--disk-cache` 本地临时缓存模式，适应网络不稳定场景。
- 📁 **整目录递归同步 (Recursive Directory Sync)**：
  - 一键同步整个文件夹，自动保持多级目录结构与相对路径。
  - 支持冲突策略：`--overwrite`（覆盖已有文件）、`--skip`（跳过已存在同名同大小文件）。
- 📊 **Rich 现代化终端可视化看板**：
  - 实时显示跨云传输速率（MB/s）、已传输字节、百分比与动态 ETA 倒计时。
  - 双端存储配额（Used / Total / Free）侧重对比与状态概览看板。
- 🔐 **开箱即用与多认证支持**：
  - **百度网盘**：自动检测并无缝迁移本地 `baidupan` 与 `BaiduPCS-Go` 的登录凭证（`BDUSS`），免去重复登录步骤。
  - **Google Drive**：同时支持 **Google Cloud 服务账号 (Service Account JSON 密钥)** 与 **标准 OAuth2 Access/Refresh Token** 授权。

---

## 🏗️ 架构原理 (Architecture)

```
┌─────────────────────────┐          Streaming Pipe          ┌─────────────────────────┐
│  Baidu Netdisk (PCS API)│ ◄──────────────────────────────► │  Google Drive (v3 API)  │
│  - HTTP Chunked GET/PUT │         (In-Memory Stream)       │  - Resumable Upload     │
│  - BDUSS / Cookie Auth  │                                  │  - Service Account/OAuth│
└─────────────────────────┘                                  └─────────────────────────┘
             ▲                                                            ▲
             │                                                            │
             └───────────────── Transfer Engine (Progress) ───────────────┘
                                           │
                                  CLI (Click + Rich UI)
```

---

## 🚀 快速安装 (Installation)

### 1. 全局安装
```bash
git clone https://github.com/duanshuaimin/pan-gdrive-sync.git
cd pan-gdrive-sync
pip install -e .
```
安装后，系统将注册全局指令：
- 主命令：`pan-gdrive-sync`
- 简短别名：`pgsync`

---

## 🔑 凭证配置 (Authentication)

### 1. 配置百度网盘
如果您之前已使用过 `baidupan` 或 `BaiduPCS-Go`，本工具将**自动继承登录状态**，无需任何操作。如需手动配置：
```bash
# 使用 BDUSS 登录
pan-gdrive-sync auth baidu --bduss "your_bduss_token"

# 或直接粘贴完整浏览器 Cookie
pan-gdrive-sync auth baidu --cookies "BDUSS=...; STOKEN=..."
```

### 2. 配置 Google Drive
支持以下两种方式之一：

#### 方式 A：服务账号 JSON 密钥（推荐用于服务器与自动化脚本）
1. 在 [Google Cloud Console](https://console.cloud.google.com/) 创建项目并启用 **Google Drive API**。
2. 在“凭据”中创建 **服务账号 (Service Account)** 并生成 JSON 密钥下载到本地（例如 `credentials.json`）。
3. 将您的 Google Drive 目标文件夹共享给该服务账号的邮箱（权限设为“编辑器”）。
4. 在 CLI 中绑定密钥：
   ```bash
   pan-gdrive-sync auth gdrive --service-account /path/to/credentials.json
   ```

#### 方式 B：OAuth2 令牌
```bash
pan-gdrive-sync auth gdrive --token "ya29.your_access_token..."
```

### 3. 查看双端连接状态
```bash
pan-gdrive-sync status
```
终端将以表格形式展示百度网盘与 Google Drive 的连接状态、账户名、VIP 等级及存储空间使用情况。

---

## 📖 使用指南 (Usage Examples)

### 1. 浏览两端文件目录 (`ls`)
```bash
# 列出百度网盘指定目录
pan-gdrive-sync ls baidu:/2015-2026语文中考真题

# 列出 Google Drive 根目录或子文件夹
pan-gdrive-sync ls gdrive:/
pan-gdrive-sync ls gdrive:/Backup
```

### 2. 单文件跨云互传 (`copy`)
```bash
# 百度网盘 -> Google Drive
pan-gdrive-sync copy baidu:/2026年全国各地中考语文作文题目精选汇编.docx gdrive:/Backup/

# Google Drive -> 百度网盘
pan-gdrive-sync copy gdrive:/Report.pdf baidu:/资料归档/

# 若目标文件存在则跳过 (--skip)
pan-gdrive-sync copy baidu:/video.mp4 gdrive:/Videos/ --skip
```

### 3. 文件夹整目录跨云同步 (`sync`)
```bash
# 将百度网盘文件夹完整同步至 Google Drive
pan-gdrive-sync sync baidu:/2015-2026语文中考真题 gdrive:/Archives/

# 将 Google Drive 文件夹完整同步至百度网盘
pan-gdrive-sync sync gdrive:/ProjectFiles baidu:/WorkBackup/

# 仅同步当前层级，不递归子文件夹 (--no-recursive)
pan-gdrive-sync sync baidu:/Docs gdrive:/Docs --no-recursive
```

### 4. 查看空间配额对比 (`quota`)
```bash
pan-gdrive-sync quota
```

### 5. 持久化同步任务与定时计划 (`job`)
无需每次手动输入命令，您可以将常用的跨云同步策略保存为**持久化任务规则**，支持开机常驻定时自动同步：

```bash
# 列出所有已保存的持久化同步任务
pan-gdrive-sync job list

# 新建持久化同步任务（例如每 1 小时自动同步一次，跳过已存在文件）
pan-gdrive-sync job add baidu:/2015-2026语文中考真题 gdrive:/Archives/ --name "真题资料定时同步" --interval 3600 --skip

# 立即手动触发执行指定持久化任务
pan-gdrive-sync job run job_1788521964_2ec45b

# 暂停或恢复任务调度
pan-gdrive-sync job toggle job_1788521964_2ec45b

# 删除持久化任务规则
pan-gdrive-sync job delete job_1788521964_2ec45b
```

### 6. 查看持久化历史传输记录 (`history`)
所有单次传输与定时同步任务的执行状态、传输耗时、错误日志均持久化记录在本地 SQLite 数据库中，即便服务重启也不丢失：

```bash
pan-gdrive-sync history --limit 20
```

---

## 🌐 Web 控制台交互界面 (Web UI Dashboard)

除了强大的命令行体验，**Pan-GDrive-Sync** 还内置了开箱即用的现代化 Web 可视化控制台！

### 1. 启动 Web 界面
```bash
# 默认启动并监听 http://127.0.0.1:8080
pan-gdrive-sync web

# 指定端口与局域网/公网绑定 IP（便于远程浏览器访问）
pan-gdrive-sync web --host 0.0.0.0 --port 8080
# 或使用快捷命令
pgsync web -h 0.0.0.0 -p 8080
```

### 2. Web 界面功能亮点
- 🖥️ **双栏网盘文件浏览器 (Dual-Pane File Explorer)**：
  - 左栏呈现百度网盘目录树，右栏呈现 Google Drive 目录树。
  - 支持多级面包屑点击跳转、文件夹穿梭、实时搜索筛选。
- ⚡ **图形化跨云点对点直传与整目录同步**：
  - 勾选文件或文件夹，点击中心控制台一键直传。
  - 支持覆盖或跳过冲突策略。
- 📈 **SSE 实时流式任务进度看板 (Task Manager)**：
  - 底部智能抽屉实时追踪每一个传输中任务的瞬时速率 (MB/s)、剩余时间估算 (ETA)、完成百分比、已传大小。
  - 支持随时中止/取消传输中任务。
- 🔑 **可视化凭证配置与配额监控**：
  - 网页端直接配置/切换百度网盘 BDUSS/Cookie 或 Google Drive 服务账号 JSON 密钥。
  - 图形化对比两端已用空间与总容量占比。

---

## 🧪 自动化测试验证 (Testing)

运行端到端自动化测试套件：
```bash
python3 tests/test_sync.py
```

---

## 📄 开源许可证 (License)

本项目采用 [MIT License](LICENSE) 开源协议。欢迎提交 Issue 与 Pull Request！
