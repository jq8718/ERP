# ERP 预上线验收报告

- 生成时间：2026-06-12 00:51:16
- 总体结果：需处理
- 硬性失败：0
- 预警：3
- 当前运行环境：development
- 生产模式：否
- 本次发布版本：2026.06.12.local
- 部署命令清单：docs\deployment-runbook-2026.06.12.local.md

## 结论说明

- 当前报告运行在非生产环境，剩余待处理项：完整上线门禁、生产环境标记、附件安全扫描；正式上线前必须在生产环境完成这些项目并重新执行 `prelaunch_report --strict`。

## 发布门禁

| 项目 | 值 |
| --- | --- |
| 报告路径 | `D:\XC\2026\ERP\docs\latest-release-gate-report.md` |
| 报告存在 | 是 |
| 最近结果 | 通过 |
| 是否未过期 | 是 |
| 检查步骤数 | 13 |
| 是否完整上线门禁 | 否 |
| 缺失门禁步骤 | Django 生产安全检查、生产严格预检 |
| 说明 | 最近门禁通过，生成于 2026-06-12 00:49:55 |

## 生产配置模拟

| 检查项 | 级别 | 结果 | 说明 |
| --- | --- | --- | --- |
| 生产配置模拟 | OK | 通过 | System check identified no issues (0 silenced). |

## 初始化验收

| 检查项 | 级别 | 结果 | 说明 |
| --- | --- | --- | --- |
| 初始化管理员 | OK | 通过 | admin / permission-admin 已通过 check-only |

## 生产预检

| 检查项 | 级别 | 结果 | 说明 |
| --- | --- | --- | --- |
| 生产环境标记 | WARN | 预警 | DJANGO_ENV 不是 production/prod |
| 数据库连接 | OK | 通过 | django.db.backends.sqlite3 |
| 数据库迁移 | OK | 通过 | 全部迁移已应用 |
| 附件目录 | OK | 通过 | D:\XC\2026\ERP\media |
| 备份目录 | OK | 通过 | D:\XC\2026\ERP\backups |
| 日志目录 | OK | 通过 | D:\XC\2026\ERP\logs |
| 目录隔离 | OK | 通过 | 附件、备份、日志和静态文件目录已隔离 |
| HTTPS 安全配置 | OK | 通过 | 非生产环境不强制检查 |
| 静态文件目录 | OK | 通过 | D:\XC\2026\ERP\staticfiles |
| 初始超级管理员 | OK | 通过 | 已存在启用状态超级管理员 |
| 权限管理员角色 | OK | 通过 | 权限管理员角色已分配 |
| 附件安全扫描 | WARN | 预警 | 未配置扫描命令，需记录风险接受人 |
| 失败后台任务 | OK | 通过 | 无失败后台任务 |
| 卡住后台任务 | OK | 通过 | 无超过 120 分钟的运行中任务 |
| 失败事务后事件 | OK | 通过 | 无失败事务后事件 |
| 卡住事务后事件 | OK | 通过 | 无超过 30 分钟的处理中事件 |

## 运维状态

| 项目 | 状态 |
| --- | --- |
| 最近备份 | BAK202606110001 / 成功 / 2026-06-11 16:42:01 |
| 最近后台任务 | JOB202606120001 / process_pending_events / 成功 / 2026-06-12 00:51:07 |
| 失败后台任务 | 0 |
| 待处理事务后事件 | 0 |
| 失败事务后事件 | 0 |
| 最近发布记录 | 2026.06.12.local / 2026-06-12 00:51:07 |

## 运维证据验收

| 检查项 | 级别 | 结果 | 说明 |
| --- | --- | --- | --- |
| 部署命令清单 | OK | 通过 | D:\XC\2026\ERP\docs\deployment-runbook-2026.06.12.local.md |
| 最近成功备份 | OK | 通过 | BAK202606110001 / 成功 / 2026-06-11 16:42:01 |
| 最近备份校验 | OK | 通过 | JOB202606110003 / backup_verify / 成功 / 2026-06-11 16:42:11 |
| 最近恢复演练 | OK | 通过 | JOB202606110004 / restore_drill / 成功 / 2026-06-11 16:42:19 |
| 事务后事件处理 | OK | 通过 | JOB202606120001 / process_pending_events / 成功 / 2026-06-12 00:51:07 |
| 发布记录 | OK | 通过 | 2026.06.12.local / 2026-06-12 00:51:07 |

## 后续动作

- 重新执行 `python manage.py release_gate --include-deploy-check --include-tests --include-production-preflight --report-file docs/latest-release-gate-report.md`，确保发布门禁报告通过且未过期。
- 设置生产环境变量：`DJANGO_ENV=production`、`DJANGO_DEBUG=false`、正式域名和 PostgreSQL 配置。
- 配置 `ERP_ATTACHMENT_SCAN_COMMAND`，或在验收记录中写明附件扫描风险接受人。
- 处理后重新执行 `python manage.py release_gate --include-deploy-check --include-tests --include-production-preflight --report-file docs/latest-release-gate-report.md`。
- 处理后重新执行 `python manage.py prelaunch_report --strict --bootstrap-username <用户名> --release-version <版本号> --deployment-runbook-file docs/deployment-runbook-<版本号>.md`。
