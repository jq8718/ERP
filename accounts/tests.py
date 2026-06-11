from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import override_settings
from django.test import TestCase
from django.utils import timezone

from accounts.models import Permission, Role, User, UserSession
from accounts.permissions import PermissionCode, ensure_default_permissions, user_has_permission
from system.models import AuditLog


class AccountsViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="login-user", password="Secret123!")

    def test_login_page_renders(self):
        response = self.client.get("/login/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ERP 登录")

    def test_login_redirects_to_dashboard(self):
        response = self.client.post("/login/", {"username": "login-user", "password": "Secret123!"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")

    def test_password_change_updates_login_password(self):
        self.client.login(username="login-user", password="Secret123!")

        page_response = self.client.get("/password/change/")
        change_response = self.client.post(
            "/password/change/",
            {
                "old_password": "Secret123!",
                "new_password1": "NewSecret123!",
                "new_password2": "NewSecret123!",
            },
        )
        self.client.logout()
        old_password_login = self.client.login(username="login-user", password="Secret123!")
        new_password_login = self.client.login(username="login-user", password="NewSecret123!")

        self.assertEqual(page_response.status_code, 200)
        self.assertContains(page_response, "修改密码")
        self.assertEqual(change_response.status_code, 302)
        self.assertFalse(old_password_login)
        self.assertTrue(new_password_login)

    def test_inactive_status_user_cannot_login(self):
        self.user.status = User.AccountStatus.INACTIVE
        self.user.save(update_fields=["status"])

        response = self.client.post("/login/", {"username": "login-user", "password": "Secret123!"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "账号已停用、锁定或删除")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_locked_user_cannot_login(self):
        self.user.status = User.AccountStatus.LOCKED
        self.user.save(update_fields=["status"])

        response = self.client.post("/login/", {"username": "login-user", "password": "Secret123!"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "账号已停用、锁定或删除")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_deleted_user_cannot_login(self):
        self.user.is_deleted = True
        self.user.save(update_fields=["is_deleted"])

        response = self.client.post("/login/", {"username": "login-user", "password": "Secret123!"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "账号已停用、锁定或删除")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_authenticated_request_creates_user_session_record(self):
        self.client.login(username="login-user", password="Secret123!")

        response = self.client.get("/")

        session = UserSession.objects.get(user=self.user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.status, UserSession.SessionStatus.ACTIVE)
        self.assertTrue(session.session_key)
        self.assertIsNotNone(session.last_seen_at)

    def test_revoked_user_session_is_logged_out(self):
        self.client.login(username="login-user", password="Secret123!")
        self.client.get("/")
        session = UserSession.objects.get(user=self.user)
        session.status = UserSession.SessionStatus.REVOKED
        session.revoked_at = timezone.now()
        session.save(update_fields=["status", "revoked_at"])

        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_user_session_list_requires_permission(self):
        self.client.force_login(self.user)

        response = self.client.get("/user-sessions/")

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "缺少管理登录会话权限", status_code=403)

    def test_user_session_list_renders_for_permission_manager(self):
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        self.client.force_login(self.user)
        self.client.get("/")

        response = self.client.get("/user-sessions/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "登录会话")
        self.assertContains(response, "login-user")

    def test_account_management_lists_require_permission(self):
        self.client.force_login(self.user)

        for path in ["/users/", "/roles/", "/permissions/"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403)

    def test_django_admin_requires_superuser_not_just_staff(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.client.force_login(self.user)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_django_admin_allows_active_superuser(self):
        superuser = get_user_model().objects.create_superuser(username="root-admin", password="Secret123!")
        self.client.force_login(superuser)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Django 管理")

    def test_account_management_lists_render_for_permission_manager(self):
        permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.SALES_PROCESS,
            defaults={"permission_name": "处理销售单据", "permission_type": Permission.PermissionType.ACTION},
        )
        role = _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        role.permissions.add(permission)
        self.client.force_login(self.user)

        user_response = self.client.get("/users/?q=login")
        role_response = self.client.get("/roles/?q=admin.permission_manage")
        permission_response = self.client.get("/permissions/?q=sales.process")

        self.assertEqual(user_response.status_code, 200)
        self.assertContains(user_response, "用户管理")
        self.assertContains(user_response, "login-user")
        self.assertEqual(role_response.status_code, 200)
        self.assertContains(role_response, "角色管理")
        self.assertContains(role_response, role.role_code)
        self.assertEqual(permission_response.status_code, 200)
        self.assertContains(permission_response, "权限清单")
        self.assertContains(permission_response, "sales.process")

    def test_account_user_and_role_details_show_effective_permissions(self):
        role = _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        sales_permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.SALES_PROCESS,
            defaults={"permission_name": "处理销售单据", "permission_type": Permission.PermissionType.ACTION},
        )
        role.permissions.add(sales_permission)
        UserSession.objects.create(user=self.user, session_key="detail-session")
        self.client.force_login(self.user)

        user_response = self.client.get(f"/users/{self.user.id}/")
        role_response = self.client.get(f"/roles/{role.id}/")

        self.assertEqual(user_response.status_code, 200)
        self.assertContains(user_response, "有效权限")
        self.assertContains(user_response, "sales.process")
        self.assertContains(user_response, "最近会话")
        self.assertContains(user_response, "有效")
        self.assertEqual(role_response.status_code, 200)
        self.assertContains(role_response, "角色详情")
        self.assertContains(role_response, "login-user")
        self.assertContains(role_response, "sales.process")

    def test_permission_manager_can_create_and_update_user(self):
        admin_role = _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        sales_permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.SALES_PROCESS,
            defaults={"permission_name": "处理销售单据", "permission_type": Permission.PermissionType.ACTION},
        )
        sales_role = Role.objects.create(role_code="sales-operator", role_name="销售处理")
        sales_role.permissions.add(sales_permission)
        self.client.login(username="login-user", password="Secret123!")

        create_response = self.client.post(
            "/users/create/",
            {
                "username": "new-user",
                "display_name": "新用户",
                "email": "new@example.com",
                "department": "销售部",
                "position": "销售",
                "security_level": User.SecurityLevel.L1,
                "status": User.AccountStatus.ACTIVE,
                "is_active": "on",
                "roles": [sales_role.id],
                "password1": "NewUser123!",
                "password2": "NewUser123!",
                "reason": "新增销售账号",
                "current_password": "Secret123!",
            },
        )
        created_user = User.objects.get(username="new-user")
        update_response = self.client.post(
            f"/users/{created_user.id}/edit/",
            {
                "display_name": "新用户二",
                "email": "new2@example.com",
                "department": "销售二部",
                "position": "主管",
                "security_level": User.SecurityLevel.L2,
                "status": User.AccountStatus.ACTIVE,
                "is_active": "on",
                "roles": [admin_role.id, sales_role.id],
                "reason": "调整角色",
                "current_password": "Secret123!",
            },
        )

        created_user.refresh_from_db()
        self.assertEqual(create_response.status_code, 302)
        self.assertTrue(created_user.check_password("NewUser123!"))
        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(created_user.display_name, "新用户二")
        self.assertTrue(created_user.roles.filter(id=admin_role.id).exists())
        self.assertTrue(AuditLog.objects.filter(action="account_user_create", source_doc_id=created_user.id).exists())
        self.assertTrue(AuditLog.objects.filter(action="account_user_update", source_doc_id=created_user.id).exists())

    def test_permission_manager_can_create_and_update_role(self):
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        sales_permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.SALES_PROCESS,
            defaults={"permission_name": "处理销售单据", "permission_type": Permission.PermissionType.ACTION},
        )
        purchase_permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.PURCHASE_PROCESS,
            defaults={"permission_name": "处理采购单据", "permission_type": Permission.PermissionType.ACTION},
        )
        self.client.login(username="login-user", password="Secret123!")

        create_response = self.client.post(
            "/roles/create/",
            {
                "role_code": "ops-role",
                "role_name": "运营角色",
                "status": Role.RoleStatus.ACTIVE,
                "permissions": [sales_permission.id],
                "remark": "初始",
                "reason": "新增运营角色",
                "current_password": "Secret123!",
            },
        )
        role = Role.objects.get(role_code="ops-role")
        update_response = self.client.post(
            f"/roles/{role.id}/edit/",
            {
                "role_code": "ops-role",
                "role_name": "运营角色二",
                "status": Role.RoleStatus.ACTIVE,
                "permissions": [sales_permission.id, purchase_permission.id],
                "remark": "追加采购权限",
                "reason": "追加采购权限",
                "current_password": "Secret123!",
            },
        )

        role.refresh_from_db()
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(role.role_name, "运营角色二")
        self.assertTrue(role.permissions.filter(permission_code=PermissionCode.PURCHASE_PROCESS).exists())
        self.assertTrue(AuditLog.objects.filter(action="role_create", source_doc_id=role.id).exists())
        self.assertTrue(AuditLog.objects.filter(action="role_update", source_doc_id=role.id).exists())

    def test_permission_manager_user_create_requires_current_password(self):
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        self.client.login(username="login-user", password="Secret123!")

        response = self.client.post(
            "/users/create/",
            {
                "username": "blocked-user",
                "display_name": "阻止用户",
                "security_level": User.SecurityLevel.L1,
                "status": User.AccountStatus.ACTIVE,
                "is_active": "on",
                "password1": "NewUser123!",
                "password2": "NewUser123!",
                "reason": "测试",
                "current_password": "Wrong!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前登录密码不正确")
        self.assertFalse(User.objects.filter(username="blocked-user").exists())

    def test_permission_manager_can_reset_user_password_and_revoke_sessions(self):
        admin = get_user_model().objects.create_user(username="password-admin", password="Secret123!")
        target = get_user_model().objects.create_user(username="password-target", password="OldSecret123!")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)
        UserSession.objects.create(user=target, session_key="target-active-session")
        self.client.login(username="password-admin", password="Secret123!")

        page_response = self.client.get(f"/users/{target.id}/password-reset/")
        reset_response = self.client.post(
            f"/users/{target.id}/password-reset/",
            {
                "new_password1": "NewSecret123!",
                "new_password2": "NewSecret123!",
                "reason": "账号疑似泄露",
                "current_password": "Secret123!",
            },
        )

        target.refresh_from_db()
        self.client.logout()
        old_password_login = self.client.login(username="password-target", password="OldSecret123!")
        new_password_login = self.client.login(username="password-target", password="NewSecret123!")
        target_session = UserSession.objects.get(session_key="target-active-session")
        audit_log = AuditLog.objects.get(action="account_user_password_reset")
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(reset_response.status_code, 302)
        self.assertFalse(old_password_login)
        self.assertTrue(new_password_login)
        self.assertEqual(target_session.status, UserSession.SessionStatus.REVOKED)
        self.assertEqual(audit_log.after_snapshot["reason"], "账号疑似泄露")

    def test_permission_manager_password_reset_requires_current_password(self):
        admin = get_user_model().objects.create_user(username="password-admin-deny", password="Secret123!")
        target = get_user_model().objects.create_user(username="password-target-deny", password="OldSecret123!")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)
        self.client.login(username="password-admin-deny", password="Secret123!")

        response = self.client.post(
            f"/users/{target.id}/password-reset/",
            {
                "new_password1": "NewSecret123!",
                "new_password2": "NewSecret123!",
                "reason": "测试错误密码",
                "current_password": "Wrong!",
            },
        )

        target.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前登录密码不正确")
        self.assertTrue(target.check_password("OldSecret123!"))

    def test_permission_manager_cannot_remove_own_last_admin_role(self):
        admin_role = _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        sales_role = Role.objects.create(role_code="sales-role-self", role_name="销售角色")
        self.user.roles.add(sales_role)
        self.client.login(username="login-user", password="Secret123!")

        response = self.client.post(
            f"/users/{self.user.id}/edit/",
            {
                "display_name": "自锁测试",
                "email": "",
                "department": "",
                "position": "",
                "security_level": User.SecurityLevel.L1,
                "status": User.AccountStatus.ACTIVE,
                "is_active": "on",
                "roles": [sales_role.id],
                "reason": "误移除权限",
                "current_password": "Secret123!",
            },
        )

        self.user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "不能移除自己的最后一个权限管理角色")
        self.assertTrue(self.user.roles.filter(id=admin_role.id).exists())

    def test_permission_manager_cannot_disable_own_last_admin_role(self):
        admin_role = _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        admin_permission = Permission.objects.get(permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE)
        self.client.login(username="login-user", password="Secret123!")

        response = self.client.post(
            f"/roles/{admin_role.id}/edit/",
            {
                "role_code": admin_role.role_code,
                "role_name": admin_role.role_name,
                "status": Role.RoleStatus.INACTIVE,
                "permissions": [admin_permission.id],
                "remark": "误停用",
                "reason": "误停用",
                "current_password": "Secret123!",
            },
        )

        admin_role.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "不能停用自己最后一个权限管理角色")
        self.assertEqual(admin_role.status, Role.RoleStatus.ACTIVE)

    def test_user_session_detail_can_revoke_other_active_session(self):
        admin = get_user_model().objects.create_user(username="session-admin", password="Secret123!")
        target = get_user_model().objects.create_user(username="session-target", password="Secret123!")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)
        target_session = UserSession.objects.create(
            user=target,
            session_key="target-session-key",
            ip_address="127.0.0.1",
            user_agent="Target Browser",
            last_seen_at=timezone.now(),
        )
        self.client.login(username="session-admin", password="Secret123!")
        self.client.get("/")

        response = self.client.post(
            f"/user-sessions/{target_session.id}/revoke/",
            {"current_password": "Secret123!", "reason": "离职账号安全处理"},
        )

        target_session.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/user-sessions/{target_session.id}/")
        self.assertEqual(target_session.status, UserSession.SessionStatus.REVOKED)
        self.assertIsNotNone(target_session.revoked_at)
        audit_log = AuditLog.objects.get(action="user_session_revoke")
        self.assertEqual(audit_log.source_doc_id, target_session.id)
        self.assertEqual(audit_log.after_snapshot["reason"], "离职账号安全处理")

    def test_user_session_revoke_requires_current_password_and_reason(self):
        admin = get_user_model().objects.create_user(username="session-admin-verify", password="Secret123!")
        target = get_user_model().objects.create_user(username="session-target-verify", password="Secret123!")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)
        target_session = UserSession.objects.create(user=target, session_key="target-session-verify")
        self.client.login(username="session-admin-verify", password="Secret123!")

        bad_password_response = self.client.post(
            f"/user-sessions/{target_session.id}/revoke/",
            {"current_password": "Wrong!", "reason": "安全处理"},
        )
        missing_reason_response = self.client.post(
            f"/user-sessions/{target_session.id}/revoke/",
            {"current_password": "Secret123!", "reason": ""},
        )

        target_session.refresh_from_db()
        self.assertEqual(bad_password_response.status_code, 302)
        self.assertEqual(missing_reason_response.status_code, 302)
        self.assertEqual(target_session.status, UserSession.SessionStatus.ACTIVE)

    def test_user_session_revoke_cannot_revoke_current_session(self):
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        self.client.login(username="login-user", password="Secret123!")
        self.client.get("/")
        current_session = UserSession.objects.get(user=self.user)

        response = self.client.post(
            f"/user-sessions/{current_session.id}/revoke/",
            {"current_password": "Secret123!", "reason": "误操作测试"},
        )

        current_session.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(current_session.status, UserSession.SessionStatus.ACTIVE)


