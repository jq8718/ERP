from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from decimal import Decimal

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from files.models import ExportLog, ImportJob
from masterdata.models import Customer, CustomerAddress, CustomerProduct, Material, MaterialSupplierPrice, MaterialUnitConversion, Supplier
from system.models import AuditLog


class MasterdataViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="masterdata-user", password="x")
        for permission_code in [
            PermissionCode.SALES_VIEW,
            PermissionCode.PURCHASE_VIEW,
            PermissionCode.INVENTORY_VIEW,
            PermissionCode.BOM_VIEW,
        ]:
            self._grant_permission(permission_code)

    def _grant_permission(self, permission_code: str):
        permission_types = {
            PermissionCode.SALES_VIEW: Permission.PermissionType.MODULE,
            PermissionCode.PURCHASE_VIEW: Permission.PermissionType.MODULE,
            PermissionCode.INVENTORY_VIEW: Permission.PermissionType.MODULE,
            PermissionCode.BOM_VIEW: Permission.PermissionType.MODULE,
            PermissionCode.SALES_VIEW_ALL: Permission.PermissionType.DATA_SCOPE,
            PermissionCode.FINANCE_VIEW_AMOUNT: Permission.PermissionType.FIELD,
            PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO: Permission.PermissionType.FIELD,
        }
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_types.get(permission_code, Permission.PermissionType.ACTION),
            },
        )
        role = Role.objects.create(role_code=f"master-role-{permission_code}-{self.user.id}", role_name=permission_code)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role

    def test_material_list_requires_login(self):
        response = self.client.get("/masterdata/materials/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_core_masterdata_lists_render(self):
        self.client.force_login(self.user)

        paths = [
            "/masterdata/materials/",
            "/masterdata/customers/",
            "/masterdata/customer-products/",
            "/masterdata/suppliers/",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_common_list_empty_state_distinguishes_no_data_and_no_filter_results(self):
        self.client.force_login(self.user)

        empty_response = self.client.get("/masterdata/materials/")
        self.assertContains(empty_response, "暂无物料数据。")

        Material.objects.create(
            material_code="RM-EMPTY-FILTER",
            material_name="空状态筛选原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            status=Material.MaterialStatus.ACTIVE,
        )
        filtered_response = self.client.get("/masterdata/materials/?q=NO_MATCH")

        self.assertContains(filtered_response, "筛选无结果")
        self.assertContains(filtered_response, "没有符合当前筛选条件的数据")
        self.assertContains(filtered_response, "清除筛选")

    def test_material_list_supports_whitelisted_sorting(self):
        Material.objects.create(
            material_code="RM-SORT-A",
            material_name="A 原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            status=Material.MaterialStatus.ACTIVE,
        )
        Material.objects.create(
            material_code="RM-SORT-B",
            material_name="B 原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            status=Material.MaterialStatus.ACTIVE,
        )
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/materials/?sort=material_code&dir=desc")
        export_response = self.client.get("/masterdata/materials/export/?sort=material_code&dir=desc")
        csv_content = _streaming_text(export_response)

        self.assertContains(response, "编码 ↓")
        self.assertContains(response, "sort=material_code&amp;dir=asc")
        content = response.content.decode("utf-8")
        self.assertLess(content.index("RM-SORT-B"), content.index("RM-SORT-A"))
        self.assertLess(csv_content.index("RM-SORT-B"), csv_content.index("RM-SORT-A"))
        export_log = ExportLog.objects.get(module="materials")
        self.assertEqual(export_log.filter_json["ordering"], "-material_code,pk")

    def test_material_create_and_detail_views(self):
        self.client.force_login(self.user)

        response = self.client.post(
            "/masterdata/materials/new/",
            {
                "material_code": "RM001",
                "material_name": "原料 1",
                "material_type": Material.MaterialType.RAW,
                "spec": "通用",
                "base_unit": "pcs",
                "qty_precision": "0",
                "min_stock_qty": "0",
                "latest_purchase_price": "",
                "status": Material.MaterialStatus.ACTIVE,
                "remark": "页面创建",
            },
        )

        material = Material.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/materials/{material.id}/")
        self.assertEqual(material.created_by, self.user)
        detail_response = self.client.get(f"/masterdata/materials/{material.id}/")
        self.assertContains(detail_response, "RM001")

    def test_material_edit_updates_material_and_increments_version(self):
        material = Material.objects.create(
            material_code="RM-EDIT",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
            version=1,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            f"/masterdata/materials/{material.id}/edit/",
            {
                "material_code": "RM-EDIT",
                "material_name": "原料改",
                "material_type": Material.MaterialType.RAW,
                "spec": "新规格",
                "base_unit": "kg",
                "qty_precision": "3",
                "min_stock_qty": "5.0000",
                "status": Material.MaterialStatus.ACTIVE,
                "remark": "编辑",
                "operation_reason": "修正物料基础资料",
            },
        )

        self.assertEqual(response.status_code, 302)
        material.refresh_from_db()
        self.assertEqual(material.material_name, "原料改")
        self.assertEqual(material.qty_precision, 3)
        self.assertEqual(material.version, 2)
        self.assertEqual(material.updated_by, self.user)
        audit_log = AuditLog.objects.get(action="material_update", source_doc_id=material.id)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "修正物料基础资料")

    def test_material_list_hides_amount_import_actions_without_permission(self):
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/materials/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "导出CSV")
        self.assertContains(response, "/masterdata/materials/export/")
        self.assertNotContains(response, "导入CSV")
        self.assertNotContains(response, "/masterdata/materials/import/")
        self.assertNotContains(response, "导入供应商价格")
        self.assertNotContains(response, "/masterdata/materials/supplier-prices/import/")

    def test_material_list_shows_amount_import_actions_with_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/materials/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "导入CSV")
        self.assertContains(response, "/masterdata/materials/import/")
        self.assertContains(response, "导入供应商价格")
        self.assertContains(response, "/masterdata/materials/supplier-prices/import/")

    def test_material_export_creates_csv_and_log(self):
        Material.objects.create(
            material_code="RM-EXP",
            material_name="导出原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            status=Material.MaterialStatus.ACTIVE,
        )
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/materials/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("编码,名称,类型,单位,状态", content)
        self.assertIn("RM-EXP", content)
        self.assertIn("导出原料", content)
        export_log = ExportLog.objects.get(module="materials")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.exported_by, self.user)

    def test_material_list_filter_and_export_share_query(self):
        Material.objects.create(
            material_code="RM-FILTER-KEEP",
            material_name="筛选保留原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            status=Material.MaterialStatus.ACTIVE,
        )
        Material.objects.create(
            material_code="RM-FILTER-HIDE",
            material_name="筛选隐藏原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            status=Material.MaterialStatus.INACTIVE,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/materials/?q=KEEP&status=active")
        export_response = self.client.get("/masterdata/materials/export/?q=KEEP&status=active")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "RM-FILTER-KEEP")
        self.assertNotContains(list_response, "RM-FILTER-HIDE")
        self.assertContains(list_response, "/masterdata/materials/export/?q=KEEP&amp;status=active")
        self.assertIn("RM-FILTER-KEEP", content)
        self.assertNotIn("RM-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="materials")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "active")

    def test_material_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/materials/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("物料编码,物料名称,物料类型,基本单位", content)
        self.assertIn("RM001", content)

    def test_material_import_creates_materials_and_import_job(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "materials.csv",
            (
                "物料编码,物料名称,物料类型,基本单位,规格,数量精度,最低库存,最近采购价,状态,备注\n"
                "RM-IMP,导入原料,raw,kg,规格,3,10.5,12.345678,active,备注\n"
                "FG-IMP,导入成品,finished,pcs,,0,0,,active,\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/masterdata/materials/")
        self.assertTrue(Material.objects.filter(material_code="RM-IMP", created_by=self.user).exists())
        self.assertTrue(Material.objects.filter(material_code="FG-IMP", created_by=self.user).exists())
        job = ImportJob.objects.get(template_type="materials")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 2)

    def test_material_import_reports_validation_errors(self):
        Material.objects.create(
            material_code="RM-DUP",
            material_name="已有原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "materials.csv",
            (
                "物料编码,物料名称,物料类型,基本单位,规格,数量精度,最低库存,最近采购价,状态,备注\n"
                "RM-DUP,重复原料,bad_type,kg,,x,abc,,active,\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "物料编码已存在")
        self.assertContains(response, "物料类型不合法")
        self.assertContains(response, "数量精度必须是非负整数")
        job = ImportJob.objects.get(template_type="materials")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertGreater(job.failed_count, 0)

    @override_settings(ERP_MAX_CSV_IMPORT_ROWS=1)
    def test_material_import_reports_row_limit_error(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "materials.csv",
            (
                "物料编码,物料名称,物料类型,基本单位,规格,数量精度,最低库存,最近采购价,状态,备注\n"
                "RM-ROW-1,第一行,raw,kg,,3,0,,active,\n"
                "RM-ROW-2,第二行,raw,kg,,3,0,,active,\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CSV 数据行数超过 1 行限制")
        self.assertFalse(Material.objects.filter(material_code__in=["RM-ROW-1", "RM-ROW-2"]).exists())
        job = ImportJob.objects.get(template_type="materials")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertEqual(job.failed_count, 1)

    @override_settings(ERP_MAX_CSV_IMPORT_SIZE=16)
    def test_material_import_rejects_oversized_csv_before_creating_job(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "materials.csv",
            "物料编码,物料名称,物料类型,基本单位\nRM-BIG,big,raw,pcs\n".encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/masterdata/materials/import/")
        self.assertFalse(ImportJob.objects.filter(template_type="materials").exists())

    def test_material_import_requires_finance_amount_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/masterdata/materials/import-template/")
        import_response = self.client.get("/masterdata/materials/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_customer_export_uses_sales_data_scope(self):
        other = get_user_model().objects.create_user(username="export-other", password="x")
        Customer.objects.create(customer_no="C-EXPORT-OWN", customer_name="我的客户", sales_owner=self.user)
        Customer.objects.create(customer_no="C-EXPORT-OTHER", customer_name="别人的客户", sales_owner=other)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/customers/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("C-EXPORT-OWN", content)
        self.assertNotIn("C-EXPORT-OTHER", content)
        export_log = ExportLog.objects.get(module="customers")
        self.assertEqual(export_log.row_count, 1)

    def test_customer_list_filter_export_and_scope_share_query(self):
        other = get_user_model().objects.create_user(username="customer-filter-other", password="x")
        Customer.objects.create(
            customer_no="C-FILTER-KEEP",
            customer_name="筛选保留客户",
            sales_owner=self.user,
            status=Customer.CustomerStatus.ACTIVE,
        )
        Customer.objects.create(
            customer_no="C-FILTER-HIDE",
            customer_name="筛选隐藏客户",
            sales_owner=self.user,
            status=Customer.CustomerStatus.INACTIVE,
        )
        Customer.objects.create(
            customer_no="C-FILTER-OTHER",
            customer_name="筛选保留客户外部",
            sales_owner=other,
            status=Customer.CustomerStatus.ACTIVE,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/customers/?q=KEEP&status=active")
        export_response = self.client.get("/masterdata/customers/export/?q=KEEP&status=active")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "C-FILTER-KEEP")
        self.assertNotContains(list_response, "C-FILTER-HIDE")
        self.assertNotContains(list_response, "C-FILTER-OTHER")
        self.assertIn("C-FILTER-KEEP", content)
        self.assertNotIn("C-FILTER-HIDE", content)
        self.assertNotIn("C-FILTER-OTHER", content)
        export_log = ExportLog.objects.get(module="customers")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "active")

    def test_customer_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/customers/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("客户编号,客户名称,简称,销售负责人账号", content)
        self.assertIn("C001", content)

    def test_customer_import_creates_customers_and_import_job(self):
        owner = get_user_model().objects.create_user(username="sales-owner", password="x")
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "customers.csv",
            (
                "客户编号,客户名称,简称,销售负责人账号,结算方式,联系电话,状态,备注\n"
                "C-IMP,导入客户,导入,sales-owner,月结,13800000000,active,备注\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/customers/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        customer = Customer.objects.get(customer_no="C-IMP")
        self.assertEqual(customer.sales_owner, owner)
        self.assertEqual(customer.created_by, self.user)
        job = ImportJob.objects.get(template_type="customers")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_customer_import_reports_missing_sales_owner(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "customers.csv",
            (
                "客户编号,客户名称,简称,销售负责人账号,结算方式,联系电话,状态,备注\n"
                "C-BAD,导入客户,导入,missing-user,月结,13800000000,active,备注\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/customers/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "销售负责人账号不存在")
        job = ImportJob.objects.get(template_type="customers")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)

    def test_customer_import_requires_personal_info_permission(self):
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/customers/")
        template_response = self.client.get("/masterdata/customers/import-template/")
        import_response = self.client.get("/masterdata/customers/import/")

        self.assertNotContains(list_response, "导入CSV")
        self.assertNotContains(list_response, "/masterdata/customers/import/")
        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_customer_product_export_uses_sales_data_scope(self):
        other = get_user_model().objects.create_user(username="product-export-other", password="x")
        own_customer = Customer.objects.create(customer_no="C-PROD-OWN", customer_name="我的客户", sales_owner=self.user)
        other_customer = Customer.objects.create(customer_no="C-PROD-OTHER", customer_name="别人的客户", sales_owner=other)
        material = Material.objects.create(
            material_code="FG-EXPORT",
            material_name="成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        CustomerProduct.objects.create(
            customer=own_customer,
            customer_product_no="CP-OWN",
            customer_product_name="我的产品",
            finished_material=material,
        )
        CustomerProduct.objects.create(
            customer=other_customer,
            customer_product_no="CP-OTHER",
            customer_product_name="别人的产品",
            finished_material=material,
        )
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/customer-products/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("CP-OWN", content)
        self.assertNotIn("CP-OTHER", content)
        export_log = ExportLog.objects.get(module="customer_products")
        self.assertEqual(export_log.row_count, 1)

    def test_customer_product_list_filter_export_and_scope_share_query(self):
        other = get_user_model().objects.create_user(username="product-filter-other", password="x")
        own_customer = Customer.objects.create(customer_no="C-PROD-FILTER", customer_name="我的客户", sales_owner=self.user)
        other_customer = Customer.objects.create(customer_no="C-PROD-FILTER-OTHER", customer_name="别人的客户", sales_owner=other)
        material = Material.objects.create(
            material_code="FG-FILTER",
            material_name="成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        CustomerProduct.objects.create(
            customer=own_customer,
            customer_product_no="CP-FILTER-KEEP",
            customer_product_name="筛选保留产品",
            finished_material=material,
            status=CustomerProduct.ProductStatus.ACTIVE,
        )
        CustomerProduct.objects.create(
            customer=own_customer,
            customer_product_no="CP-FILTER-HIDE",
            customer_product_name="筛选隐藏产品",
            finished_material=material,
            status=CustomerProduct.ProductStatus.INACTIVE,
        )
        CustomerProduct.objects.create(
            customer=other_customer,
            customer_product_no="CP-FILTER-OTHER",
            customer_product_name="筛选保留产品外部",
            finished_material=material,
            status=CustomerProduct.ProductStatus.ACTIVE,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/customer-products/?q=KEEP&status=active")
        export_response = self.client.get("/masterdata/customer-products/export/?q=KEEP&status=active")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "CP-FILTER-KEEP")
        self.assertNotContains(list_response, "CP-FILTER-HIDE")
        self.assertNotContains(list_response, "CP-FILTER-OTHER")
        self.assertIn("CP-FILTER-KEEP", content)
        self.assertNotIn("CP-FILTER-HIDE", content)
        self.assertNotIn("CP-FILTER-OTHER", content)
        export_log = ExportLog.objects.get(module="customer_products")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "active")

    def test_customer_product_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/customer-products/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("客户编号,客户产品编号,客户产品名称,关联成品编码", content)
        self.assertIn("CP001", content)

    def test_customer_product_import_creates_products_and_import_job(self):
        Customer.objects.create(customer_no="C-CP-IMP", customer_name="导入客户", sales_owner=self.user)
        material = Material.objects.create(
            material_code="FG-CP-IMP",
            material_name="导入成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "customer_products.csv",
            (
                "客户编号,客户产品编号,客户产品名称,关联成品编码,默认销售价,状态\n"
                "C-CP-IMP,CP-IMP,导入客户产品,FG-CP-IMP,88.88,active\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/customer-products/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        product = CustomerProduct.objects.get(customer_product_no="CP-IMP")
        self.assertEqual(product.finished_material, material)
        self.assertEqual(product.created_by, self.user)
        job = ImportJob.objects.get(template_type="customer_products")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)

    def test_customer_product_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "customer_products.csv",
            (
                "客户编号,客户产品编号,客户产品名称,关联成品编码,默认销售价,状态\n"
                "C-MISSING,CP-BAD,错误客户产品,FG-MISSING,abc,bad_status\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/customer-products/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "客户编号不存在")
        self.assertContains(response, "关联成品编码不存在")
        self.assertContains(response, "数值格式不合法")
        self.assertContains(response, "状态不合法")
        job = ImportJob.objects.get(template_type="customer_products")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)

    def test_customer_product_import_requires_finance_amount_permission(self):
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/customer-products/")
        template_response = self.client.get("/masterdata/customer-products/import-template/")
        import_response = self.client.get("/masterdata/customer-products/import/")

        self.assertNotContains(list_response, "导入CSV")
        self.assertNotContains(list_response, "/masterdata/customer-products/import/")
        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_supplier_export_masks_contact_without_personal_info_permission(self):
        Supplier.objects.create(
            supplier_no="S-EXPORT",
            supplier_name="导出供应商",
            contact_name="李四",
        )
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/suppliers/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("导出供应商", content)
        self.assertIn("******", content)
        self.assertNotIn("李四", content)

    def test_supplier_export_shows_contact_with_personal_info_permission(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        Supplier.objects.create(
            supplier_no="S-EXPORT-VIEW",
            supplier_name="导出供应商",
            contact_name="李四",
        )
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/suppliers/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("李四", content)
        self.assertNotIn("******", content)

    def test_supplier_list_filter_and_export_share_query_with_masking(self):
        Supplier.objects.create(
            supplier_no="S-FILTER-KEEP",
            supplier_name="筛选保留供应商",
            contact_name="李四",
            status=Supplier.SupplierStatus.ACTIVE,
        )
        Supplier.objects.create(
            supplier_no="S-FILTER-HIDE",
            supplier_name="筛选隐藏供应商",
            contact_name="王五",
            status=Supplier.SupplierStatus.INACTIVE,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/suppliers/?q=KEEP&status=active")
        export_response = self.client.get("/masterdata/suppliers/export/?q=KEEP&status=active")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "S-FILTER-KEEP")
        self.assertNotContains(list_response, "S-FILTER-HIDE")
        self.assertContains(list_response, "/masterdata/suppliers/export/?q=KEEP&amp;status=active")
        self.assertIn("S-FILTER-KEEP", content)
        self.assertNotIn("S-FILTER-HIDE", content)
        self.assertIn("******", content)
        self.assertNotIn("李四", content)
        export_log = ExportLog.objects.get(module="suppliers")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "active")

    def test_supplier_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/suppliers/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("供应商编号,供应商名称,联系人,联系电话", content)
        self.assertIn("S001", content)

    def test_supplier_import_creates_suppliers_and_import_job(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "suppliers.csv",
            (
                "供应商编号,供应商名称,联系人,联系电话,供应商类型,付款方式,状态,备注\n"
                "S-IMP,导入供应商,李四,13900000000,原料,月结,active,备注\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/suppliers/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        supplier = Supplier.objects.get(supplier_no="S-IMP")
        self.assertEqual(supplier.contact_name, "李四")
        self.assertEqual(supplier.supplier_type, "原料")
        self.assertEqual(supplier.payment_method, "月结")
        self.assertEqual(supplier.created_by, self.user)
        job = ImportJob.objects.get(template_type="suppliers")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)

    def test_supplier_import_reports_validation_errors(self):
        Supplier.objects.create(supplier_no="S-DUP", supplier_name="已有供应商")
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "suppliers.csv",
            (
                "供应商编号,供应商名称,联系人,联系电话,供应商类型,付款方式,状态,备注\n"
                "S-DUP,重复供应商,李四,13900000000,乱填类型,乱填付款,bad_status,备注\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/suppliers/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "供应商编号已存在")
        self.assertContains(response, "状态不合法")
        self.assertContains(response, "供应商类型不合法")
        self.assertContains(response, "付款方式不合法")
        job = ImportJob.objects.get(template_type="suppliers")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)

    def test_supplier_import_requires_personal_info_permission(self):
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/suppliers/")
        template_response = self.client.get("/masterdata/suppliers/import-template/")
        import_response = self.client.get("/masterdata/suppliers/import/")

        self.assertNotContains(list_response, "导入CSV")
        self.assertNotContains(list_response, "/masterdata/suppliers/import/")
        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_material_unit_conversion_import_template_downloads_csv(self):
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/materials/unit-conversions/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("物料编码,源单位,目标单位,换算比例,状态", content)
        self.assertIn("RM001", content)

    def test_material_unit_conversion_import_creates_conversions_and_import_job(self):
        material = Material.objects.create(
            material_code="RM-CONV-IMP",
            material_name="导入换算原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "unit_conversions.csv",
            (
                "物料编码,源单位,目标单位,换算比例,状态\n"
                "RM-CONV-IMP,g,kg,0.00100000,active\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/unit-conversions/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/masterdata/materials/")
        conversion = MaterialUnitConversion.objects.get(material=material)
        self.assertEqual(conversion.source_unit, "g")
        self.assertEqual(conversion.target_unit, "kg")
        self.assertEqual(conversion.ratio, Decimal("0.00100000"))
        self.assertEqual(conversion.created_by, self.user)
        job = ImportJob.objects.get(template_type="material_unit_conversions")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)

    def test_material_unit_conversion_import_reports_validation_errors(self):
        material = Material.objects.create(
            material_code="RM-CONV-DUP",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        MaterialUnitConversion.objects.create(material=material, source_unit="g", target_unit="kg", ratio="0.00100000")
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "unit_conversions.csv",
            (
                "物料编码,源单位,目标单位,换算比例,状态\n"
                "RM-CONV-DUP,g,kg,0,bad_status\n"
                "RM-MISSING,kg,kg,abc,active\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/unit-conversions/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "单位换算已存在")
        self.assertContains(response, "换算比例必须大于 0")
        self.assertContains(response, "物料编码不存在")
        self.assertContains(response, "源单位和目标单位不能相同")
        self.assertContains(response, "换算比例格式不合法")
        self.assertContains(response, "状态不合法")
        job = ImportJob.objects.get(template_type="material_unit_conversions")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)

    def test_customer_address_import_requires_personal_info_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/masterdata/customers/addresses/import-template/")
        import_response = self.client.get("/masterdata/customers/addresses/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_customer_address_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/customers/addresses/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("客户编号,地址类型,收件人,收件电话,地址,是否默认,状态", content)
        self.assertIn("C001", content)

    def test_customer_address_import_creates_addresses_and_import_job(self):
        customer = Customer.objects.create(customer_no="C-ADDR-IMP", customer_name="导入地址客户", sales_owner=self.user)
        CustomerAddress.objects.create(
            customer=customer,
            address_type=CustomerAddress.AddressType.SHIPPING,
            receiver_name="旧默认",
            is_default=True,
        )
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "customer_addresses.csv",
            (
                "客户编号,地址类型,收件人,收件电话,地址,是否默认,状态\n"
                "C-ADDR-IMP,shipping,王五,13800000000,深圳市测试路 2 号,true,active\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/customers/addresses/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/masterdata/customers/")
        address = CustomerAddress.objects.get(customer=customer, receiver_name="王五")
        self.assertTrue(address.is_default)
        self.assertEqual(address.receiver_phone_encrypted, "13800000000")
        self.assertEqual(address.created_by, self.user)
        self.assertFalse(CustomerAddress.objects.get(customer=customer, receiver_name="旧默认").is_default)
        job = ImportJob.objects.get(template_type="customer_addresses")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)

    def test_customer_address_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "customer_addresses.csv",
            (
                "客户编号,地址类型,收件人,收件电话,地址,是否默认,状态\n"
                "C-MISSING,bad_type,,13800000000,,maybe,bad_status\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/customers/addresses/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "客户编号不存在")
        self.assertContains(response, "地址类型不合法")
        self.assertContains(response, "必填字段不能为空")
        self.assertContains(response, "是否默认格式不合法")
        self.assertContains(response, "状态不合法")
        job = ImportJob.objects.get(template_type="customer_addresses")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)

    def test_material_supplier_price_import_requires_finance_amount_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/masterdata/materials/supplier-prices/import-template/")
        import_response = self.client.get("/masterdata/materials/supplier-prices/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_material_supplier_price_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/materials/supplier-prices/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("物料编码,供应商编号,采购价,币种,生效日期,失效日期,是否默认,状态", content)
        self.assertIn("S001", content)

    def test_material_supplier_price_import_creates_prices_and_import_job(self):
        material = Material.objects.create(
            material_code="RM-PRICE-IMP",
            material_name="导入价格原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        supplier = Supplier.objects.create(supplier_no="S-PRICE-IMP", supplier_name="导入价格供应商")
        MaterialSupplierPrice.objects.create(
            material=material,
            supplier=supplier,
            purchase_price="8.000000",
            is_default=True,
        )
        other_supplier = Supplier.objects.create(supplier_no="S-PRICE-NEW", supplier_name="新供应商")
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "supplier_prices.csv",
            (
                "物料编码,供应商编号,采购价,币种,生效日期,失效日期,是否默认,状态\n"
                "RM-PRICE-IMP,S-PRICE-NEW,12.345600,CNY,2026-06-09,,true,active\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/supplier-prices/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/masterdata/materials/")
        price = MaterialSupplierPrice.objects.get(material=material, supplier=other_supplier)
        self.assertEqual(price.purchase_price, Decimal("12.345600"))
        self.assertEqual(price.effective_from.isoformat(), "2026-06-09")
        self.assertTrue(price.is_default)
        self.assertEqual(price.created_by, self.user)
        self.assertFalse(MaterialSupplierPrice.objects.get(material=material, supplier=supplier).is_default)
        job = ImportJob.objects.get(template_type="material_supplier_prices")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)

    def test_material_supplier_price_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "supplier_prices.csv",
            (
                "物料编码,供应商编号,采购价,币种,生效日期,失效日期,是否默认,状态\n"
                "RM-MISSING,S-MISSING,-1,CNY,2026-06-10,2026-06-09,maybe,bad_status\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/masterdata/materials/supplier-prices/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "物料编码不存在")
        self.assertContains(response, "供应商编号不存在")
        self.assertContains(response, "采购价格不能小于 0")
        self.assertContains(response, "失效日期不能早于生效日期")
        self.assertContains(response, "是否默认格式不合法")
        self.assertContains(response, "状态不合法")
        job = ImportJob.objects.get(template_type="material_supplier_prices")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)

    def test_material_purchase_prices_mask_without_finance_permission(self):
        material = Material.objects.create(
            material_code="RM-MASK",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            latest_purchase_price="12.345600",
        )
        supplier = Supplier.objects.create(supplier_no="S-MASK", supplier_name="供应商")
        MaterialSupplierPrice.objects.create(material=material, supplier=supplier, purchase_price="9.876543")
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/materials/{material.id}/")

        self.assertContains(response, "******")
        self.assertNotContains(response, "12.345600")
        self.assertNotContains(response, "9.876543")

    def test_material_purchase_prices_visible_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        material = Material.objects.create(
            material_code="RM-VIEW",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            latest_purchase_price="12.345600",
        )
        supplier = Supplier.objects.create(supplier_no="S-VIEW", supplier_name="供应商")
        MaterialSupplierPrice.objects.create(material=material, supplier=supplier, purchase_price="9.876543")
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/materials/{material.id}/")

        self.assertContains(response, "12.345600")
        self.assertContains(response, "9.876543")

    def test_customer_create_and_detail_views(self):
        self.client.force_login(self.user)

        response = self.client.post(
            "/masterdata/customers/new/",
            {
                "customer_no": "C001",
                "customer_name": "测试客户",
                "short_name": "测试",
                "sales_owner": "",
                "settlement_method": "月结",
                "contact_phone_encrypted": "13800000000",
                "status": Customer.CustomerStatus.ACTIVE,
                "remark": "页面创建",
            },
        )

        customer = Customer.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/customers/{customer.id}/")
        self.assertEqual(customer.created_by, self.user)
        self.assertEqual(customer.contact_phone_encrypted, "")
        detail_response = self.client.get(f"/masterdata/customers/{customer.id}/")
        self.assertContains(detail_response, "测试客户")

    def test_customer_edit_without_personal_info_permission_preserves_phone(self):
        customer = Customer.objects.create(
            customer_no="C-EDIT",
            customer_name="客户",
            sales_owner=self.user,
            contact_phone_encrypted="13811112222",
            version=1,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            f"/masterdata/customers/{customer.id}/edit/",
            {
                "customer_no": "C-EDIT",
                "customer_name": "客户改",
                "short_name": "改",
                "sales_owner": self.user.id,
                "settlement_method": "月结",
                "status": Customer.CustomerStatus.ACTIVE,
                "remark": "编辑",
            },
        )

        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertEqual(customer.customer_name, "客户改")
        self.assertEqual(customer.contact_phone_encrypted, "13811112222")
        self.assertEqual(customer.version, 2)

    def test_customer_product_default_price_masks_without_finance_permission(self):
        customer = Customer.objects.create(customer_no="C-MASK", customer_name="客户", sales_owner=self.user)
        material = Material.objects.create(
            material_code="FG-MASK",
            material_name="成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        CustomerProduct.objects.create(
            customer=customer,
            customer_product_no="CP-MASK",
            customer_product_name="客户产品",
            finished_material=material,
            default_sale_price="66.6600",
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/customers/{customer.id}/")

        self.assertContains(response, "******")
        self.assertNotContains(response, "66.6600")

    def test_customer_product_list_and_detail_views(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        customer = Customer.objects.create(customer_no="C-CP-DETAIL", customer_name="客户", sales_owner=self.user)
        material = Material.objects.create(
            material_code="FG-CP-DETAIL",
            material_name="成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        product = CustomerProduct.objects.create(
            customer=customer,
            customer_product_no="CP-DETAIL",
            customer_product_name="客户产品详情",
            finished_material=material,
            default_sale_price="55.5500",
            label_requirements={"label": "正标"},
            packaging_requirements={"box": "彩盒"},
            created_by=self.user,
            updated_by=self.user,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/customer-products/")
        customer_response = self.client.get(f"/masterdata/customers/{customer.id}/")
        detail_response = self.client.get(f"/masterdata/customer-products/{product.id}/")

        self.assertContains(list_response, "CP-DETAIL")
        self.assertContains(list_response, f"/masterdata/customer-products/{product.id}/")
        self.assertContains(customer_response, f"/masterdata/customer-products/{product.id}/")
        self.assertContains(detail_response, "客户产品详情")
        self.assertContains(detail_response, "FG-CP-DETAIL")
        self.assertContains(detail_response, "55.5500")
        self.assertContains(detail_response, "正标")
        self.assertContains(detail_response, "彩盒")

    def test_customer_product_create_view_adds_product_from_customer_detail(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        customer = Customer.objects.create(customer_no="C-CP-CREATE", customer_name="客户", sales_owner=self.user)
        material = Material.objects.create(
            material_code="FG-CP-CREATE",
            material_name="成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        self.client.force_login(self.user)

        response = self.client.post(
            f"/masterdata/customers/{customer.id}/products/new/",
            {
                "customer_product_no": "CP-CREATE",
                "customer_product_name": "页面客户产品",
                "finished_material": material.id,
                "default_sale_price": "18.8800",
                "label_requirements": "无要求",
                "packaging_requirements": "塑料包装",
                "status": CustomerProduct.ProductStatus.ACTIVE,
            },
        )

        self.assertEqual(response.status_code, 302)
        product = CustomerProduct.objects.get(customer=customer, customer_product_no="CP-CREATE")
        self.assertEqual(response["Location"], f"/masterdata/customer-products/{product.id}/")
        self.assertEqual(product.finished_material, material)
        self.assertEqual(product.default_sale_price, Decimal("18.8800"))
        self.assertEqual(product.label_requirements, "无要求")
        self.assertEqual(product.packaging_requirements, "塑料包装")
        self.assertEqual(product.created_by, self.user)

    def test_customer_product_edit_updates_product_and_preserves_price_without_permission(self):
        customer = Customer.objects.create(customer_no="C-CP-EDIT", customer_name="客户", sales_owner=self.user)
        material = Material.objects.create(
            material_code="FG-CP-EDIT",
            material_name="成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        product = CustomerProduct.objects.create(
            customer=customer,
            customer_product_no="CP-EDIT",
            customer_product_name="客户产品",
            finished_material=material,
            default_sale_price="77.7700",
            label_requirements={"old": "label"},
            packaging_requirements={"old": "pack"},
            version=1,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            f"/masterdata/customer-products/{product.id}/edit/",
            {
                "customer_product_no": "CP-EDIT",
                "customer_product_name": "客户产品改",
                "finished_material": material.id,
                "label_requirements": '{"new": "label"}',
                "packaging_requirements": '{"new": "pack"}',
                "status": CustomerProduct.ProductStatus.INACTIVE,
                "operation_reason": "客户标签要求变更",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/customer-products/{product.id}/")
        product.refresh_from_db()
        self.assertEqual(product.customer_product_name, "客户产品改")
        self.assertEqual(product.status, CustomerProduct.ProductStatus.INACTIVE)
        self.assertEqual(product.default_sale_price, Decimal("77.7700"))
        self.assertEqual(product.label_requirements, {"new": "label"})
        self.assertEqual(product.packaging_requirements, {"new": "pack"})
        self.assertEqual(product.version, 2)
        self.assertEqual(product.updated_by, self.user)
        audit_log = AuditLog.objects.get(action="customer_product_update", source_doc_id=product.id)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "客户标签要求变更")

    def test_customer_contact_info_masks_without_personal_info_permission(self):
        customer = Customer.objects.create(
            customer_no="C-PII",
            customer_name="客户",
            sales_owner=self.user,
            contact_phone_encrypted="13811112222",
        )
        CustomerAddress.objects.create(
            customer=customer,
            receiver_name="王五",
            receiver_phone_encrypted="13933334444",
            address_encrypted="深圳市测试路 1 号",
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/customers/{customer.id}/")

        self.assertContains(response, "******")
        self.assertNotContains(response, "13811112222")
        self.assertNotContains(response, "王五")
        self.assertNotContains(response, "13933334444")

    def test_customer_contact_info_visible_with_personal_info_permission(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        customer = Customer.objects.create(
            customer_no="C-PII-VIEW",
            customer_name="客户",
            sales_owner=self.user,
            contact_phone_encrypted="13811112222",
        )
        CustomerAddress.objects.create(
            customer=customer,
            receiver_name="王五",
            receiver_phone_encrypted="13933334444",
            address_encrypted="深圳市测试路 1 号",
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/customers/{customer.id}/")

        self.assertContains(response, "13811112222")
        self.assertContains(response, "王五")
        self.assertContains(response, "13933334444")

    def test_customer_address_create_view_requires_and_uses_personal_info_permission(self):
        customer = Customer.objects.create(customer_no="C-ADDR", customer_name="客户", sales_owner=self.user)
        self.client.force_login(self.user)

        denied_response = self.client.get(f"/masterdata/customers/{customer.id}/addresses/new/")

        self.assertEqual(denied_response.status_code, 403)

        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        response = self.client.post(
            f"/masterdata/customers/{customer.id}/addresses/new/",
            {
                "address_type": CustomerAddress.AddressType.SHIPPING,
                "receiver_name": "王五",
                "receiver_phone_encrypted": "13933334444",
                "address_encrypted": "深圳市测试路 1 号",
                "is_default": "on",
                "status": CustomerAddress.AddressStatus.ACTIVE,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/customers/{customer.id}/")
        address = CustomerAddress.objects.get(customer=customer)
        self.assertEqual(address.receiver_name, "王五")
        self.assertTrue(address.is_default)
        self.assertEqual(address.created_by, self.user)

    def test_customer_address_edit_requires_permission_and_switches_default(self):
        customer = Customer.objects.create(customer_no="C-ADDR-EDIT", customer_name="客户", sales_owner=self.user)
        old_default = CustomerAddress.objects.create(
            customer=customer,
            address_type=CustomerAddress.AddressType.SHIPPING,
            receiver_name="旧默认",
            receiver_phone_encrypted="13800000000",
            address_encrypted="旧地址",
            is_default=True,
        )
        address = CustomerAddress.objects.create(
            customer=customer,
            address_type=CustomerAddress.AddressType.SHIPPING,
            receiver_name="待编辑",
            receiver_phone_encrypted="13900000000",
            address_encrypted="待编辑地址",
            is_default=False,
            version=1,
        )
        self.client.force_login(self.user)

        denied_response = self.client.get(f"/masterdata/customers/addresses/{address.id}/edit/")
        self.assertEqual(denied_response.status_code, 403)

        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        detail_response = self.client.get(f"/masterdata/customers/{customer.id}/")
        response = self.client.post(
            f"/masterdata/customers/addresses/{address.id}/edit/",
            {
                "address_type": CustomerAddress.AddressType.SHIPPING,
                "receiver_name": "王五",
                "receiver_phone_encrypted": "13911112222",
                "address_encrypted": "新地址",
                "is_default": "on",
                "status": CustomerAddress.AddressStatus.ACTIVE,
                "operation_reason": "客户迁址",
            },
        )

        self.assertContains(detail_response, f"/masterdata/customers/addresses/{address.id}/edit/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/customers/{customer.id}/")
        old_default.refresh_from_db()
        address.refresh_from_db()
        self.assertFalse(old_default.is_default)
        self.assertTrue(address.is_default)
        self.assertEqual(address.receiver_name, "王五")
        self.assertEqual(address.receiver_phone_encrypted, "13911112222")
        self.assertEqual(address.address_encrypted, "新地址")
        self.assertEqual(address.version, 2)
        self.assertEqual(address.updated_by, self.user)
        audit_log = AuditLog.objects.get(action="customer_address_update", source_doc_id=address.id)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "客户迁址")

    def test_customer_list_filters_to_owner_without_view_all(self):
        other = get_user_model().objects.create_user(username="other-owner", password="x")
        Customer.objects.create(customer_no="C-OWN", customer_name="我的客户", sales_owner=self.user)
        Customer.objects.create(customer_no="C-OTHER", customer_name="别人的客户", sales_owner=other)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/customers/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "我的客户")
        self.assertNotContains(response, "别人的客户")

    def test_customer_list_view_all_permission_shows_all_customers(self):
        other = get_user_model().objects.create_user(username="other-owner", password="x")
        Customer.objects.create(customer_no="C-OWN", customer_name="我的客户", sales_owner=self.user)
        Customer.objects.create(customer_no="C-OTHER", customer_name="别人的客户", sales_owner=other)
        self._grant_permission(PermissionCode.SALES_VIEW_ALL)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/customers/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "我的客户")
        self.assertContains(response, "别人的客户")

    def test_supplier_create_and_detail_views(self):
        self.client.force_login(self.user)

        response = self.client.post(
            "/masterdata/suppliers/new/",
            {
                "supplier_no": "S001",
                "supplier_name": "测试供应商",
                "contact_name": "李四",
                "contact_phone_encrypted": "13900000000",
                "supplier_type": "原料",
                "payment_method": "月结",
                "status": Supplier.SupplierStatus.ACTIVE,
                "remark": "页面创建",
            },
        )

        supplier = Supplier.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/suppliers/{supplier.id}/")
        self.assertEqual(supplier.created_by, self.user)
        self.assertEqual(supplier.contact_name, "")
        self.assertEqual(supplier.contact_phone_encrypted, "")
        detail_response = self.client.get(f"/masterdata/suppliers/{supplier.id}/")
        self.assertContains(detail_response, "测试供应商")

    def test_supplier_type_and_payment_method_use_select_options(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        self.client.force_login(self.user)

        response = self.client.get("/masterdata/suppliers/new/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="supplier-grid"')
        self.assertContains(response, 'class="supplier-field medium"')
        self.assertContains(response, 'name="contact_phone_encrypted"')
        self.assertNotContains(response, '<textarea name="contact_phone_encrypted"')
        self.assertContains(response, 'select name="supplier_type"')
        self.assertContains(response, '<option value="原料">原料</option>', html=True)
        self.assertContains(response, '<option value="外协加工">外协加工</option>', html=True)
        self.assertContains(response, '<option value="运输">运输</option>', html=True)
        self.assertContains(response, 'select name="payment_method"')
        self.assertContains(response, '<option value="转账">转账</option>', html=True)
        self.assertContains(response, '<option value="月结30天">月结30天</option>', html=True)
        self.assertContains(response, '<option value="货到付款">货到付款</option>', html=True)

    def test_supplier_edit_without_personal_info_permission_preserves_contact(self):
        supplier = Supplier.objects.create(
            supplier_no="S-EDIT",
            supplier_name="供应商",
            contact_name="李四",
            contact_phone_encrypted="13900000000",
            version=1,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            f"/masterdata/suppliers/{supplier.id}/edit/",
            {
                "supplier_no": "S-EDIT",
                "supplier_name": "供应商改",
                "supplier_type": "原料",
                "payment_method": "月结",
                "status": Supplier.SupplierStatus.ACTIVE,
                "remark": "编辑",
            },
        )

        self.assertEqual(response.status_code, 302)
        supplier.refresh_from_db()
        self.assertEqual(supplier.supplier_name, "供应商改")
        self.assertEqual(supplier.contact_name, "李四")
        self.assertEqual(supplier.contact_phone_encrypted, "13900000000")
        self.assertEqual(supplier.version, 2)

    def test_supplier_purchase_prices_mask_without_finance_permission(self):
        material = Material.objects.create(
            material_code="RM-SUP",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
        )
        supplier = Supplier.objects.create(supplier_no="S-SUP", supplier_name="供应商")
        price = MaterialSupplierPrice.objects.create(material=material, supplier=supplier, purchase_price="7.777777")
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/suppliers/{supplier.id}/")

        self.assertContains(response, "******")
        self.assertNotContains(response, "7.777777")
        self.assertContains(response, f"/masterdata/materials/{material.id}/")
        self.assertNotContains(response, f"/masterdata/materials/supplier-prices/{price.id}/edit/")

    def test_supplier_detail_links_material_and_price_edit_with_amount_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        material = Material.objects.create(
            material_code="RM-SUP-LINK",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
        )
        supplier = Supplier.objects.create(supplier_no="S-SUP-LINK", supplier_name="供应商")
        price = MaterialSupplierPrice.objects.create(material=material, supplier=supplier, purchase_price="7.777777")
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/suppliers/{supplier.id}/")

        self.assertContains(response, "7.777777")
        self.assertContains(response, f"/masterdata/materials/{material.id}/")
        self.assertContains(response, f"/masterdata/materials/supplier-prices/{price.id}/edit/")

    def test_supplier_contact_info_masks_without_personal_info_permission(self):
        supplier = Supplier.objects.create(
            supplier_no="S-PII",
            supplier_name="供应商",
            contact_name="李四",
            contact_phone_encrypted="13900000000",
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/masterdata/suppliers/")
        detail_response = self.client.get(f"/masterdata/suppliers/{supplier.id}/")

        self.assertContains(list_response, "******")
        self.assertNotContains(list_response, "李四")
        self.assertContains(detail_response, "******")
        self.assertNotContains(detail_response, "李四")
        self.assertNotContains(detail_response, "13900000000")

    def test_supplier_contact_info_visible_with_personal_info_permission(self):
        self._grant_permission(PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)
        supplier = Supplier.objects.create(
            supplier_no="S-PII-VIEW",
            supplier_name="供应商",
            contact_name="李四",
            contact_phone_encrypted="13900000000",
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/masterdata/suppliers/{supplier.id}/")

        self.assertContains(response, "李四")
        self.assertContains(response, "13900000000")

    def test_material_unit_conversion_create_view_adds_conversion(self):
        material = Material.objects.create(
            material_code="RM-CONV",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        self.client.force_login(self.user)

        response = self.client.post(
            f"/masterdata/materials/{material.id}/unit-conversions/new/",
            {
                "source_unit": "g",
                "target_unit": "kg",
                "ratio": "0.00100000",
                "status": "active",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/materials/{material.id}/")
        conversion = material.unit_conversions.get()
        self.assertEqual(conversion.source_unit, "g")
        self.assertEqual(conversion.target_unit, "kg")
        self.assertEqual(conversion.created_by, self.user)

    def test_material_unit_conversion_edit_view_updates_conversion(self):
        material = Material.objects.create(
            material_code="RM-CONV-EDIT",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        conversion = MaterialUnitConversion.objects.create(
            material=material,
            source_unit="g",
            target_unit="kg",
            ratio="0.00100000",
            version=1,
        )
        self.client.force_login(self.user)

        detail_response = self.client.get(f"/masterdata/materials/{material.id}/")
        response = self.client.post(
            f"/masterdata/materials/unit-conversions/{conversion.id}/edit/",
            {
                "source_unit": "g",
                "target_unit": "kg",
                "ratio": "0.00200000",
                "status": MaterialUnitConversion.ConversionStatus.INACTIVE,
            },
        )

        self.assertContains(detail_response, f"/masterdata/materials/unit-conversions/{conversion.id}/edit/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/materials/{material.id}/")
        conversion.refresh_from_db()
        self.assertEqual(conversion.ratio, Decimal("0.00200000"))
        self.assertEqual(conversion.status, MaterialUnitConversion.ConversionStatus.INACTIVE)
        self.assertEqual(conversion.version, 2)
        self.assertEqual(conversion.updated_by, self.user)

    def test_material_supplier_price_create_view_requires_finance_permission(self):
        material = Material.objects.create(
            material_code="RM-PRICE",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        supplier = Supplier.objects.create(supplier_no="S-PRICE", supplier_name="供应商")
        self.client.force_login(self.user)

        denied_response = self.client.get(f"/masterdata/materials/{material.id}/supplier-prices/new/")

        self.assertEqual(denied_response.status_code, 403)

        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        response = self.client.post(
            f"/masterdata/materials/{material.id}/supplier-prices/new/",
            {
                "supplier": supplier.id,
                "purchase_price": "9.876543",
                "currency": "CNY",
                "effective_from": "",
                "effective_to": "",
                "is_default": "on",
                "status": MaterialSupplierPrice.PriceStatus.ACTIVE,
                "operation_reason": "供应商调价",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/materials/{material.id}/")
        price = MaterialSupplierPrice.objects.get(material=material, supplier=supplier)
        self.assertEqual(price.purchase_price, Decimal("9.876543"))
        self.assertTrue(price.is_default)
        self.assertEqual(price.created_by, self.user)

    def test_material_supplier_price_edit_requires_permission_and_switches_default(self):
        material = Material.objects.create(
            material_code="RM-PRICE-EDIT",
            material_name="原料",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        supplier_a = Supplier.objects.create(supplier_no="S-PRICE-A", supplier_name="供应商A")
        supplier_b = Supplier.objects.create(supplier_no="S-PRICE-B", supplier_name="供应商B")
        price_a = MaterialSupplierPrice.objects.create(
            material=material,
            supplier=supplier_a,
            purchase_price="8.000000",
            is_default=True,
        )
        price_b = MaterialSupplierPrice.objects.create(
            material=material,
            supplier=supplier_b,
            purchase_price="9.000000",
            is_default=False,
            version=1,
        )
        self.client.force_login(self.user)

        denied_response = self.client.get(f"/masterdata/materials/supplier-prices/{price_b.id}/edit/")
        self.assertEqual(denied_response.status_code, 403)

        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        detail_response = self.client.get(f"/masterdata/materials/{material.id}/")
        response = self.client.post(
            f"/masterdata/materials/supplier-prices/{price_b.id}/edit/",
            {
                "supplier": supplier_b.id,
                "purchase_price": "10.123456",
                "currency": "CNY",
                "effective_from": "",
                "effective_to": "",
                "is_default": "on",
                "status": MaterialSupplierPrice.PriceStatus.ACTIVE,
                "operation_reason": "供应商调价",
            },
        )

        self.assertContains(detail_response, f"/masterdata/materials/supplier-prices/{price_b.id}/edit/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/masterdata/materials/{material.id}/")
        price_a.refresh_from_db()
        price_b.refresh_from_db()
        self.assertFalse(price_a.is_default)
        self.assertTrue(price_b.is_default)
        self.assertEqual(price_b.purchase_price, Decimal("10.123456"))
        self.assertEqual(price_b.version, 2)
        self.assertEqual(price_b.updated_by, self.user)
        audit_log = AuditLog.objects.get(action="material_supplier_price_update", source_doc_id=price_b.id)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "供应商调价")


def _streaming_text(response) -> str:
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    response.close()
    return content
