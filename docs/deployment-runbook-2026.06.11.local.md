# ERP 生产部署命令清单
# host=erp.example.com
# operator=admin
# version=2026.06.11.local

.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py collectstatic --noinput
.\.venv\Scripts\python manage.py bootstrap_admin --username admin --password-env ERP_BOOTSTRAP_ADMIN_PASSWORD --noinput
.\.venv\Scripts\python manage.py bootstrap_admin --username admin --check-only
.\.venv\Scripts\python manage.py simulate_production_settings --host erp.example.com
.\.venv\Scripts\python manage.py release_gate --operator admin --include-deploy-check --include-tests --include-production-preflight --report-file docs\latest-release-gate-report.md
.\.venv\Scripts\python manage.py backup_daily
.\.venv\Scripts\python manage.py verify_backups
.\.venv\Scripts\python manage.py restore_drill
.\.venv\Scripts\python manage.py process_pending_events
.\.venv\Scripts\python manage.py business_smoke_test --operator admin
.\.venv\Scripts\python manage.py record_release 2026.06.11.local --summary 本地预上线验证 --released-by admin
.\.venv\Scripts\python manage.py prelaunch_report --strict --bootstrap-username admin --release-version 2026.06.11.local --report-file docs\prelaunch-acceptance-report.md --deployment-runbook-file docs\deployment-runbook-2026.06.11.local.md
