# Windows10 内网 ERP 一键安装手册

适用场景：

- 一台干净的 Windows 10 电脑作为 ERP 服务器。
- 公司内网使用，没有域名。
- 员工用浏览器访问 ERP。
- 安装人员不需要懂编程，只按步骤操作。

## 1. 安装前准备

请先准备：

| 项目 | 说明 |
| --- | --- |
| ERP 项目文件夹 | 包含 `ERP-Setup.exe`、`ERP-Uninstall.exe`、`installer`、`manage.py` |
| 安装包目录 | `installer\packages`，里面应有 Python、PostgreSQL、Git、NSSM 安装包 |
| 服务器固定 IP | 例如 `192.168.1.10` |
| PostgreSQL 超级用户密码 | 安装 PostgreSQL 时自己设置，请记下来 |
| ERP 数据库密码 | 给 ERP 连接数据库用，请自己设置一个强密码 |
| ERP 管理员密码 | ERP 登录账号 `admin` 的初始密码 |

建议把整个 ERP 文件夹放到：

```text
D:\ERP安装包
```

安装完成后的系统默认会放到：

```text
D:\ERP\app
D:\ERP\data
D:\ERP\backups
D:\ERP\logs
```

## 2. 确认服务器 IP

在服务器上按：

```text
Win + R
```

输入：

```text
cmd
```

点击确定。

在黑色窗口输入：

```cmd
ipconfig
```

找到类似下面的 IPv4 地址：

```text
IPv4 地址 . . . . . . . . . . . . : 192.168.1.10
```

把这个 IP 记下来，后面会用到。

## 3. 安装 Python

打开：

```text
ERP项目文件夹\installer\packages
```

双击：

```text
python-3.12.10-amd64.exe
```

安装时注意：

1. 勾选 `Add python.exe to PATH`。
2. 点击 `Install Now`。
3. 等待安装完成。
4. 点击 `Close`。

## 4. 安装 PostgreSQL

打开：

```text
ERP项目文件夹\installer\packages
```

双击：

```text
postgresql-17.10-1-windows-x64.exe
```

安装时按默认下一步即可，注意：

1. 安装目录可以保持默认。
2. 组件保持默认。
3. 数据目录保持默认。
4. 设置 PostgreSQL 超级用户 `postgres` 的密码，请务必记下来。
5. 端口保持默认：`5432`。
6. Locale 保持默认。
7. 点击安装。

安装完成后，如果出现 Stack Builder，可以取消，不需要安装。

## 5. 安装 NSSM

NSSM 用来把 ERP 注册成 Windows 服务。

打开：

```text
ERP项目文件夹\installer\packages
```

找到：

```text
nssm-2.24.zip
```

右键，选择：

```text
全部解压
```

解压到当前目录即可。解压后应出现：

```text
installer\packages\nssm-2.24\win64\nssm.exe
```

## 6. Git 可以不装

如果 ERP 项目文件夹已经完整复制到服务器，Git 可以不装。

只有需要在服务器上从 GitHub 拉代码时，才需要安装：

```text
Git-2.51.0-64-bit.exe
```

普通内网部署可以跳过 Git。

## 7. 运行 ERP 安装程序

回到 ERP 项目文件夹，找到：

```text
ERP-Setup.exe
```

右键点击，选择：

```text
以管理员身份运行
```

如果 Windows 弹出安全提示，点击允许。

## 8. 输入服务器 IP

安装程序会提示：

```text
请输入 ERP 服务器固定 IP 或内网主机名，例如 192.168.1.10
```

输入刚才记下来的 IP，例如：

```text
192.168.1.10
```

然后按回车。

## 9. 查看安装检查结果

程序会检查：

- Python
- PostgreSQL
- PostgreSQL 服务
- NSSM

如果显示 `[OK]`，说明通过。

如果显示 `[MISS]`，说明缺少对应软件，请回到前面步骤安装，然后重新运行 `ERP-Setup.exe`。

Git 显示 `[MISS]` 一般没关系，只要项目文件夹已经完整复制到服务器，就可以继续。

看到提示：

```text
是否继续初始化 PostgreSQL 和 ERP？输入 Y 继续
```

输入：

```text
Y
```

按回车。

## 10. 输入 PostgreSQL 密码

程序会提示输入 PostgreSQL 超级用户密码：

