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
| ERP 项目文件夹 | 包含 `ERP-Setup.exe`、`ERP-Setup-Console.cmd`、`ERP-Uninstall.exe`、`installer`、`manage.py` |
| 前置软件安装包目录 | 开发机保存于 `D:\XC\2026\ERP\installer\packages`，里面有 Python、PostgreSQL、Git、NSSM 安装包 |
| 服务器固定 IP | 例如 `192.168.1.10`，可以先用本机当前内网 IP，但正式使用前建议固定 |
| PostgreSQL 超级用户密码 | 安装 PostgreSQL 时自己设置，后面 ERP 初始化数据库时会用到，请记下来 |
| ERP 数据库密码 | 给 ERP 连接数据库用，请自己设置一个强密码 |
| ERP 管理员密码 | ERP 登录账号 `admin` 的初始密码 |

建议把整个 ERP 文件夹放到：

```text
D:\ERP安装包
```

注意：`ERP安装包` 默认不再包含 Python、PostgreSQL、Git 这些大安装程序。现场干净电脑需要先从开发机的：

```text
D:\XC\2026\ERP\installer\packages
```

复制或准备对应安装程序。`nssm.exe` 很小，已经随 ERP 安装包放在：

```text
ERP安装包\installer\tools\nssm.exe
```

Python 依赖库已经随 ERP 安装包放在：

```text
ERP安装包\installer\wheels
```

因此安装过程中的“安装 Python 依赖”通常不需要联网。

安装完成后的系统默认会放到：

```text
D:\ERP\app
D:\ERP\data
D:\ERP\backups
D:\ERP\logs
```

## 2. 确认服务器 IP

ERP 给多名员工使用时，访问地址一般是：

```text
http://服务器IP:8000/
```

所以服务器 IP 最好固定。安装程序可以自动检测本机当前内网 IP，直接按回车使用默认值也可以，但如果这个 IP 是路由器动态分配的，服务器重启后可能会变。

如果 IP 变了，会出现这些情况：

- 员工原来收藏的 ERP 地址打不开。
- 桌面快捷方式可能打不开。
- ERP 配置里的允许访问地址还是旧 IP，可能需要技术人员修改 `.env` 后重启服务。

因此正式使用建议二选一：

1. 在路由器里给这台服务器做 DHCP 地址保留，让它每次都拿到同一个 IP。
2. 在 Windows 网络设置里手动设置固定 IP。

如果只是临时测试，可以先用自动检测到的本机地址。

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

如果不确定这个 IP 是否固定，请找负责网络的人确认。

## 3. 安装 Python

打开前置软件安装包目录，例如：

```text
D:\XC\2026\ERP\installer\packages
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

打开前置软件安装包目录，例如：

```text
D:\XC\2026\ERP\installer\packages
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

如果已经进入 Stack Builder 的“欢迎使用 Stack Builder”界面：

- 正常做法：直接点击 `Cancel` / `取消`，关闭 Stack Builder。
- 不要选择 `远程服务器`。
- 如果界面必须先选择一项才能继续关闭，选择 `PostgreSQL 17 (x64) on port 5432`，然后后续不要勾选任何附加组件，直接取消退出。

Stack Builder 是 PostgreSQL 的附加组件安装工具，ERP 不需要通过它安装任何东西。

### PostgreSQL 密码怎么填

安装 PostgreSQL 时会要求设置 `postgres` 用户密码。这个密码是数据库管理员密码，用来管理 PostgreSQL 数据库。

这个密码不是 ERP 登录密码，员工以后登录 ERP 时不会用它。

它的用途是：运行 `ERP-Setup.exe` 时，安装程序需要用这个密码登录 PostgreSQL，然后自动创建 ERP 数据库和 ERP 专用数据库账号。

请设置一个强密码，并写在纸上或交给系统管理员保存。示例：

```text
PgAdmin@2026!
```

注意：

- 不要设置成 `123456`、`password`、公司名、手机号等简单密码。
- 不要和 ERP 管理员 `admin` 的登录密码相同。
- 安装完成后不要删除或忘记这个密码，以后数据库维护、备份恢复时可能还会用到。

### PostgreSQL 端口号怎么填

端口号保持默认：

```text
5432
```

端口号可以理解为 ERP 程序连接数据库时使用的“门牌号”。ERP 程序通过这个端口找到 PostgreSQL 数据库。

普通内网安装不要修改端口号，直接使用 `5432` 即可。

只有在服务器上已经安装过另一个 PostgreSQL，并且 `5432` 被占用时，才需要改成其他端口，例如：

```text
5433
```

如果安装 PostgreSQL 时改了端口号，后面 ERP 配置里的 `POSTGRES_PORT` 也必须填写同一个端口号。否则 ERP 会连不上数据库。

## 5. 确认 NSSM

NSSM 用来把 ERP 注册成 Windows 服务。注册成服务后，服务器重启时 ERP 可以自动启动，不需要人工打开命令窗口。

