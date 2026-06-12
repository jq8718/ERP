# ERP Windows 轻量安装器

本目录用于 Windows 内网服务器部署。它不是把 ERP 编译成单个 exe，而是提供一组可审计、可重复执行的 PowerShell 安装脚本。

## 目录说明

| 路径 | 用途 |
| --- | --- |
| `download-prerequisites.ps1` | 下载 Python、PostgreSQL、Git、NSSM 安装包到 `installer/packages/` |
| `preflight-prerequisites.ps1` | 检查服务器是否已安装 Python、PostgreSQL、Git |
| `install-erp.ps1` | 部署 ERP、创建 `.env`、安装依赖、迁移数据库、收集静态文件、创建管理员 |
| `register-windows-service.ps1` | 使用 NSSM 注册 Waitress Windows 服务 |
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
.\installer\install-erp.ps1 -ServerHost 192.168.1.10
.\installer\register-windows-service.ps1
```

4. 打开浏览器访问：

```text
http://服务器IP:8000/
```

## 说明

- 轻量安装器默认不自动安装 Python/PostgreSQL/Git，因为这些安装过程需要管理员权限和现场确认。
- 如果 `preflight-prerequisites.ps1` 提示缺少组件，可从 `installer/packages/` 中手动安装对应安装包。
- `install-erp.ps1` 可以重复执行；它会复用现有 `.env`，不会覆盖已有管理员密码。