class PermissionHelperTests(TestCase):
    def setUp(self):
        UserModel = get_user_model()
        self.user = UserModel.objects.create_user(username="permission-user", password="x")
        self.permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE,
            defaults={
                "permission_name": "权限与审批规则管理",
                "permission_type": Permission.PermissionType.ACTION,
            },
        )

    def test_user_has_permission_via_active_role(self):
        role = Role.objects.create(role_code="admin-role", role_name="权限管理员")
        role.permissions.add(self.permission)
        self.user.roles.add(role)

        self.assertTrue(user_has_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE))

    def test_inactive_user_has_no_permission(self):
        role = Role.objects.create(role_code="admin-role", role_name="权限管理员")
        role.permissions.add(self.permission)
        self.user.roles.add(role)
        self.user.status = User.AccountStatus.INACTIVE
        self.user.save(update_fields=["status"])

        self.assertFalse(user_has_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE))

    def test_superuser_has_all_erp_permissions(self):
        superuser = get_user_model().objects.create_superuser(username="root", password="x")

        self.assertTrue(user_has_permission(superuser, "any.permission"))

    def test_ensure_default_permissions_creates_sales_scope_permission(self):
        ensure_default_permissions()

        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.SALES_VIEW_ALL).exists())

    def test_ensure_default_permissions_creates_personal_info_permission(self):
        ensure_default_permissions()

        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO).exists())

    def test_ensure_default_permissions_creates_finance_payment_process_permission(self):
        ensure_default_permissions()

        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.FINANCE_PAYMENT_PROCESS).exists())

    def test_ensure_default_permissions_creates_business_process_permissions(self):
        ensure_default_permissions()

        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.SALES_PROCESS).exists())
        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.BOM_PROCESS).exists())
        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.PURCHASE_PROCESS).exists())
        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.INVENTORY_PROCESS).exists())
        self.assertTrue(Permission.objects.filter(permission_code=PermissionCode.PRODUCTION_PROCESS).exists())


