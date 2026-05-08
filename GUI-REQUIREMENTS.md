# Sym-Link GUI 需求文档

## 概述

为现有的 Sym-Link 符号链接管理工具开发一个 Python 图形界面，取代 `watch_symlinks.ps1`，
提供托盘常驻、状态可视化、断链修复等功能。

---

## 技术栈

- **语言**：Python 3
- **托盘**：`pystray`
- **界面**：`tkinter`（标准库，无需额外安装）
- **图标**：`Pillow`（动态绘制托盘图标颜色）
- **文件监听**：`watchdog`
- **打包**：`pyinstaller`（打包为单个 .exe，无需 Python 环境）

---

## 文件结构

```
Sym-Link/
├── symlinks.json          ← 已有，数据来源
├── sync_symlinks.ps1      ← 已有，保留作独立工具
├── setup.ps1              ← 已有，改为注册 Python GUI 到开机启动
├── scan_symlinks.ps1      ← 已有，保留
├── README.md              ← 已有
└── gui/
    ├── main.py            ← 入口
    ├── tray.py            ← 托盘逻辑
    ├── window.py          ← 主窗口
    ├── symlink_manager.py ← 核心逻辑（读写 JSON、建链、检测）
    ├── watcher.py         ← 文件监听 + 定时检测
    └── assets/
        └── icon.png       ← 托盘图标底图（可选）
```

---

## 功能需求

### 1. 托盘图标

- 程序启动后常驻系统托盘，无主窗口
- 图标颜色动态反映当前状态：
  - 🟢 绿色：所有链接正常
  - 🟡 黄色：有链接 target 不存在（等待同步，非错误）
  - 🔴 红色：有 broken junction（target 曾存在现已消失）
- 鼠标悬停 tooltip 显示简要状态，例如："14 OK, 1 broken"

### 2. 托盘右键菜单

```
打开状态窗口
立即同步
───────────
上次同步：14:32:01
下次检测：14:42:01
───────────
退出
```

### 3. 主状态窗口

点击"打开状态窗口"或双击托盘图标时弹出。

**表格列：**

| 列 | 说明 |
|---|---|
| 状态图标 | ✅ OK / ⚠️ Missing / ❌ Broken |
| ID | symlink 的 id 字段 |
| Link 路径 | 链接所在位置（相对 OneDrive 根显示） |
| Target 路径 | 目标路径（相对 OneDrive 根显示） |
| 操作 | Broken 行显示"修复..."按钮 |

**状态说明：**
- ✅ OK：Junction 存在且 target 可达
- ⚠️ Missing：Junction 已建但 target 不存在（OneDrive 未同步，静默）
- ❌ Broken：Junction 存在但 target 曾存在现已消失（需要修复）
- ➕ Pending：target 存在但 Junction 尚未创建

**底部按钮：**
- 立即同步
- 打开 symlinks.json

### 4. 断链修复流程

点击 Broken 行的"修复..."按钮：

1. 弹出文件夹选择对话框，标题："为 [id] 选择新的 target 目录"
2. 用户选择新路径后，预览变更："将 target 从 X 改为 Y"
3. 确认后：
   - 写回 `symlinks.json`（更新 target 字段）
   - 立即重建 Junction
   - 状态行刷新为 ✅ OK

### 5. 后台监听与定时检测

取代 `watch_symlinks.ps1`，在后台线程中：

- **文件监听**：`watchdog` 监听 `symlinks.json` 变化，变化后 debounce 2 秒触发同步
- **定时检测**：每 10 分钟执行一次状态检测
- **启动时**：执行一次完整同步

### 6. Toast 通知

检测到 Broken junction 时发送 Windows toast 通知：

- 标题："Sym-Link: N 个断链"
- 内容：断链的 id 列表
- 点击通知：打开主状态窗口并高亮断链行

### 7. 开机自启

`setup.ps1` 更新为将 `gui/dist/symlink-gui.exe` 注册到 Task Scheduler（登录触发，隐藏窗口），取代原来注册 `watch_symlinks.ps1` 的逻辑。

---

## 非功能需求

- 打包后 .exe 体积尽量小（pyinstaller --onefile）
- 窗口关闭时不退出程序，只隐藏到托盘
- 支持多显示器（窗口居中显示）
- 日志写入 `gui/symlink-gui.log`，保留最近 500 行

---

## 不在范围内

- 新增 / 删除 symlink 条目（直接编辑 JSON）
- 管理 machines 配置
- Linux / macOS 支持

---

**版本**：v0.1  
**日期**：2026-05-08