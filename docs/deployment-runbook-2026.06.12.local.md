# ERP 生产部署命令清单

- host: `erp.example.com`
- operator: `admin`
- version: `2026.06.12.local`
- generated_at: `2026-06-12`

## 1. 执行前确认

- 已配置生产 `.env`，且 `DJANGO_ENV=production`。
- 已配置真实 PostgreSQL：`DB_ENGINE=postgres`、`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_HOST`、`POSTGRES_PORT`。
- 已配置 `DJANGO_SECRET_KEY`，长度不少于 50 字符，不能使用开发默认值。
- 已配置正式域名：`DJANGO_ALLOWED_HOSTS`、`DJANGO_CSRF_TRUSTED_ORIGINS`。
- 已确认附件、备份、日志、静态文件目录互相隔离。
- `.tmp/` 仅用于本机安装缓存，不参与部署和版本提交。

## 2. 部署命令

```powershell
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py bootstrap_admin --username admin --password-env ERP_BOOTSTRAP_ADMIN_PASSWORD --noinput
python manage.py bootstrap_admin --username admin --check-only
python manage.py simulate_production_settings --host erp.example.com
python manage.py release_gate --operator admin --include-deploy-check --include-tests --include-production-preflight --report-file docs/latest-release-gate-report.md
python manage.py backup_daily
python manage.py verify_backups
python manage.py restore_drill
python manage.py process_pending_events
python manage.py business_smoke_test --operator admin
python manage.py record_release 2026.06.12.local --summary '生产发布' --released-by admin
python manage.py prelaunch_report --strict --bootstrap-username admin --release-version 2026.06.12.local --report-file docs/prelaunch-acceptance-report.md --deployment-runbook-file 'docs\deployment-runbook-2026.06.12.local.md'
```

## 3. 当前本机验证结果

- 发布门禁：通过，13/13。
- 完整自动测试：通过，728 tests OK。
- PostgreSQL 行锁并发测试：通过，3 tests OK。
- 本机 PostgreSQL：17.10，服务 `postgresql-x64-17` 运行中。

正式上线时必须在生产服务器重新执行本清单，并以生产环境生成的 `latest-release-gate-report.md` 和 `prelaunch-acceptance-report.md` 为最终依据。
