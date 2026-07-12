from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from bom.models import Bom, BomItem
from files.models import ExportLog
from masterdata.models import Material
from system.models import AuditLog


class BomViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bom-user", password="x")
        self.finished = Material.objects.create(
            material_code="FG001",
            material_name="成品 1",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        self.raw = Material.objects.create(
            material_code="RM001",
            material_name="原料 1",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
        )
        self._grant_permission(PermissionCode.BOM_VIEW)

    def _grant_permission(self, permission_code: str):
        permission_type = Permission.PermissionType.MODULE if permission_code == PermissionCode.BOM_VIEW else Permission.PermissionType.ACTION
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_type,
            },
        )
        role = Role.objects.create(role_code=f"bom-role-{permission_code}-{self.user.id}", role_name=permission_code)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role

    def _bom_form_data(self, **overrides):
        data = {
            "bom_no": "BOM001",
            "finished_material_code": self.finished.material_code,
            "finished_material_name": self.finished.material_name,
            "finished_material_spec": self.finished.spec,
            "finished_material_base_unit": self.finished.base_unit,
            "finished_material_qty_precision": str(self.finished.qty_precision),
            "bom_version": "V1",
            "base_qty": "1",
            "effective_date": "",
            "expiry_date": "",
            "remark": "页面创建",
        }
        data.update(overrides)
        return data

    def test_bom_list_renders(self):
        self.client.force_login(self.user)

        response = self.client.get("/bom/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "产品组成清单")
        self.assertContains(response, "导出CSV")
        self.assertNotContains(response, "/bom/new/")

    def test_bom_list_shows_create_action_with_bom_process_permission(self):
        self._grant_permission(PermissionCode.BOM_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/bom/")

        self.assertContains(response, "/bom/new/")

    def test_bom_list_filter_and_export_share_query(self):
        keep = Bom.objects.create(
            bom_no="BOM-FILTER-KEEP",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.ENABLED,
            created_by=self.user,
        )
        Bom.objects.create(
            bom_no="BOM-FILTER-HIDE",
            finished_material=self.finished,
            bom_version="V2",
            base_qty="1",
            status=Bom.BomStatus.DISABLED,
            created_by=self.user,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/bom/?q=KEEP&status=enabled")
        export_response = self.client.get("/bom/export/?q=KEEP&status=enabled")
        content = _streaming_text(export_response)

        self.assertContains(list_response, keep.bom_no)
        self.assertNotContains(list_response, "BOM-FILTER-HIDE")
        self.assertContains(list_response, "/bom/export/?q=KEEP&amp;status=enabled")
        self.assertIn("BOM-FILTER-KEEP", content)
        self.assertNotIn("BOM-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="boms")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "enabled")

    def test_bom_create_and_detail_views(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)

        response = self.client.post(
            "/bom/new/",
            self._bom_form_data(is_default="on"),
        )

        bom = Bom.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")
        self.assertEqual(bom.created_by, self.user)
        self.assertEqual(bom.status, Bom.BomStatus.DRAFT)
        self.assertFalse(bom.is_default)
        self.finished.refresh_from_db()
        self.assertEqual(bom.finished_material, self.finished)
        audit_log = AuditLog.objects.get(action="bom_create")
        self.assertEqual(audit_log.source_doc_id, bom.id)
        self.assertEqual(audit_log.after_snapshot["bom_no"], "BOM001")
        detail_response = self.client.get(f"/bom/{bom.id}/")
        self.assertContains(detail_response, "BOM001")
        self.assertContains(detail_response, self.finished.material_code)

    def test_bom_create_can_create_finished_material_and_items(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)

        response = self.client.post(
            "/bom/new/",
            self._bom_form_data(
                bom_no="BOM-NEW-FG",
                finished_material_code="FG-NEW",
                finished_material_name="新成品",
                finished_material_spec="双9V电源板",
                finished_material_base_unit="pcs",
                **{
                    "items-TOTAL_FORMS": "1",
                    "items-INITIAL_FORMS": "0",
                    "items-MIN_NUM_FORMS": "0",
                    "items-MAX_NUM_FORMS": "1000",
                    "items-0-line_no": "1",
                    "items-0-component_material": self.raw.id,
                    "items-0-usage_qty": "2",
                    "items-0-usage_unit": "pcs",
                    "items-0-loss_rate": "0",
                    "items-0-is_required": "on",
                    "items-0-remark": "主原料",
                },
            ),
        )

        bom = Bom.objects.get(bom_no="BOM-NEW-FG")
        finished = Material.objects.get(material_code="FG-NEW")
        item = bom.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")
        self.assertEqual(finished.material_type, Material.MaterialType.FINISHED)
        self.assertEqual(finished.material_name, "新成品")
        self.assertEqual(finished.spec, "双9V电源板")
        self.assertEqual(bom.finished_material, finished)
        self.assertEqual(item.component_material, self.raw)
        self.assertEqual(item.usage_unit, "pcs")
        audit_log = AuditLog.objects.get(action="bom_create")
        self.assertEqual(audit_log.after_snapshot["finished_material_code"], "FG-NEW")
        self.assertEqual(len(audit_log.after_snapshot["items"]), 1)

    def test_bom_create_rejects_non_positive_base_qty(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)

        response = self.client.post(
            "/bom/new/",
            self._bom_form_data(bom_no="BOM-ZERO", base_qty="0", remark="错误基准数量"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BOM 基准数量必须大于 0")
        self.assertFalse(Bom.objects.filter(bom_no="BOM-ZERO").exists())

    def test_bom_date_fields_use_calendar_inputs(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)

        create_response = self.client.get("/bom/new/")

        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, 'type="date" name="effective_date"')
        self.assertContains(create_response, 'type="date" name="expiry_date"')

        bom = Bom.objects.create(
            bom_no="BOM-DATE",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            effective_date=date(2026, 7, 1),
            expiry_date=date(2026, 12, 31),
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )

        edit_response = self.client.get(f"/bom/{bom.id}/edit/")

        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, 'type="date" name="effective_date" value="2026-07-01"')
        self.assertContains(edit_response, 'type="date" name="expiry_date" value="2026-12-31"')

    def test_bom_date_fields_accept_common_manual_formats(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)

        response = self.client.post(
            "/bom/new/",
            self._bom_form_data(
                bom_no="BOM-DATE-MANUAL",
                effective_date="2026/7/4",
                expiry_date="2026年12月31日",
                remark="兼容手工日期",
            ),
        )

        self.assertEqual(response.status_code, 302)
        bom = Bom.objects.get(bom_no="BOM-DATE-MANUAL")
        self.assertEqual(bom.effective_date, date(2026, 7, 4))
        self.assertEqual(bom.expiry_date, date(2026, 12, 31))

    def test_bom_create_rejects_expiry_before_effective_date(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)

        response = self.client.post(
            "/bom/new/",
            self._bom_form_data(
                bom_no="BOM-DATE-RANGE",
                effective_date="2026-07-05",
                expiry_date="2026-07-04",
                remark="日期倒挂",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "失效日期不能早于生效日期")
        self.assertFalse(Bom.objects.filter(bom_no="BOM-DATE-RANGE").exists())

    def test_bom_edit_updates_draft_header(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )

        response = self.client.post(
            f"/bom/{bom.id}/edit/",
            {
                "bom_no": "BOM001-EDIT",
                "finished_material_code": self.finished.material_code,
                "finished_material_name": "成品 1 改",
                "finished_material_spec": "新版规格",
                "finished_material_base_unit": "pcs",
                "finished_material_qty_precision": "0",
                "bom_version": "V1A",
                "base_qty": "2",
                "effective_date": "",
                "expiry_date": "",
                "remark": "已编辑",
                "operation_reason": "修正 BOM 头信息",
            },
        )

        bom.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")
        self.assertEqual(bom.bom_no, "BOM001-EDIT")
        self.assertEqual(bom.bom_version, "V1A")
        self.assertEqual(str(bom.base_qty), "2.0000")
        self.assertEqual(bom.remark, "已编辑")
        self.assertEqual(bom.updated_by, self.user)
        self.assertEqual(bom.version, 2)
        self.finished.refresh_from_db()
        self.assertEqual(self.finished.material_name, "成品 1 改")
        self.assertEqual(self.finished.spec, "新版规格")
        audit_log = AuditLog.objects.get(action="bom_update")
        self.assertEqual(audit_log.before_snapshot["bom_no"], "BOM001")
        self.assertEqual(audit_log.after_snapshot["bom_no"], "BOM001-EDIT")
        self.assertEqual(audit_log.after_snapshot["reason"], "修正 BOM 头信息")

    def test_bom_edit_rejects_enabled_bom(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.ENABLED,
            created_by=self.user,
        )

        response = self.client.get(f"/bom/{bom.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")

    def test_bom_item_create_view_adds_component(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )

        response = self.client.post(
            f"/bom/{bom.id}/items/new/",
            {
                "line_no": "1",
                "component_material": self.raw.id,
                "usage_qty": "2.5",
                "usage_unit": "pcs",
                "loss_rate": "0.02",
                "is_required": "on",
                "remark": "页面新增",
                "operation_reason": "新增原料子件",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")
        item = BomItem.objects.get()
        bom.refresh_from_db()
        self.assertEqual(item.component_material, self.raw)
        self.assertEqual(item.usage_unit, "pcs")
        self.assertEqual(bom.updated_by, self.user)
        self.assertEqual(bom.version, 2)
        audit_log = AuditLog.objects.get(action="bom_item_create")
        self.assertEqual(audit_log.after_snapshot["component_material_code"], self.raw.material_code)
        self.assertEqual(audit_log.after_snapshot["reason"], "新增原料子件")

    def test_bom_enable_requires_items(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )

        response = self.client.post(f"/bom/{bom.id}/enable/", {"current_password": "x", "reason": "启用测试"})

        bom.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")
        self.assertEqual(bom.status, Bom.BomStatus.DRAFT)

    def test_bom_enable_rejects_invalid_base_qty(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM-BAD-BASE",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="0",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        response = self.client.post(f"/bom/{bom.id}/enable/", {"current_password": "x", "reason": "启用测试"})

        bom.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bom.status, Bom.BomStatus.DRAFT)

    def test_bom_enable_and_disable_actions(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        old_bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.ENABLED,
            is_default=True,
            enabled_at=timezone.now(),
            created_by=self.user,
        )
        new_bom = Bom.objects.create(
            bom_no="BOM002",
            finished_material=self.finished,
            bom_version="V2",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        BomItem.objects.create(
            bom=new_bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        missing_reason_response = self.client.post(f"/bom/{new_bom.id}/enable/", {"current_password": "x"})
        new_bom.refresh_from_db()
        self.assertEqual(missing_reason_response.status_code, 302)
        self.assertEqual(new_bom.status, Bom.BomStatus.DRAFT)

        enable_response = self.client.post(f"/bom/{new_bom.id}/enable/", {"current_password": "x", "reason": "新版测试通过"})

        old_bom.refresh_from_db()
        new_bom.refresh_from_db()
        self.assertEqual(enable_response.status_code, 302)
        self.assertEqual(new_bom.status, Bom.BomStatus.ENABLED)
        self.assertTrue(new_bom.is_default)
        self.assertFalse(old_bom.is_default)
        self.assertEqual(new_bom.approved_by, self.user)
        self.assertIsNotNone(new_bom.enabled_at)
        enable_log = AuditLog.objects.get(action="bom_enable")
        self.assertEqual(enable_log.source_doc_id, new_bom.id)
        self.assertEqual(enable_log.after_snapshot["status"], Bom.BomStatus.ENABLED)
        self.assertEqual(enable_log.after_snapshot["reason"], "新版测试通过")

        missing_disable_reason_response = self.client.post(f"/bom/{new_bom.id}/disable/", {"current_password": "x"})
        new_bom.refresh_from_db()
        self.assertEqual(missing_disable_reason_response.status_code, 302)
        self.assertEqual(new_bom.status, Bom.BomStatus.ENABLED)

        disable_response = self.client.post(f"/bom/{new_bom.id}/disable/", {"current_password": "x", "reason": "旧版停用"})

        new_bom.refresh_from_db()
        self.assertEqual(disable_response.status_code, 302)
        self.assertEqual(new_bom.status, Bom.BomStatus.DISABLED)
        self.assertFalse(new_bom.is_default)
        self.assertIsNotNone(new_bom.disabled_at)
        disable_log = AuditLog.objects.get(action="bom_disable")
        self.assertEqual(disable_log.source_doc_id, new_bom.id)
        self.assertEqual(disable_log.after_snapshot["status"], Bom.BomStatus.DISABLED)
        self.assertEqual(disable_log.after_snapshot["reason"], "旧版停用")

    def test_bom_item_create_rejects_duplicate_line(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        response = self.client.post(
            f"/bom/{bom.id}/items/new/",
            {
                "line_no": "1",
                "component_material": self.raw.id,
                "usage_qty": "2",
                "usage_unit": "pcs",
                "loss_rate": "0",
                "is_required": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(BomItem.objects.filter(bom=bom).count(), 1)

    def test_bom_copy_version_creates_draft_with_items(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.ENABLED,
            is_default=True,
            created_by=self.user,
        )
        BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1.5",
            usage_unit="pcs",
            loss_rate="0.010000",
            remark="原版本明细",
        )

        response = self.client.post(
            f"/bom/{bom.id}/copy-version/",
            {"new_bom_no": "BOM002", "new_bom_version": "V2", "operation_reason": "客户要求改版"},
        )

        copied = Bom.objects.get(bom_no="BOM002")
        copied_item = copied.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{copied.id}/")
        self.assertEqual(copied.status, Bom.BomStatus.DRAFT)
        self.assertFalse(copied.is_default)
        self.assertEqual(copied.finished_material, self.finished)
        self.assertEqual(copied.created_by, self.user)
        self.assertEqual(copied_item.component_material, self.raw)
        self.assertEqual(copied_item.usage_unit, "pcs")
        audit_log = AuditLog.objects.get(action="bom_copy_version")
        self.assertEqual(audit_log.source_doc_id, copied.id)
        self.assertEqual(audit_log.before_snapshot["bom_no"], "BOM001")
        self.assertEqual(audit_log.after_snapshot["bom_no"], "BOM002")
        self.assertEqual(audit_log.after_snapshot["reason"], "客户要求改版")

    def test_bom_item_edit_updates_draft_item(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        item = BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        response = self.client.post(
            f"/bom/{bom.id}/items/{item.id}/edit/",
            {
                "line_no": "2",
                "component_material": self.raw.id,
                "usage_qty": "3.25",
                "usage_unit": "kg",
                "loss_rate": "0.03",
                "remark": "已调整",
                "operation_reason": "用量单位修正",
            },
        )

        item.refresh_from_db()
        bom.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")
        self.assertEqual(item.line_no, 2)
        self.assertEqual(str(item.usage_qty), "3.250000")
        self.assertEqual(item.usage_unit, "kg")
        self.assertFalse(item.is_required)
        self.assertEqual(item.remark, "已调整")
        self.assertEqual(bom.updated_by, self.user)
        self.assertEqual(bom.version, 2)
        audit_log = AuditLog.objects.get(action="bom_item_update")
        self.assertEqual(audit_log.before_snapshot["line_no"], 1)
        self.assertEqual(audit_log.after_snapshot["line_no"], 2)
        self.assertEqual(audit_log.after_snapshot["reason"], "用量单位修正")

    def test_bom_item_delete_rejects_enabled_bom(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.ENABLED,
            created_by=self.user,
        )
        item = BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        response = self.client.post(
            f"/bom/{bom.id}/items/{item.id}/delete/",
            {"current_password": "x", "reason": "启用状态测试"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(BomItem.objects.filter(id=item.id).count(), 1)

    def test_bom_item_delete_requires_second_verify_and_reason(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        item = BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        bad_password_response = self.client.post(
            f"/bom/{bom.id}/items/{item.id}/delete/",
            {"current_password": "wrong", "reason": "删除测试"},
        )
        missing_reason_response = self.client.post(
            f"/bom/{bom.id}/items/{item.id}/delete/",
            {"current_password": "x", "reason": ""},
        )

        self.assertEqual(bad_password_response.status_code, 302)
        self.assertEqual(missing_reason_response.status_code, 302)
        self.assertTrue(BomItem.objects.filter(id=item.id).exists())

    def test_bom_item_delete_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        item = BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        response = self.client.post(
            f"/bom/{bom.id}/items/{item.id}/delete/",
            {"current_password": "x", "reason": "删除错误子件"},
        )

        bom.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(BomItem.objects.filter(id=item.id).exists())
        self.assertEqual(bom.version, 2)
        audit_log = AuditLog.objects.get(action="bom_item_delete")
        self.assertEqual(audit_log.source_doc_id, bom.id)
        self.assertEqual(audit_log.before_snapshot["component_material_code"], self.raw.material_code)
        self.assertEqual(audit_log.after_snapshot["reason"], "删除错误子件")

    def test_bom_process_actions_require_bom_process_permission(self):
        self.client.force_login(self.user)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        item = BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        responses = [
            self.client.get("/bom/new/"),
            self.client.post(
                "/bom/new/",
                self._bom_form_data(bom_no="BOM002", bom_version="V2", remark="无权限创建"),
            ),
            self.client.get(f"/bom/{bom.id}/edit/"),
            self.client.post(f"/bom/{bom.id}/items/new/", {"line_no": "2", "component_material": self.raw.id, "usage_qty": "1", "usage_unit": "pcs"}),
            self.client.get(f"/bom/{bom.id}/items/{item.id}/edit/"),
            self.client.post(f"/bom/{bom.id}/items/{item.id}/edit/", {"line_no": "1", "component_material": self.raw.id, "usage_qty": "2", "usage_unit": "pcs"}),
            self.client.post(f"/bom/{bom.id}/items/{item.id}/delete/", {"current_password": "x", "reason": "无权限删除"}),
            self.client.post(f"/bom/{bom.id}/copy-version/", {"new_bom_no": "BOM003", "new_bom_version": "V3"}),
            self.client.post(f"/bom/{bom.id}/enable/", {"current_password": "x", "reason": "无权限启用"}),
        ]

        self.assertTrue(all(response.status_code == 403 for response in responses))
        self.assertFalse(Bom.objects.filter(bom_no__in=["BOM002", "BOM003"]).exists())
        self.assertEqual(BomItem.objects.filter(bom=bom).count(), 1)
        bom.refresh_from_db()
        self.assertEqual(bom.status, Bom.BomStatus.DRAFT)

    def test_bom_enable_requires_second_verify(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.BOM_PROCESS)
        bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            base_qty="1",
            status=Bom.BomStatus.DRAFT,
            created_by=self.user,
        )
        BomItem.objects.create(
            bom=bom,
            line_no=1,
            component_material=self.raw,
            usage_qty="1",
            usage_unit="pcs",
        )

        response = self.client.post(f"/bom/{bom.id}/enable/", {"current_password": "wrong"})

        bom.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/bom/{bom.id}/")
        self.assertEqual(bom.status, Bom.BomStatus.DRAFT)


def _streaming_text(response) -> str:
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    response.close()
    return content
