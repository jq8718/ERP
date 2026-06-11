from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from approvals.models import Approval, ApprovalLog
from approvals.models import ApprovalRule
from approvals.services import apply_approval_action
from files.models import Attachment
from notifications.models import SystemMessage


class ApprovalServiceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.submitter = User.objects.create_user(username="submitter", password="x")
        self.approver = User.objects.create_user(username="approver", password="x")
        self.other = User.objects.create_user(username="other", password="x")
        content_type = ContentType.objects.get_for_model(User)
        self.approval = Approval.objects.create(
            approval_no="AP001",
            approval_type="sales_order",
            source_content_type=content_type,
            source_object_id=self.submitter.id,
            source_doc_type="sales_order",
            source_no="SO001",
            source_title="销售订单 SO001",
            current_approver=self.approver,
            submitted_by=self.submitter,
        )

    def test_approve_writes_log_and_notifies_submitter(self):
        result = apply_approval_action(self.approval.id, ApprovalLog.Action.APPROVE, self.approver.id, "同意")

        self.assertTrue(result.success)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.APPROVED)
        self.assertEqual(ApprovalLog.objects.get().action, ApprovalLog.Action.APPROVE)
        self.assertTrue(SystemMessage.objects.filter(receiver=self.submitter).exists())

    def test_transfer_changes_current_approver_and_notifies_target(self):
        result = apply_approval_action(
            self.approval.id,
            ApprovalLog.Action.TRANSFER,
            self.approver.id,
            "转交处理",
            target_user_id=self.other.id,
        )

        self.assertTrue(result.success)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.PENDING)
        self.assertEqual(self.approval.current_approver, self.other)
        self.assertTrue(SystemMessage.objects.filter(receiver=self.other).exists())

    def test_add_approver_returns_to_original_approver_after_added_user_approves(self):
        add_result = apply_approval_action(
            self.approval.id,
            ApprovalLog.Action.ADD_APPROVER,
            self.approver.id,
            "请协助确认价格",
            target_user_id=self.other.id,
        )

        self.assertTrue(add_result.success)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.PENDING)
        self.assertEqual(self.approval.current_approver, self.other)
        self.assertEqual(self.approval.return_to_approver, self.approver)
        self.assertTrue(SystemMessage.objects.filter(receiver=self.other, title__contains="加签").exists())

        approve_result = apply_approval_action(
            self.approval.id,
            ApprovalLog.Action.APPROVE,
            self.other.id,
            "加签同意",
        )

        self.assertTrue(approve_result.success)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.PENDING)
        self.assertEqual(self.approval.current_approver, self.approver)
        self.assertIsNone(self.approval.return_to_approver)
        self.assertTrue(SystemMessage.objects.filter(receiver=self.approver, title__contains="加签已同意").exists())

        final_result = apply_approval_action(self.approval.id, ApprovalLog.Action.APPROVE, self.approver.id, "最终同意")

        self.assertTrue(final_result.success)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.APPROVED)
        self.assertEqual(
            list(ApprovalLog.objects.filter(approval=self.approval).order_by("id").values_list("action", flat=True)),
            [ApprovalLog.Action.ADD_APPROVER, ApprovalLog.Action.APPROVE, ApprovalLog.Action.APPROVE],
        )

    def test_add_approver_requires_target_user(self):
        result = apply_approval_action(
            self.approval.id,
            ApprovalLog.Action.ADD_APPROVER,
            self.approver.id,
            "请协助确认",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "STATE_INVALID_TRANSITION")
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.current_approver, self.approver)

    def test_reject_requires_comment(self):
        result = apply_approval_action(self.approval.id, ApprovalLog.Action.REJECT, self.approver.id)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "APPROVAL_COMMENT_REQUIRED")

    def test_return_to_edit_sets_rejected_and_notifies_submitter(self):
        result = apply_approval_action(
            self.approval.id,
            ApprovalLog.Action.RETURN_TO_EDIT,
            self.approver.id,
            "资料不完整，请修改",
        )

        self.assertTrue(result.success)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.REJECTED)
        self.assertEqual(ApprovalLog.objects.get().action, ApprovalLog.Action.RETURN_TO_EDIT)
        self.assertTrue(SystemMessage.objects.filter(receiver=self.submitter, title__contains="退回修改").exists())

    def test_submitter_can_withdraw_and_audit_remote_context(self):
        result = apply_approval_action(
            self.approval.id,
            ApprovalLog.Action.WITHDRAW,
            self.submitter.id,
            "重新整理资料",
            ip_address="10.0.0.8",
            user_agent="approval-browser",
        )

        self.assertTrue(result.success)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.WITHDRAWN)
        log = ApprovalLog.objects.get()
        self.assertEqual(log.action, ApprovalLog.Action.WITHDRAW)
        self.assertEqual(log.from_approver, self.approver)
        self.assertEqual(log.ip_address, "10.0.0.8")
        self.assertEqual(log.user_agent, "approval-browser")
        self.assertTrue(SystemMessage.objects.filter(receiver=self.approver, title__contains="已撤回").exists())

    def test_approver_cannot_withdraw_submitted_approval(self):
        result = apply_approval_action(self.approval.id, ApprovalLog.Action.WITHDRAW, self.approver.id, "尝试撤回")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "AUTH_NO_PERMISSION")
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.PENDING)


class ApprovalViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.submitter = User.objects.create_user(username="view-submitter", password="x")
        self.approver = User.objects.create_user(username="view-approver", password="x")
        self.other = User.objects.create_user(username="view-other", password="x")
        content_type = ContentType.objects.get_for_model(User)
        self.approval = Approval.objects.create(
            approval_no="APV001",
            approval_type="sales_order",
            source_content_type=content_type,
            source_object_id=self.submitter.id,
            source_doc_type="sales_order",
            source_no="SO-VIEW-001",
            source_title="销售订单 SO-VIEW-001",
            source_summary={"客户": "测试客户", "金额": "100.00"},
            current_approver=self.approver,
            submitted_by=self.submitter,
        )

    def _grant_permission_manage(self, user):
        permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE,
            defaults={
                "permission_name": "权限与审批规则管理",
                "permission_type": Permission.PermissionType.ACTION,
            },
        )
        role = Role.objects.create(role_code=f"admin-{user.id}", role_name="权限管理员")
        role.permissions.add(permission)
        user.roles.add(role)
        return role

    def test_approval_list_links_to_detail(self):
        self.client.force_login(self.approver)

        response = self.client.get("/approvals/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.approval.approval_no)
        self.assertContains(response, f"/approvals/{self.approval.id}/")
        self.assertNotContains(response, "/approvals/rules/")

    def test_approval_list_only_shows_related_approvals(self):
        Approval.objects.create(
            approval_no="APV-OTHER-ONLY",
            approval_type="purchase_order",
            source_content_type=self.approval.source_content_type,
            source_object_id=self.other.id,
            source_doc_type="purchase_order",
            source_no="PO-OTHER-ONLY",
            source_title="采购单 PO-OTHER-ONLY",
            current_approver=self.other,
            submitted_by=self.other,
        )
        self.client.force_login(self.approver)

        response = self.client.get("/approvals/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.approval.approval_no)
        self.assertNotContains(response, "APV-OTHER-ONLY")

    def test_approval_list_permission_manager_can_see_all_approvals(self):
        other_approval = Approval.objects.create(
            approval_no="APV-ADMIN-VIEW",
            approval_type="purchase_order",
            source_content_type=self.approval.source_content_type,
            source_object_id=self.other.id,
            source_doc_type="purchase_order",
            source_no="PO-ADMIN-VIEW",
            source_title="采购单 PO-ADMIN-VIEW",
            current_approver=self.other,
            submitted_by=self.other,
        )
        self._grant_permission_manage(self.approver)
        self.client.force_login(self.approver)

        response = self.client.get("/approvals/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.approval.approval_no)
        self.assertContains(response, other_approval.approval_no)

    def test_approval_rule_button_requires_permission_manage(self):
        self.client.force_login(self.approver)

        response = self.client.get("/approvals/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "审批规则")

        self._grant_permission_manage(self.approver)
        response = self.client.get("/approvals/")

        self.assertContains(response, "审批规则")
        self.assertContains(response, "/approvals/rules/")

    def test_approval_list_filters_by_keyword_and_status(self):
        Approval.objects.create(
            approval_no="APV-HIDE",
            approval_type="purchase_order",
            source_content_type=self.approval.source_content_type,
            source_object_id=self.submitter.id,
            source_doc_type="purchase_order",
            source_no="PO-HIDE",
            source_title="采购单 PO-HIDE",
            current_approver=self.other,
            submitted_by=self.submitter,
            status=Approval.Status.APPROVED,
        )
        self.client.force_login(self.approver)

        response = self.client.get("/approvals/?q=SO-VIEW&status=pending")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.approval.approval_no)
        self.assertNotContains(response, "APV-HIDE")
        self.assertContains(response, "清除")

    def test_approval_detail_renders_summary_and_actions_for_current_approver(self):
        self.client.force_login(self.approver)

        response = self.client.get(f"/approvals/{self.approval.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "审批摘要")
        self.assertContains(response, "测试客户")
        self.assertContains(response, "同意")
        self.assertContains(response, "驳回")
        self.assertContains(response, "转交")
        self.assertContains(response, "退回修改")
        self.assertContains(response, "加签")

    def test_approval_detail_shows_attachment_panel(self):
        Attachment.objects.create(
            attachment_no="ATT-APV-001",
            source_doc_type="approval",
            source_doc_id=self.approval.id,
            source_doc_no=self.approval.approval_no,
            original_filename="approval-note.pdf",
            stored_filename="approval-note.pdf",
            file_path="attachments/approval-note.pdf",
            file_size=100,
            uploaded_by=self.submitter,
        )
        self.client.force_login(self.approver)

        response = self.client.get(f"/approvals/{self.approval.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "approval-note.pdf")
        self.assertContains(response, 'name="source_doc_type" value="approval"')

    def test_approval_detail_hides_actions_for_submitter(self):
        self.client.force_login(self.submitter)

        response = self.client.get(f"/approvals/{self.approval.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "审批操作")
        self.assertContains(response, "撤回审批")
        self.assertContains(response, f"/approvals/{self.approval.id}/withdraw/")

    def test_approval_detail_blocks_unrelated_user(self):
        self.client.force_login(self.other)

        response = self.client.get(f"/approvals/{self.approval.id}/")

        self.assertEqual(response.status_code, 404)

    def test_approval_approve_action_updates_status(self):
        self.client.force_login(self.approver)

        response = self.client.post(f"/approvals/{self.approval.id}/approve/", {"comment": "页面同意"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/approvals/{self.approval.id}/")
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.APPROVED)
        self.assertEqual(ApprovalLog.objects.get(approval=self.approval).comment, "页面同意")

    def test_approval_transfer_action_changes_current_approver(self):
        self.client.force_login(self.approver)

        response = self.client.post(
            f"/approvals/{self.approval.id}/transfer/",
            {"target_user": self.other.id, "comment": "请代审"},
        )

        self.assertEqual(response.status_code, 302)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.PENDING)
        self.assertEqual(self.approval.current_approver, self.other)

    def test_approval_add_approver_action_changes_current_approver_temporarily(self):
        self.client.force_login(self.approver)

        response = self.client.post(
            f"/approvals/{self.approval.id}/add_approver/",
            {"target_user": self.other.id, "comment": "请协助审批"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/approvals/{self.approval.id}/")
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.PENDING)
        self.assertEqual(self.approval.current_approver, self.other)
        self.assertEqual(self.approval.return_to_approver, self.approver)
        log = ApprovalLog.objects.get(approval=self.approval)
        self.assertEqual(log.action, ApprovalLog.Action.ADD_APPROVER)
        self.assertEqual(log.to_approver, self.other)

    def test_approval_return_to_edit_action_rejects_for_revision(self):
        self.client.force_login(self.approver)

        response = self.client.post(
            f"/approvals/{self.approval.id}/return_to_edit/",
            {"comment": "请补充附件后再提交"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/approvals/{self.approval.id}/")
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.REJECTED)
        log = ApprovalLog.objects.get(approval=self.approval)
        self.assertEqual(log.action, ApprovalLog.Action.RETURN_TO_EDIT)
        self.assertEqual(log.comment, "请补充附件后再提交")

    def test_approval_withdraw_action_by_submitter_updates_status_and_log_ip(self):
        self.client.force_login(self.submitter)

        response = self.client.post(
            f"/approvals/{self.approval.id}/withdraw/",
            {"comment": "资料需要重做"},
            HTTP_X_FORWARDED_FOR="10.0.0.9, 10.0.0.1",
            HTTP_USER_AGENT="mobile-approval",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/approvals/{self.approval.id}/")
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.WITHDRAWN)
        log = ApprovalLog.objects.get(approval=self.approval)
        self.assertEqual(log.action, ApprovalLog.Action.WITHDRAW)
        self.assertEqual(log.ip_address, "10.0.0.9")
        self.assertEqual(log.user_agent, "mobile-approval")

    def test_approval_withdraw_action_rejects_current_approver(self):
        self.client.force_login(self.approver)

        response = self.client.post(f"/approvals/{self.approval.id}/withdraw/", {"comment": "审批人不能撤回"})

        self.assertEqual(response.status_code, 302)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.Status.PENDING)
        self.assertFalse(ApprovalLog.objects.filter(approval=self.approval).exists())

    def test_approval_rule_list_and_detail_render(self):
        self._grant_permission_manage(self.approver)
        self.client.force_login(self.approver)
        role = Role.objects.create(role_code="sales_manager", role_name="销售主管")
        rule = ApprovalRule.objects.create(
            doc_type="sales_order",
            condition_json={"min_amount": "1000"},
            level_no=1,
            approver_role=role,
            status=ApprovalRule.RuleStatus.ACTIVE,
            created_by=self.approver,
        )

        list_response = self.client.get("/approvals/rules/")
        detail_response = self.client.get(f"/approvals/rules/{rule.id}/")

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "sales_order")
        self.assertContains(list_response, f"/approvals/rules/{rule.id}/")
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "销售主管")
        self.assertContains(detail_response, "min_amount")

    def test_approval_rule_list_filters_by_keyword_and_status(self):
        self._grant_permission_manage(self.approver)
        self.client.force_login(self.approver)
        role = Role.objects.create(role_code="sales_manager_filter", role_name="销售主管")
        keep_rule = ApprovalRule.objects.create(
            doc_type="sales_order",
            level_no=1,
            approver_role=role,
            status=ApprovalRule.RuleStatus.ACTIVE,
            created_by=self.approver,
        )
        ApprovalRule.objects.create(
            doc_type="purchase_order",
            level_no=1,
            approver_user=self.other,
            status=ApprovalRule.RuleStatus.INACTIVE,
            created_by=self.approver,
        )

        response = self.client.get("/approvals/rules/?q=sales&status=active")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, keep_rule.doc_type)
        self.assertNotContains(response, "purchase_order")
        self.assertContains(response, "清除")

    def test_approval_rule_create_view_creates_rule(self):
        self._grant_permission_manage(self.approver)
        self.client.force_login(self.approver)
        role = Role.objects.create(role_code="finance", role_name="财务")

        response = self.client.post(
            "/approvals/rules/new/",
            {
                "doc_type": "customer_receipt",
                "condition_json": '{"min_amount": "5000"}',
                "level_no": "1",
                "approver_role": role.id,
                "approver_user": "",
                "allow_auto_skip_same_user": "on",
                "require_second_verify": "on",
                "status": ApprovalRule.RuleStatus.ACTIVE,
                "remark": "大额收款审批",
            },
        )

        rule = ApprovalRule.objects.get(doc_type="customer_receipt")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/approvals/rules/{rule.id}/")
        self.assertEqual(rule.approver_role, role)
        self.assertTrue(rule.require_second_verify)
        self.assertEqual(rule.condition_json["min_amount"], "5000")

    def test_approval_rule_create_requires_approver(self):
        self._grant_permission_manage(self.approver)
        self.client.force_login(self.approver)

        response = self.client.post(
            "/approvals/rules/new/",
            {
                "doc_type": "purchase_order",
                "condition_json": "{}",
                "level_no": "1",
                "approver_role": "",
                "approver_user": "",
                "status": ApprovalRule.RuleStatus.ACTIVE,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "审批角色和审批人员至少填写一个")
        self.assertFalse(ApprovalRule.objects.filter(doc_type="purchase_order").exists())

    def test_approval_rule_list_requires_permission(self):
        self.client.force_login(self.approver)

        response = self.client.get("/approvals/rules/")

        self.assertEqual(response.status_code, 403)