新版 ERP 安装包已经内置 `nssm.exe`，请确认下面文件存在：

```text
ERP安装包\installer\tools\nssm.exe
```

如果能看到 `nssm.exe`，就说明 NSSM 准备好了。

NSSM 不需要单独双击运行，后面执行 `ERP-Setup.exe` 时，安装程序会自动调用它，把 ERP 注册成 Windows 服务。

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

如果双击后窗口闪一下就消失，请看本手册第 20.6 节“ERP-Setup.exe 闪退怎么办”。

## 8. 输入服务器 IP

安装程序会提示：

```text
Enter ERP server IP or intranet host name [default: 192.168.1.10]
```

方括号里的 `default` 是安装程序自动检测到的本机当前内网 IP。

如果这个 IP 已经是固定 IP，可以直接按回车使用默认值。

如果要指定其他固定 IP，请输入刚才记下来的 IP，例如：

```text
192.168.1.10
```

然后按回车。

注意：不要输入 `127.0.0.1` 或 `localhost`。这两个地址只能服务器自己访问，其他员工电脑打不开。

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

后面文件复制完成后，程序还会再次提示：

```text
Enter PostgreSQL application user password for erp_app
```

这里仍然输入同一个 ERP 数据库密码。

注意：

- `erp_app` 是数据库用户名。
- 这一步要输入的是 `erp_app` 的数据库密码，不是用户名。
- 不要输入 `user`，除非你前面真的把 `erp_app` 密码设置成了 `user`。
- 不要输入 PostgreSQL 超级用户 `postgres` 的密码。
- 不要输入 ERP 管理员 `admin` 的登录密码。

示例：如果前面设置给 `erp_app` 的密码是：

```text
ErpDb@2026!
```

那么这里也输入：

```text
ErpDb@2026!
```

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
- 安装 Python 依赖，优先从 `installer\wheels` 离线安装。
- 初始化数据库表。
- 收集静态文件。
- 创建 ERP 管理员。
- 执行生产预检。
- 注册 ERP Windows 服务。
- 自动开放 `8000` 防火墙端口。
- 注册备份和后台任务。
- 创建桌面快捷方式。

说明：现场服务器安装时默认不跑完整发布门禁测试。完整测试应在开发机或发布打包前执行；现场安装只检查生产配置、数据库、目录、管理员、服务等部署必需项。

这个过程可能需要几分钟。

看到类似下面内容说明完成：

```text
ERP 安装完成。访问地址：http://192.168.1.10:8000/
```

如果没有看到“ERP 安装完成”或“ERP setup completed”，而是看到 `CommandError`、`[FAIL]`、`failed`、`生产预检未通过`，说明安装还没有完成。请不要直接使用 ERP，先查看第 20.7 节。

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

## 18. 开放 8000 防火墙端口

ERP 默认通过 `8000` 端口提供浏览器访问。

新版安装器会在安装完成时自动放行 `8000` 端口，一般不需要手工执行本节命令。

如果服务器自己能打开 ERP，但局域网其他电脑打不开，通常是 Windows 防火墙没有允许外部电脑访问 `8000` 端口。此时再按下面步骤手工补一遍即可。

在 ERP 服务器上操作：

1. 点击开始菜单。
2. 输入：

```text
powershell
```

3. 右键 `Windows PowerShell`。
4. 选择 `以管理员身份运行`。
5. 在 PowerShell 窗口里输入：

```powershell
New-NetFirewallRule -DisplayName "ERP Web 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Any
```

看到没有报错，就说明规则已添加。

如果你打开的是 CMD 黑色窗口，不是 PowerShell，请改用下面命令：

```cmd
netsh advfirewall firewall add rule name="ERP Web 8000" dir=in action=allow protocol=TCP localport=8000
```

然后在其他电脑浏览器访问：

```text
http://服务器IP:8000/
```

例如：

```text
http://192.168.1.10:8000/
```

## 19. 卸载 ERP 服务

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

## 20. 常见问题

### 20.1 浏览器打不开

检查：

1. 服务器 IP 是否输入正确。
2. ERP Web Service 是否正在运行。
3. 防火墙是否允许 TCP 8000。
4. 访问地址是否是 `http://`，不是 `https://`。

如果服务器本机可以打开 ERP，但其他电脑打不开，请先在其他电脑上测试：

```powershell
Test-NetConnection 服务器IP -Port 8000
```

例如：

```powershell
Test-NetConnection 192.168.1.10 -Port 8000
```

如果结果是：

```text
TcpTestSucceeded : False
```

请回到第 18 节，在服务器上开放 `8000` 防火墙端口。

### 20.2 提示数据库连接失败

检查：

1. PostgreSQL 服务是否正在运行。
2. `.env` 中 `POSTGRES_PASSWORD` 是否正确。
3. 安装时设置的 ERP 数据库密码是否和 `.env` 一致。

### 20.3 忘记 ERP 管理员密码

需要技术人员在服务器上执行密码重置命令。

### 20.4 服务器重启后还能用吗