```text
请输入 PostgreSQL 超级用户 postgres 的密码
```

输入第 4 步安装 PostgreSQL 时设置的密码。

输入时屏幕可能不显示，这是正常的。

然后程序会提示：

```text
请输入要设置给应用账号 erp_app 的密码
```

这里输入一个新的 ERP 数据库密码。请记下来。

## 11. 输入 ERP 初始管理员密码

程序会提示：

```text
请输入 ERP 初始管理员 admin 的密码
```

输入 ERP 管理员密码。

要求：

- 至少 12 位。
- 不要包含 `admin`。
- 建议包含大小写字母、数字和符号。

示例：

```text
ErpAdmin@2026!
```

## 12. 等待安装完成

安装程序会自动执行：

- 创建数据库。
- 创建 `.env` 配置文件。
- 安装 Python 依赖。
- 初始化数据库表。
- 收集静态文件。
- 创建 ERP 管理员。
- 注册 ERP Windows 服务。
- 注册备份和后台任务。
- 创建桌面快捷方式。

这个过程可能需要几分钟。

看到类似下面内容说明完成：

```text
ERP 安装完成。访问地址：http://192.168.1.10:8000/
```

## 13. 打开 ERP 系统

服务器会自动打开浏览器。

也可以手动打开浏览器，输入：

```text
http://服务器IP:8000/
```

例如：

```text
http://192.168.1.10:8000/
```

登录账号：

```text
admin
```

密码就是第 11 步输入的 ERP 管理员密码。

## 14. 其他电脑访问 ERP

公司内网其他电脑打开浏览器，输入：

```text
http://服务器IP:8000/
```

例如：

```text
http://192.168.1.10:8000/
```

推荐使用：

- Microsoft Edge
- Google Chrome

## 15. 安装后必须做的一件事

安装完成后，请打开：

```text
D:\ERP\app\.env
```

找到：

```ini
ERP_BOOTSTRAP_ADMIN_PASSWORD=刚才输入的管理员密码
```

改成：

```ini
ERP_BOOTSTRAP_ADMIN_PASSWORD=
```

保存文件。

注意：

- `DJANGO_SECRET_KEY` 不要清空。
- `POSTGRES_PASSWORD` 不要清空。
- 只清空 `ERP_BOOTSTRAP_ADMIN_PASSWORD`。

## 16. 保护 .env 文件

在服务器上右键开始菜单，选择：

```text
Windows PowerShell（管理员）
```

输入：

```powershell
icacls D:\ERP\app\.env /inheritance:r
icacls D:\ERP\app\.env /grant:r Administrators:F SYSTEM:R
```

这样普通用户就不能随便查看 `.env` 里的数据库密码。

## 17. 检查 ERP 服务是否运行

按：

```text
Win + R
```

输入：

```text
services.msc
```

找到：

```text
ERP Web Service
```

状态应为：

```text
正在运行
```

启动类型应为：

```text
自动
```

## 18. 卸载 ERP 服务

如果只是停止 ERP，不要删除数据，可以运行：

```text
ERP-Uninstall.exe
```

右键选择：

```text
以管理员身份运行
```

输入：

```text
UNINSTALL
```

它会删除：

- ERP Windows 服务。
- ERP 计划任务。

它不会删除：

- PostgreSQL 数据库。
- 附件。
- 备份。
- 日志。
- `D:\ERP` 数据目录。

## 19. 常见问题

### 19.1 浏览器打不开

检查：

1. 服务器 IP 是否输入正确。
2. ERP Web Service 是否正在运行。
3. 防火墙是否允许 TCP 8000。
4. 访问地址是否是 `http://`，不是 `https://`。

### 19.2 提示数据库连接失败

检查：

1. PostgreSQL 服务是否正在运行。
2. `.env` 中 `POSTGRES_PASSWORD` 是否正确。
3. 安装时设置的 ERP 数据库密码是否和 `.env` 一致。

### 19.3 忘记 ERP 管理员密码

需要技术人员在服务器上执行密码重置命令。

### 19.4 服务器重启后还能用吗

可以。ERP 已注册为 Windows 服务，正常会自动启动。

### 19.5 能不能直接删除 ERP 文件夹

不建议。请先运行 `ERP-Uninstall.exe` 删除服务和计划任务，再由技术人员决定是否删除数据。
