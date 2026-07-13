# ERP Windows 轻量安装器

本目录用于 Windows 内网服务器部署。它不是把 ERP 编译成单个 exe，而是提供一组可审计、可重复执行的 PowerShell 安装脚本。

## 目录说明

| 路径 | 用途 |
| --- | --- |
| `download-prerequisites.ps1` | 下载 Python、PostgreSQL、Git、NSSM 安装包到 `installer/packages/` |
| `build-launchers.ps1` | 生成根目录 `ERP-Setup.exe` 和 `ERP-Uninstall.exe` 双击启动器 |
| `preflight-prerequisites.ps1` | 检查服务器是否已安装 Python、PostgreSQL、Git |
| `setup-postgres-db.ps1` | 创建或修复 PostgreSQL 数据库和应用账号 |
| `install-erp.ps1` | 部署 ERP、创建 `.env`、安装依赖、迁移数据库、收集静态文件、创建管理员 |
| `register-windows-service.ps1` | 使用 NSSM 注册 Waitress Windows 服务 |
| `register-scheduled-tasks.ps1` | 注册备份、校验、事件处理等 Windows 计划任务 |
| `create-desktop-shortcut.ps1` | 创建浏览器访问 ERP 的桌面快捷方式 |
| `templates/intranet.env.template` | Windows 内网无域名 `.env` 模板 |
| `packages/` | 本地安装包缓存，不提交 Git |
| `work/` | 临时工作目录，不提交 Git |

## 推荐流程

1. 在开发机执行：

```powershell
.\installer\download-prerequisites.ps1
```

2. 将整个项目目录复制到 Windows 服务器。

3. 在服务器上以管理员 PowerShell 执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\installer\preflight-prerequisites.ps1
.\installer\setup-postgres-db.ps1
.\installer\install-erp.ps1 -ServerHost 192.168.1.10
.\installer\register-windows-service.ps1
.\installer\register-scheduled-tasks.ps1
.\installer\create-desktop-shortcut.ps1 -ServerHost 192.168.1.10
```

也可以直接双击项目根目录的 `ERP-Setup.exe`。该 exe 是启动器，会以管理员权限调用 `installer\erp-setup-launcher.ps1`。

4. 打开浏览器访问：

```text
http://服务器IP:8000/
```

## 说明

- 轻量安装器默认不自动安装 Python/PostgreSQL/Git，因为这些安装过程需要管理员权限和现场确认。
- 如果 `preflight-prerequisites.ps1` 提示缺少组件，可从 `installer/packages/` 中手动安装对应安装包。
- `install-erp.ps1` 可以重复执行；它会复用现有 `.env`，不会覆盖已有管理员密码。
- `register-scheduled-tasks.ps1` 会通过 `wscript.exe` 调用 `run-scheduled-task-hidden.js` 隐藏运行计划任务，避免服务器桌面每 5 分钟弹出 CMD 黑色窗口；任务输出写入 `logs\scheduled-*.log`。
- `ERP-Setup.exe` 和 `ERP-Uninstall.exe` 是小型启动器，不包含全部安装包和项目文件；复制到服务器时必须保留完整项目目录和 `installer\packages\`。