class BootstrapAdminCommandTests(TestCase):
    def test_bootstrap_admin_creates_superuser_and_permission_admin_role(self):
        output = StringIO()

        call_command(
            "bootstrap_admin",
            username="bootstrap-admin",
            password="StrongSecret123!",
            email="admin@example.com",
            display_name="上线管理员",
            stdout=output,
        )

        user = get_user_model().objects.get(username="bootstrap-admin")
        role = Role.objects.get(role_code="permission-admin")
        self.assertEqual(user.email, "admin@example.com")
        self.assertEqual(user.display_name, "上线管理员")
        self.assertEqual(user.security_level, User.SecurityLevel.L4)
        self.assertEqual(user.status, User.AccountStatus.ACTIVE)
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("StrongSecret123!"))
        self.assertTrue(user.roles.filter(id=role.id).exists())
        self.assertTrue(role.permissions.filter(permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE).exists())
        self.assertNotIn("StrongSecret123!", output.getvalue())

    def test_bootstrap_admin_is_idempotent_without_password_on_existing_user(self):
        call_command("bootstrap_admin", username="bootstrap-admin", password="StrongSecret123!")
        output = StringIO()

        call_command("bootstrap_admin", username="bootstrap-admin", stdout=output)

        user = get_user_model().objects.get(username="bootstrap-admin")
        self.assertTrue(user.check_password("StrongSecret123!"))
        self.assertEqual(Role.objects.filter(role_code="permission-admin").count(), 1)
        self.assertEqual(Permission.objects.filter(permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE).count(), 1)
        self.assertIn("password unchanged", output.getvalue())

    def test_bootstrap_admin_does_not_overwrite_existing_password_by_default(self):
        get_user_model().objects.create_user(username="existing-admin", password="OldSecret123!")

        call_command("bootstrap_admin", username="existing-admin", password="NewSecret123!")

        user = get_user_model().objects.get(username="existing-admin")
        self.assertTrue(user.check_password("OldSecret123!"))
        self.assertFalse(user.check_password("NewSecret123!"))
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.roles.filter(role_code="permission-admin").exists())

    def test_bootstrap_admin_resets_existing_password_when_explicit(self):
        get_user_model().objects.create_user(username="existing-admin", password="OldSecret123!")

        call_command(
            "bootstrap_admin",
            username="existing-admin",
            password="NewSecret123!",
            reset_password=True,
        )

        user = get_user_model().objects.get(username="existing-admin")
        self.assertFalse(user.check_password("OldSecret123!"))
        self.assertTrue(user.check_password("NewSecret123!"))

    @override_settings(IS_PRODUCTION=True)
    def test_bootstrap_admin_rejects_short_production_password(self):
        with self.assertRaises(CommandError):
            call_command("bootstrap_admin", username="bootstrap-admin", password="Short123!")

    def test_bootstrap_admin_check_only_passes_after_initialization(self):
        call_command("bootstrap_admin", username="bootstrap-admin", password="StrongSecret123!")
        output = StringIO()

        call_command("bootstrap_admin", username="bootstrap-admin", check_only=True, stdout=output)

        self.assertIn("Bootstrap admin check passed", output.getvalue())

    def test_bootstrap_admin_check_only_fails_without_initialization(self):
        with self.assertRaises(CommandError) as context:
            call_command("bootstrap_admin", username="missing-admin", check_only=True)

        self.assertIn("初始化检查未通过", str(context.exception))
        self.assertFalse(User.objects.filter(username="missing-admin").exists())


def _grant_permission(user, permission_code: str):
    permission, _ = Permission.objects.get_or_create(
        permission_code=permission_code,
        defaults={"permission_name": permission_code, "permission_type": Permission.PermissionType.ACTION},
    )
    role = Role.objects.create(role_code=f"role-{permission_code}-{user.id}", role_name=permission_code)
    role.permissions.add(permission)
    user.roles.add(role)
    return role
