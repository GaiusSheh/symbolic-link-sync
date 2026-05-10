# Sym-Link GUI

## 项目概述

Windows Junction/Symlink 管理工具，GUI 用 Python + tkinter 实现，配置存 `symlinks.json`，运行时状态存 `gui/state.json`。

## 当前状态

- **阶段**：功能完善中，核心功能已稳定
- **最近完成**：GUI 文件重组（core/ + ui/）、cut&paste 修复、多行选中删除、分隔符居中、confirmed_empty 生命周期管理
- **下一步**：按需

## 项目结构

```
gui/
├── main.py               # 入口，AppController
├── core/
│   ├── symlink_manager.py  # JSON 读写、条目管理、normalize_entries
│   ├── settings_manager.py # settings.json
│   └── watcher.py          # watchdog + DriveRootThread (ReadDirectoryChangesW)
└── ui/
    ├── window.py           # 主状态窗口（Treeview）
    ├── scan_window.py      # 扫描窗口
    ├── settings_window.py  # 设置 / base 管理
    ├── registration_window.py
    ├── dialogs.py
    ├── tray.py / icons.py / notifier.py
    └── assets/
```

## 关键技术决策

- **multi-base**：`{key}` 模板替代单一 `{onedrive}`，`symlinks.json` 的 `machines` 区存各机器 base 路径
- **local_data**：本机独有条目和扫描结果存 `local_data[MACHINE]`，不同步到其他机器
- **DriveRootThread**：`ReadDirectoryChangesW` 监听整个驱动器目录变化，解决 Explorer cut&paste junction 后无法自动修复的问题
- **normalize_entries**：统一 promote/demote 逻辑，`_do_sync/_do_refresh/_do_repath` 等处均调用
- **venv**：`C:\venvs\sym-link-gui`（OneDrive 外），`pywin32>=306` 必须安装

## 已知约定

- `symlinks.json` 在 `.gitignore`（用户数据）
- `gui/state.json`、`gui/settings.json` 在 `.gitignore`（运行时状态）
- Treeview iid 中 `{}`→`__LB__/__RB__` 转义（避免 Tcl 元字符报错）
- `confirmed_empty` 在每次 refresh/sync 后自动 prune 过期 ID