可以。ERP 已注册为 Windows 服务，正常会自动启动。

### 20.5 能不能直接删除 ERP 文件夹

不建议。请先运行 `ERP-Uninstall.exe` 删除服务和计划任务，再由技术人员决定是否删除数据。

### 20.6 ERP-Setup.exe 闪退怎么办

如果现场双击 `ERP-Setup.exe` 后窗口闪一下就消失，通常不是 ERP 已安装完成，而是安装器启动失败或中途报错。

请按下面顺序检查：

1. 不要只拷贝 `ERP-Setup.exe` 一个文件。必须拷贝整个 `ERP安装包` 文件夹。
2. `ERP-Setup.exe` 必须和 `installer`、`manage.py` 在同一个目录。
3. 请右键 `ERP-Setup.exe`，选择 `以管理员身份运行`。
4. 如果 Windows 弹出权限确认窗口，请点击 `是`。
5. 不要从压缩包里面直接双击运行。请先把 `ERP安装包.zip` 完整解压到磁盘目录，例如：

```text
D:\ERP安装包
```

然后再运行：

```text
D:\ERP安装包\ERP-Setup.exe
```

新版安装器如果出错，会停在黑色窗口并显示错误原因，同时生成日志文件。日志位置一般在：

```text
ERP安装包\installer\logs
```

如果没有看到 `logs` 目录，说明使用的可能是旧安装包，或者新版安装器还没有运行到创建日志的步骤。请先换用最新的 `ERP安装包`，然后重新运行 `ERP-Setup.exe`。

如果还是闪退，请不要继续双击 `ERP-Setup.exe`，改用控制台启动器：

```text
ERP-Setup-Console.cmd
```

操作方法：

1. 右键 `ERP-Setup-Console.cmd`。
2. 选择 `以管理员身份运行`。
3. 黑色窗口会停住，不会自动消失。这个窗口里的提示是英文，属于正常现象。
4. 按窗口提示操作。
5. 如果失败，把窗口里的错误信息截图，或把 `installer\logs` 里的最新日志发给开发人员。

如果运行 `ERP-Setup-Console.cmd` 时看到类似 `不是内部或外部命令` 的乱码错误，说明使用的是旧安装包里的中文批处理文件。请换用新版 `ERP安装包.zip`，新版控制台启动器已经改成英文，不会再被 Windows 批处理解释成乱码命令。

新版 `ERP-Setup.exe` 在 PowerShell 还没启动前也会先写一个启动日志，文件名类似：

```text
installer\logs\erp-launcher-erp-setup-20260612-221500.log
```

如果仍然无法安装，请把这个目录里的最新 `.log` 文件发给开发人员。

### 20.7 提示生产预检未通过怎么办

如果日志里出现：

```text
[WARN] 生产环境标记: DJANGO_ENV 不是 production/prod
CommandError: 生产预检未通过
```

说明 ERP 文件、数据库和管理员账号可能已经初始化成功，但安装还没有完整完成，Windows 服务和桌面快捷方式可能还没有正确注册。

常见原因是：服务器上以前安装过一次，`D:\ERP\app\.env` 已存在，旧配置里没有设置生产环境标记。

新版安装器会自动修复这个问题。处理方法：

1. 换用最新的 `ERP安装包`。
2. 右键运行 `ERP-Setup-Console.cmd`，选择“以管理员身份运行”。
3. 按提示重新输入服务器 IP 和数据库密码。
4. 看到“ERP setup completed”或“ERP 安装完成”后，才表示安装完成。

如果暂时无法换新版安装包，也可以让技术人员手动打开：

```text
D:\ERP\app\.env
```

确认或修改下面几行：

```ini
DJANGO_ENV=production
DJANGO_DEBUG=false
DB_ENGINE=postgres
DJANGO_ALLOWED_HOSTS=服务器IP
ERP_INTRANET_HTTP_RISK_ACCEPTED_BY=System Administrator
ERP_ATTACHMENT_SCAN_RISK_ACCEPTED_BY=System Administrator
```

保存后重新运行 `ERP-Setup-Console.cmd`。

注意：不要删除 `DJANGO_SECRET_KEY` 和 `POSTGRES_PASSWORD`，这两个值必须保留。

### 20.8 怎么判断安装是否真的成功

安装成功需要同时满足：

1. 安装窗口最后显示“ERP setup completed”或“ERP 安装完成”。
2. `installer\logs` 最新日志里没有 `CommandError`、`[FAIL]`、`failed with exit code`。
3. Windows 服务里能看到 `ERP Web Service`，状态是“正在运行”。
4. 浏览器能打开：

```text
http://服务器IP:8000/
```

5. 能用 `admin` 账号登录 ERP。

如果只看到“数据库创建成功”“迁移 OK”“静态文件 copied”，还不能算安装完成；这些只是中间步骤。

如果日志里出现“Release gate skipped during server installation”，这是正常提示，表示现场安装跳过了完整发布门禁测试，不代表失败。
