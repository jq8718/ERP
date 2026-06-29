# ERP 发布前门禁报告

- 生成时间：2026-06-26 15:59:48
- 总体结果：通过
- 检查步骤数：13

| 步骤 | 结果 | 摘要 | 命令 |
| --- | --- | --- | --- |
| Django 系统检查 | OK | System check identified no issues (0 silenced). | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check` |
| URL 引用完整性检查 | OK | URL 引用检查通过：715 个静态引用，712 个 URL 名称，294 个模板参数引用 | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_url_references` |
| 模板语法检查 | OK | 模板语法检查通过：113 个模板 | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_templates` |
| 权限配置检查 | OK | 权限配置检查通过：17 个默认权限 | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_permissions` |
| 权限引用完整性检查 | OK | 权限引用检查通过：15 个静态权限引用，17 个默认权限 | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_permission_references` |
| 路由保护检查 | OK | 路由保护检查通过：305 个业务 URL 均有登录或权限保护 | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_route_protection` |
| CSRF 表单检查 | OK | CSRF 表单检查通过：108 个 POST 表单均包含 csrf_token | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_csrf_tokens` |
| 导航页面烟测 | OK | 导航页面烟测通过：51 个主导航页面可正常打开，用户 admin | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_navigation_pages` |
| 低频入口烟测 | OK | 低频入口烟测通过：91 个入口可反转，44 个非写入口可访问，47 个导出或对象级入口仅做反转检查，用户 admin | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py check_low_frequency_entrypoints` |
| 迁移一致性检查 | OK | No changes detected | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py makemigrations --check --dry-run` |
| Python 依赖检查 | OK | No broken requirements found. | `D:\XC\2026\ERP\.venv\Scripts\python.exe -m pip check` |
| 业务冒烟测试 | OK | 业务冒烟测试通过：tag=202606260752207F28, operator=admin, 已回滚冒烟数据 | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py business_smoke_test --operator admin` |
| 完整自动测试 | OK | Ran 762 tests in 441.681s | `D:\XC\2026\ERP\.venv\Scripts\python.exe D:\XC\2026\ERP\manage.py test --noinput --verbosity 1` |
