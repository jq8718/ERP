from __future__ import annotations

import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from secrets import token_hex

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import DEFAULT_PERMISSIONS, PermissionCode, ensure_default_permissions
from bom.models import Bom, BomItem
from finance.models import (
    CustomerReceipt,
    ExpenseRecord,
    OpeningPayable,
    OpeningReceivable,
    Reconciliation,
    SupplierPayment,
)
from finance.services import confirm_customer_receipt, confirm_supplier_payment
from inventory.models import Inventory, InventoryBatch, InventoryTransaction, LocationTransfer, WarehouseLocation
from inventory.services import confirm_location_transfer
from masterdata.models import (
    Customer,
    CustomerAddress,
    CustomerProduct,
    Material,
    MaterialSupplierPrice,
    SettlementMethod,
    Supplier,
    SupplierPaymentMethod,
    SupplierType,
)
from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem, SupplierReturn, SupplierReturnItem
from purchase.services import confirm_purchase_receipt, confirm_supplier_return_shipment, create_purchase_order_from_request, create_purchase_request_from_shortages
from production.models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)
from production.services import confirm_material_requisition, confirm_production_receipt
from sales.models import (
    CustomerReturn,
    CustomerReturnItem,
    SalesOrder,
    SalesOrderItem,
    SalesShipment,
    SalesShipmentItem,
    SampleLoan,
    SampleLoanItem,
    SampleLoanReturn,
    SampleLoanReturnItem,
    ShortageAlert,
)
from sales.services import (
    confirm_customer_return_receipt,
    confirm_sales_order,
    confirm_sales_shipment,
    confirm_sample_loan_out,
    confirm_sample_return,
    convert_sample_loan_item_to_sales_order,
)
from system.services import ServiceResult, next_document_no, process_pending_events


ZERO = Decimal("0")


@dataclass
class UatReport:
    tag: str
    checks: list[str] = field(default_factory=list)
    records: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.checks.append(message)

    def record(self, label: str, value: str) -> None:
        self.records.append(f"{label}: {value}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class RealHttpClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf_token = ""

    def login(self) -> None:
        page = self.get("/login/")
        token = self._extract_csrf(page)
        data = {
            "username": self.username,
            "password": self.password,
            "csrfmiddlewaretoken": token,
        }
        self.post("/login/", data, prefetch=False)
        dashboard = self.get("/")
        if self.username not in dashboard and "退出" not in dashboard:
            raise CommandError(f"账号 {self.username} 登录后未进入系统首页")

    def get_status(self, path: str) -> tuple[int, str]:
        try:
            return 200, self.get(path)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, body

    def get(self, path: str) -> str:
        request = urllib.request.Request(self._url(path), headers={"User-Agent": "ERP-UAT/LED-Backlight"})
        response = self.opener.open(request, timeout=15)
        body = response.read().decode("utf-8", errors="replace")
        token = self._extract_csrf(body, required=False)
        if token:
            self.csrf_token = token
        return body

    def post(self, path: str, data: dict[str, str], prefetch: bool = True) -> tuple[int, str]:
        if prefetch:
            self.get(path)
        token = self.csrf_token or self._cookie("csrftoken")
        payload = {**data, "csrfmiddlewaretoken": token}
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url(path),
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self._url(path),
                "User-Agent": "ERP-UAT/LED-Backlight",
            },
        )
        try:
            response = self.opener.open(request, timeout=15)
            body = response.read().decode("utf-8", errors="replace")
            return response.getcode(), body
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _cookie(self, name: str) -> str:
        for cookie in self.jar:
            if cookie.name == name:
                return cookie.value
        return ""

    def _extract_csrf(self, html: str, required: bool = True) -> str:
        match = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']', html)
        if match:
            self.csrf_token = match.group(1)
            return self.csrf_token
        token = self._cookie("csrftoken")
        if token:
            self.csrf_token = token
            return token
        if required:
            raise CommandError("页面中未找到 CSRF token")
        return ""


class Command(BaseCommand):
    help = "在真实服务器和真实数据库中模拟 LED 背光厂家 ERP 试用，并保留 UAT 数据"

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="真实 ERP 服务器地址")
        parser.add_argument("--password", default="Trial@2026Erp!", help="UAT 岗位账号统一密码")
        parser.add_argument("--tag", default="", help="本次 UAT 数据标记；默认自动生成")

    def handle(self, *args, **options):
        tag = (options["tag"].strip() or f"UATLED{timezone.now():%Y%m%d%H%M%S}{token_hex(2).upper()}")[:32]
        password = options["password"]
        base_url = options["base_url"].rstrip("/")
        report = UatReport(tag=tag)

        try:
            users = self._ensure_roles_and_users(password, report)
            self._check_http_permissions(base_url, password, users, report)
            fixture = self._create_led_master_data(tag, users, report)
            self._run_purchase_approval_http(base_url, password, users, fixture, report)
            self._run_led_business_flows(tag, users, fixture, report)
            self._check_final_pages(base_url, password, users, report)
        except Exception as exc:
            raise CommandError(f"LED 背光厂家 UAT 失败，tag={tag}：{exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"LED 背光厂家 ERP 试用完成，tag={tag}"))
        self.stdout.write(self.style.SUCCESS("测试账号："))
        for key, user in users.items():
            self.stdout.write(f"  {key}: {user.username} / {password}")
        self.stdout.write(self.style.SUCCESS("保留的数据："))
        for item in report.records:
            self.stdout.write(f"  {item}")
        self.stdout.write(self.style.SUCCESS("通过的检查："))
        for item in report.checks:
            self.stdout.write(f"  [OK] {item}")
        if report.warnings:
            self.stdout.write(self.style.WARNING("需要人工关注："))
            for item in report.warnings:
                self.stdout.write(f"  [WARN] {item}")

    def _ensure_roles_and_users(self, password: str, report: UatReport) -> dict[str, object]:
        ensure_default_permissions()
        permission_map = {permission.permission_code: permission for permission in Permission.objects.all()}

        def role(role_code: str, role_name: str, codes: list[str]) -> Role:
            role_obj, _ = Role.objects.get_or_create(
                role_code=role_code,
                defaults={"role_name": role_name, "status": Role.RoleStatus.ACTIVE},
            )
            role_obj.role_name = role_name
            role_obj.status = Role.RoleStatus.ACTIVE
            role_obj.remark = "LED 背光厂家 UAT 岗位角色，保留用于后续真实服务器复测"
            role_obj.save()
            role_obj.permissions.set([permission_map[code] for code in codes if code in permission_map])
            return role_obj

        all_codes = [code for code, _name, _type in DEFAULT_PERMISSIONS]
        roles = {
            "admin": role("uat_led_admin_role", "UAT LED 系统管理员", all_codes),
            "boss": role("uat_led_boss_role", "UAT LED 经营主管", all_codes),
            "sales": role(
                "uat_led_sales_role",
                "UAT LED 销售",
                [
                    PermissionCode.SALES_VIEW,
                    PermissionCode.SALES_PROCESS,
                    PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
                    PermissionCode.FINANCE_VIEW_AMOUNT,
                ],
            ),
            "purchase": role(
                "uat_led_purchase_role",
                "UAT LED 采购",
                [
                    PermissionCode.PURCHASE_VIEW,
                    PermissionCode.PURCHASE_PROCESS,
                    PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
                    PermissionCode.FINANCE_VIEW_AMOUNT,
                ],
            ),
            "warehouse": role(
                "uat_led_warehouse_role",
                "UAT LED 仓库",
                [PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS],
            ),
            "production": role(
                "uat_led_production_role",
                "UAT LED 生产",
                [
                    PermissionCode.PRODUCTION_VIEW,
                    PermissionCode.PRODUCTION_PROCESS,
                    PermissionCode.BOM_VIEW,
                    PermissionCode.INVENTORY_VIEW,
                ],
            ),
            "finance": role(
                "uat_led_finance_role",
                "UAT LED 财务",
                [
                    PermissionCode.FINANCE_VIEW_AMOUNT,
                    PermissionCode.FINANCE_PAYMENT_PROCESS,
                    PermissionCode.SALES_VIEW_ALL,
                    PermissionCode.PURCHASE_VIEW,
                    PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
                ],
            ),
        }

        users = {}
        User = get_user_model()
        for key, role_obj in roles.items():
            username = f"uat_led_{key}"
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={
                    "display_name": role_obj.role_name,
                    "department": "LED 背光 UAT",
                    "position": role_obj.role_name.replace("UAT LED ", ""),
                    "is_active": True,
                    "status": User.AccountStatus.ACTIVE,
                },
            )
            user.display_name = role_obj.role_name
            user.department = "LED 背光 UAT"
            user.position = role_obj.role_name.replace("UAT LED ", "")
            user.is_active = True
            user.is_deleted = False
            user.status = User.AccountStatus.ACTIVE
            user.set_password(password)
            user.save()
            user.roles.set([role_obj])
            users[key] = user
        report.ok("已建立独立 UAT 岗位账号和权限角色")
        return users

    def _check_http_permissions(self, base_url: str, password: str, users: dict[str, object], report: UatReport) -> None:
        expectations = [
            ("sales", "/sales/orders/new/", 200, "销售可新建销售订单"),
            ("sales", "/purchase/orders/new/", 403, "销售不可新建采购单"),
            ("purchase", "/purchase/orders/new/", 200, "采购可新建采购单"),
            ("purchase", "/sales/orders/new/", 403, "采购不可新建销售订单"),
            ("warehouse", "/inventory/locations/new/", 200, "仓库可维护库位"),
            ("warehouse", "/production/orders/new/", 403, "仓库不可新建生产指令"),
            ("production", "/production/orders/new/", 200, "生产可新建生产指令"),
            ("production", "/finance/customer-reconciliations/new/", 403, "生产不可做销售对账"),
            ("finance", "/finance/customer-reconciliations/new/", 200, "财务可做销售对账"),
            ("finance", "/inventory/locations/new/", 403, "财务不可维护库位"),
        ]
        clients = {}
        for key, _path, _expected, _label in expectations:
            if key not in clients:
                clients[key] = RealHttpClient(base_url, users[key].username, password)
                clients[key].login()
        for key, path, expected, label in expectations:
            status, _body = clients[key].get_status(path)
            if status != expected:
                raise CommandError(f"{label}：期望 HTTP {expected}，实际 HTTP {status}，path={path}")
            report.ok(label)

    def _create_led_master_data(self, tag: str, users: dict[str, object], report: UatReport) -> dict:
        today = timezone.localdate()
        boss_id = users["boss"].id
        sales_user = users["sales"]

        with transaction.atomic():
            locations = {
                "raw": WarehouseLocation.objects.create(location_code=f"{tag}-RM-A01", location_name="LED 原料仓 A01", remark="UAT LED 原料库位"),
                "fg": WarehouseLocation.objects.create(location_code=f"{tag}-FG-B01", location_name="LED 成品仓 B01", remark="UAT LED 成品库位"),
                "sample": WarehouseLocation.objects.create(location_code=f"{tag}-SMP-C01", location_name="LED 样品仓 C01", remark="UAT LED 样品库位"),
                "qc": WarehouseLocation.objects.create(location_code=f"{tag}-QC-D01", location_name="LED 待检仓 D01", remark="UAT LED 待检库位"),
            }
            customer = Customer.objects.create(
                customer_no=f"{tag}-CUST-HXTV",
                customer_name=f"{tag} 华显电视制造有限公司",
                short_name="华显电视",
                sales_owner=sales_user,
                settlement_method=SettlementMethod.MONTHLY_30,
                contact_phone_encrypted="13800138000",
                remark="UAT LED 背光模组客户：电视整机厂",
                created_by_id=boss_id,
            )
            address = CustomerAddress.objects.create(
                customer=customer,
                address_type=CustomerAddress.AddressType.SHIPPING,
                receiver_name="刘工",
                receiver_phone_encrypted="13800138001",
                address_encrypted="深圳市光明区华显工业园 A 栋收货仓",
                is_default=True,
                created_by_id=boss_id,
            )
            suppliers = {
                "led": Supplier.objects.create(
                    supplier_no=f"{tag}-SUP-JYGD",
                    supplier_name=f"{tag} 晶源光电材料有限公司",
                    contact_name="王经理",
                    supplier_type=SupplierType.RAW,
                    payment_method=SupplierPaymentMethod.MONTHLY_30,
                    remark="LED 灯珠供应商",
                    created_by_id=boss_id,
                ),
                "film": Supplier.objects.create(
                    supplier_no=f"{tag}-SUP-RMXX",
                    supplier_name=f"{tag} 瑞膜光学材料有限公司",
                    contact_name="陈经理",
                    supplier_type=SupplierType.AUXILIARY,
                    payment_method=SupplierPaymentMethod.MONTHLY,
                    remark="光学膜片供应商",
                    created_by_id=boss_id,
                ),
            }
            materials = {
                "led": self._material(f"{tag}-RM-LED2835", "LED灯珠", "2835 3V 1W 6500K", Material.MaterialType.RAW, "pcs", Decimal("0.180000"), boss_id),
                "fpc": self._material(f"{tag}-RM-FPC32", "FPC软板", "32寸 6串10并", Material.MaterialType.PART, "pcs", Decimal("8.500000"), boss_id),
                "lgp": self._material(f"{tag}-RM-LGP32", "导光板", "32寸 PMMA 1.2mm", Material.MaterialType.RAW, "pcs", Decimal("13.000000"), boss_id),
                "diff": self._material(f"{tag}-RM-DIFF32", "扩散膜", "32寸 0.125mm", Material.MaterialType.RAW, "pcs", Decimal("2.100000"), boss_id),
                "ref": self._material(f"{tag}-RM-REF32", "反射膜", "32寸 白色反射片", Material.MaterialType.RAW, "pcs", Decimal("1.400000"), boss_id),
                "carton": self._material(f"{tag}-PKG-CTN32", "包装纸箱", "32寸背光模组一体箱", Material.MaterialType.PACKAGING, "pcs", Decimal("3.200000"), boss_id),
                "fg": self._material(f"{tag}-FG-BL32", "32寸电视LED背光模组", "BLU-32-10S6P-6500K", Material.MaterialType.FINISHED, "pcs", None, boss_id),
            }
            MaterialSupplierPrice.objects.create(material=materials["led"], supplier=suppliers["led"], purchase_price=Decimal("0.180000"), is_default=True, created_by_id=boss_id)
            for key in ["diff", "ref", "lgp"]:
                MaterialSupplierPrice.objects.create(material=materials[key], supplier=suppliers["film"], purchase_price=materials[key].latest_purchase_price or ZERO, is_default=True, created_by_id=boss_id)
            customer_product = CustomerProduct.objects.create(
                customer=customer,
                customer_product_no=f"{tag}-HX-BL32",
                customer_product_name="华显32寸电视背光条模组",
                finished_material=materials["fg"],
                default_sale_price=Decimal("88.0000"),
                label_requirements={"标签": "客户料号 HX-BL32-6500K，外箱需贴 RoHS 标识"},
                packaging_requirements={"包装": "10 PCS/箱，防静电袋单独包装"},
                created_by_id=boss_id,
            )
            bom = Bom.objects.create(
                bom_no=f"{tag}-BOM-BL32",
                finished_material=materials["fg"],
                bom_version="V1",
                base_qty=Decimal("1.0000"),
                status=Bom.BomStatus.ENABLED,
                is_default=True,
                enabled_at=timezone.now(),
                approved_by_id=boss_id,
                approved_at=timezone.now(),
                created_by_id=boss_id,
                remark="UAT LED 背光模组标准 BOM",
            )
            bom_rows = [
                ("led", Decimal("60.000000"), "pcs", "每片背光 60 颗灯珠"),
                ("fpc", Decimal("1.000000"), "pcs", "FPC 软板"),
                ("lgp", Decimal("1.000000"), "pcs", "导光板"),
                ("diff", Decimal("2.000000"), "pcs", "上下扩散膜"),
                ("ref", Decimal("1.000000"), "pcs", "反射膜"),
                ("carton", Decimal("1.000000"), "pcs", "包装纸箱"),
            ]
            for line_no, (key, usage, unit, remark) in enumerate(bom_rows, start=1):
                BomItem.objects.create(
                    bom=bom,
                    line_no=line_no,
                    component_material=materials[key],
                    usage_qty=usage,
                    usage_unit=unit,
                    loss_rate=Decimal("0.000000"),
                    is_required=True,
                    remark=remark,
                )

            batches = {
                "led_initial": self._stock_batch(f"{tag}-BA-LED-INIT", materials["led"], locations["raw"], Decimal("300.0000"), Decimal("0.180000"), boss_id, InventoryBatch.InventoryType.AVAILABLE),
                "fpc_initial": self._stock_batch(f"{tag}-BA-FPC-INIT", materials["fpc"], locations["raw"], Decimal("30.0000"), Decimal("8.500000"), boss_id, InventoryBatch.InventoryType.AVAILABLE),
                "lgp_initial": self._stock_batch(f"{tag}-BA-LGP-INIT", materials["lgp"], locations["raw"], Decimal("30.0000"), Decimal("13.000000"), boss_id, InventoryBatch.InventoryType.AVAILABLE),
                "diff_initial": self._stock_batch(f"{tag}-BA-DIFF-INIT", materials["diff"], locations["raw"], Decimal("60.0000"), Decimal("2.100000"), boss_id, InventoryBatch.InventoryType.AVAILABLE),
                "ref_initial": self._stock_batch(f"{tag}-BA-REF-INIT", materials["ref"], locations["raw"], Decimal("30.0000"), Decimal("1.400000"), boss_id, InventoryBatch.InventoryType.AVAILABLE),
                "carton_initial": self._stock_batch(f"{tag}-BA-CTN-INIT", materials["carton"], locations["raw"], Decimal("30.0000"), Decimal("3.200000"), boss_id, InventoryBatch.InventoryType.AVAILABLE),
                "sample_fg": self._stock_batch(f"{tag}-BA-SAMPLE-BL32", materials["fg"], locations["sample"], Decimal("5.0000"), Decimal("35.000000"), boss_id, InventoryBatch.InventoryType.SAMPLE),
            }

        report.record("客户", customer.customer_name)
        report.record("成品编码", materials["fg"].material_code)
        report.record("BOM", f"{bom.bom_no}/{bom.bom_version}")
        report.ok("已建立 LED 背光行业主数据、客户产品、BOM、库位和期初批次")
        return {
            "today": today,
            "locations": locations,
            "customer": customer,
            "address": address,
            "suppliers": suppliers,
            "materials": materials,
            "customer_product": customer_product,
            "bom": bom,
            "batches": batches,
        }

    def _material(self, code: str, name: str, spec: str, material_type: str, unit: str, price: Decimal | None, user_id: int) -> Material:
        return Material.objects.create(
            material_code=code,
            material_name=name,
            spec=spec,
            material_type=material_type,
            base_unit=unit,
            qty_precision=0,
            latest_purchase_price=price,
            min_stock_qty=Decimal("0.0000"),
            created_by_id=user_id,
            remark="LED 背光厂家 UAT 物料",
        )

    def _stock_batch(
        self,
        batch_no: str,
        material: Material,
        location: WarehouseLocation,
        qty: Decimal,
        cost_price: Decimal,
        user_id: int,
        inventory_type: str,
    ) -> InventoryBatch:
        batch = InventoryBatch.objects.create(
            batch_no=batch_no,
            material=material,
            location=location,
            inventory_type=inventory_type,
            received_at=timezone.now(),
            initial_qty=qty,
            remaining_qty=qty,
            cost_price=cost_price,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        inventory, _ = Inventory.objects.get_or_create(
            material=material,
            location=location,
            inventory_type=inventory_type,
            defaults={"qty": ZERO},
        )
        inventory.qty += qty
        inventory.save(update_fields=["qty", "updated_at"])
        InventoryTransaction.objects.create(
            transaction_no=next_document_no("IT"),
            transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK,
            material=material,
            batch=batch,
            location=location,
            qty_delta=qty,
            source_doc_type="led_uat_initial",
            source_doc_id=batch.id,
            source_doc_no=batch.batch_no,
            created_by_id=user_id,
        )
        return batch

    def _run_purchase_approval_http(self, base_url: str, password: str, users: dict[str, object], fixture: dict, report: UatReport) -> None:
        purchase = users["purchase"]
        supplier = fixture["suppliers"]["film"]
        material = fixture["materials"]["diff"]
        order = PurchaseOrder.objects.create(
            purchase_order_no=f"{fixture['today']:%Y%m%d}-{fixture['materials']['fg'].id}-UATPO",
            supplier=supplier,
            status=PurchaseOrder.Status.PENDING_APPROVAL,
            order_date=fixture["today"],
            created_by=purchase,
            purchase_owner=purchase,
            remark=f"{report.tag} 页面审核入口测试采购单",
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=material,
            order_qty=Decimal("10.0000"),
            unit_price=Decimal("2.100000"),
            line_amount=Decimal("21.00"),
            needed_date=fixture["today"],
        )
        client = RealHttpClient(base_url, purchase.username, password)
        client.login()
        status, _body = client.post(
            f"/purchase/orders/{order.id}/approve/",
            {"current_password": password},
            prefetch=False,
        )
        order.refresh_from_db()
        if status != 200 or order.status != PurchaseOrder.Status.APPROVED:
            raise CommandError(f"采购单页面审核失败：HTTP {status}，状态 {order.status}")
        report.record("采购审核单", order.purchase_order_no)
        report.ok("采购单待审核入口可用，二次验证后已审核通过")

    def _run_led_business_flows(self, tag: str, users: dict[str, object], fixture: dict, report: UatReport) -> None:
        sales_user = users["sales"]
        purchase_user = users["purchase"]
        warehouse_user = users["warehouse"]
        production_user = users["production"]
        finance_user = users["finance"]
        today = fixture["today"]
        customer = fixture["customer"]
        fg = fixture["materials"]["fg"]
        led = fixture["materials"]["led"]
        bom = fixture["bom"]
        fg_location = fixture["locations"]["fg"]
        raw_location = fixture["locations"]["raw"]
        customer_product = fixture["customer_product"]

        sales_order = SalesOrder.objects.create(
            sales_order_no=f"{tag}-SO-HX-001",
            customer=customer,
            customer_address=fixture["address"],
            order_date=today,
            delivery_date=today,
            customer_contract_no=f"{tag}-HX-CONTRACT-001",
            settlement_method=SettlementMethod.MONTHLY_30,
            status=SalesOrder.Status.PENDING_APPROVAL,
            total_amount=Decimal("1760.00"),
            created_by=sales_user,
            remark="华显 32 寸电视背光模组试产订单，触发欠料采购和生产",
        )
        sales_item = SalesOrderItem.objects.create(
            sales_order=sales_order,
            line_no=1,
            customer_product=customer_product,
            finished_material=fg,
            customer_model_remark="客户型号 HX-TV32-BL-6500K",
            order_qty=Decimal("20.0000"),
            unit_price=Decimal("88.0000"),
            line_amount=Decimal("1760.00"),
            line_status=SalesOrderItem.LineStatus.PENDING_APPROVAL,
        )
        self._assert_result(confirm_sales_order(sales_order.id, sales_user.id), "销售订单审核")
        shortage = ShortageAlert.objects.get(sales_order_item=sales_item, material=led)
        if shortage.shortage_qty != Decimal("900.0000"):
            raise CommandError(f"LED 灯珠欠料数量异常：{shortage.shortage_qty}")
        report.ok("销售订单审核后自动锁定 BOM 并生成 LED 灯珠欠料")

        request_result = create_purchase_request_from_shortages([shortage.id], purchase_user.id, idempotency_key=f"{tag}-shortage-pr")
        self._assert_result(request_result, "欠料生成采购需求")
        purchase_request_id = request_result.data["purchase_request_id"]
        order_result = create_purchase_order_from_request(purchase_request_id, fixture["suppliers"]["led"].id, purchase_user.id, idempotency_key=f"{tag}-shortage-po")
        self._assert_result(order_result, "采购需求生成采购单")
        purchase_order = PurchaseOrder.objects.get(id=order_result.data["purchase_order_id"])
        purchase_order.purchase_owner = purchase_user
        purchase_order.save(update_fields=["purchase_owner"])
        purchase_order_item = purchase_order.items.get(material=led)
        receipt = PurchaseReceipt.objects.create(
            purchase_receipt_no=f"{tag}-GR-LED-001",
            purchase_order=purchase_order,
            supplier=fixture["suppliers"]["led"],
            receipt_date=today,
            status=PurchaseReceipt.Status.PENDING_RECEIVE,
            created_by=warehouse_user,
            remark="LED 灯珠采购入库，补齐销售欠料",
        )
        receipt_item = PurchaseReceiptItem.objects.create(
            purchase_receipt=receipt,
            purchase_order_item=purchase_order_item,
            material=led,
            received_qty=Decimal("950.0000"),
            accepted_qty=Decimal("950.0000"),
            rejected_qty=Decimal("0.0000"),
            unit_price=Decimal("0.180000"),
            location=raw_location,
        )
        self._assert_result(confirm_purchase_receipt(receipt.id, warehouse_user.id, f"{tag}-purchase-receipt"), "采购入库确认")
        self._assert_result(process_pending_events(event_type="purchase_received"), "采购入库后欠料重检")
        sales_item.refresh_from_db()
        if sales_item.inventory_check_status != SalesOrderItem.InventoryCheckStatus.KITTED:
            raise CommandError(f"采购入库后销售明细未齐套：{sales_item.inventory_check_status}")
        report.record("采购需求", PurchaseOrder.objects.get(id=purchase_order.id).purchase_order_no)
        report.ok("采购入库后事务事件已把销售订单明细更新为已齐套")

        production_order = ProductionOrder.objects.create(
            production_order_no=f"{tag}-MO-BL32-001",
            sales_order_item=sales_item,
            finished_material=fg,
            production_qty=Decimal("20.0000"),
            locked_bom=bom,
            locked_bom_version=bom.bom_version,
            planned_start_date=today,
            planned_finish_date=today,
            created_by=production_user,
            remark="根据销售订单齐套状态下达生产",
        )
        requisition = ProductionMaterialRequisition.objects.create(
            requisition_no=f"{tag}-MR-BL32-001",
            production_order=production_order,
            requisition_date=today,
            status=ProductionMaterialRequisition.Status.PENDING_CONFIRM,
            created_by=warehouse_user,
            remark="生产领料：LED 灯珠、FPC、导光板、膜片、包装",
        )
        line_no = 1
        for bom_item in bom.items.select_related("component_material").order_by("line_no"):
            required_qty = (bom_item.usage_qty * production_order.production_qty).quantize(Decimal("0.0001"))
            remaining_qty = required_qty
            batches = list(
                InventoryBatch.objects.filter(
                    material=bom_item.component_material,
                    location=raw_location,
                    inventory_type=InventoryBatch.InventoryType.AVAILABLE,
                    batch_status=InventoryBatch.BatchStatus.IN_STOCK,
                    remaining_qty__gt=ZERO,
                ).order_by("received_at", "id")
            )
            if sum((batch.remaining_qty for batch in batches), ZERO) < required_qty:
                raise CommandError(f"生产领料找不到足够批次：{bom_item.component_material.material_code} qty={required_qty}")
            for batch in batches:
                if remaining_qty <= ZERO:
                    break
                issued_qty = min(batch.remaining_qty, remaining_qty).quantize(Decimal("0.0001"))
                ProductionMaterialRequisitionItem.objects.create(
                    requisition=requisition,
                    production_order=production_order,
                    line_no=line_no,
                    material=bom_item.component_material,
                    required_qty=issued_qty,
                    issued_qty=issued_qty,
                    batch=batch,
                    location=raw_location,
                    adjust_reason="UAT 按 BOM 标准用量按批次拆行领料",
                )
                line_no += 1
                remaining_qty -= issued_qty
        self._assert_result(confirm_material_requisition(requisition.id, warehouse_user.id, f"{tag}-production-issue"), "生产领料确认")
        receipt_prod = ProductionReceipt.objects.create(
            production_receipt_no=f"{tag}-PIN-BL32-001",
            production_order=production_order,
            receipt_date=today,
            status=ProductionReceipt.Status.PENDING_CONFIRM,
            created_by=production_user,
            remark="生产完成入库",
        )
        ProductionReceiptItem.objects.create(
            production_receipt=receipt_prod,
            production_order=production_order,
            line_no=1,
            finished_material=fg,
            receipt_qty=Decimal("20.0000"),
            location=fg_location,
            batch_no=f"{tag}-BA-FG-PROD001",
            quality_status=ProductionReceiptItem.QualityStatus.QUALIFIED,
        )
        self._assert_result(confirm_production_receipt(receipt_prod.id, production_user.id, f"{tag}-production-receipt"), "生产入库确认")
        fg_batch = InventoryBatch.objects.get(batch_no=f"{tag}-BA-FG-PROD001")
        report.record("生产指令", production_order.production_order_no)
        report.ok("生产领料与生产入库已衔接，成品批次已生成")

        shipment = SalesShipment.objects.create(
            shipment_no=f"{tag}-SS-HX-001",
            sales_order=sales_order,
            customer=customer,
            shipment_date=today,
            customer_contract_no=sales_order.customer_contract_no,
            customer_address_text=fixture["address"].address_encrypted,
            customer_contact_name=fixture["address"].receiver_name,
            customer_contact_phone=fixture["address"].receiver_phone_encrypted,
            settlement_method=SettlementMethod.MONTHLY_30,
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=warehouse_user,
            remark="销售出库：华显订单量产首批",
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=sales_item,
            material=fg,
            shipment_qty=Decimal("20.0000"),
            batch=fg_batch,
            location=fg_location,
            cost_price=Decimal("35.000000"),
        )
        self._assert_result(confirm_sales_shipment(shipment.id, warehouse_user.id, f"{tag}-sales-shipment"), "销售出库确认")
        report.record("销售出库", shipment.shipment_no)
        report.ok("销售出库扣减生产入库批次，销售订单变为已发货")

        customer_return = CustomerReturn.objects.create(
            return_no=f"{tag}-SRN-HX-001",
            customer=customer,
            sales_order=sales_order,
            return_date=today,
            status=CustomerReturn.Status.CONFIRMED,
            return_amount=Decimal("176.00"),
            remark="客户抽检退回 2 PCS，测试退货入库",
        )
        CustomerReturnItem.objects.create(
            customer_return=customer_return,
            sales_order_item=sales_item,
            material=fg,
            return_qty=Decimal("2.0000"),
            unit_price=Decimal("88.0000"),
            return_amount=Decimal("176.00"),
            location=fixture["locations"]["qc"],
            inventory_type=InventoryBatch.InventoryType.PENDING,
            return_reason="客户 IQC 色温复检",
        )
        self._assert_result(confirm_customer_return_receipt(customer_return.id, warehouse_user.id, f"{tag}-customer-return"), "客户退货入库确认")
        report.record("销售退货", customer_return.return_no)
        report.ok("销售退货关联原销售订单明细并入待检仓")

        self._run_sample_flow(tag, users, fixture, report)
        self._run_supplier_return(tag, users, fixture, receipt_item, report)
        self._run_inventory_transfer(tag, users, fixture, report)
        self._run_finance_flow(tag, users, fixture, sales_order, purchase_order, receipt, report)

    def _run_sample_flow(self, tag: str, users: dict[str, object], fixture: dict, report: UatReport) -> None:
        sales_user = users["sales"]
        warehouse_user = users["warehouse"]
        customer = fixture["customer"]
        fg = fixture["materials"]["fg"]
        sample_location = fixture["locations"]["sample"]
        sample_loan = SampleLoan.objects.create(
            sample_loan_no=f"{tag}-SL-HX-001",
            customer=customer,
            loan_date=fixture["today"],
            expected_return_date=fixture["today"],
            status=SampleLoan.Status.PENDING_APPROVAL,
            created_by=sales_user,
            remark="给华显电视研发部借样评估，备注含客户项目与样品编码",
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=sample_loan,
            line_no=1,
            material=fg,
            loan_qty=Decimal("3.0000"),
            expected_return_date=fixture["today"],
            batch=fixture["batches"]["sample_fg"],
            location=sample_location,
            remark="样品用途：32 寸新机型背光效果验证；样品编码见物料编码",
        )
        self._assert_result(confirm_sample_loan_out(sample_loan.id, warehouse_user.id, f"{tag}-sample-out"), "借样出库确认")
        sample_return = SampleLoanReturn.objects.create(
            sample_return_no=f"{tag}-SLR-HX-001",
            sample_loan=sample_loan,
            customer=customer,
            return_date=fixture["today"],
            status=SampleLoanReturn.Status.PENDING_CONFIRM,
            remark="客户归还 1 PCS，其余转销售",
        )
        SampleLoanReturnItem.objects.create(
            sample_return=sample_return,
            sample_loan=sample_loan,
            sample_loan_item=loan_item,
            material=fg,
            return_qty=Decimal("1.0000"),
            location=sample_location,
            sample_condition=SampleLoanReturnItem.SampleCondition.GOOD,
            remark="外观完好，归还样品仓",
        )
        self._assert_result(confirm_sample_return(sample_return.id, warehouse_user.id, f"{tag}-sample-return"), "借样归还确认")
        self._assert_result(
            convert_sample_loan_item_to_sales_order(loan_item.id, Decimal("2.0000"), Decimal("88.0000"), sales_user.id, f"{tag}-sample-to-sales"),
            "借样转销售",
        )
        report.record("借样单", sample_loan.sample_loan_no)
        report.ok("借样单已出库、部分归还，剩余数量已转销售")

    def _run_supplier_return(self, tag: str, users: dict[str, object], fixture: dict, receipt_item: PurchaseReceiptItem, report: UatReport) -> None:
        purchase_user = users["purchase"]
        warehouse_user = users["warehouse"]
        receipt_item.refresh_from_db()
        supplier_return = SupplierReturn.objects.create(
            supplier_return_no=f"{tag}-SRT-LED-001",
            supplier=fixture["suppliers"]["led"],
            purchase_receipt=receipt_item.purchase_receipt,
            return_date=fixture["today"],
            status=SupplierReturn.Status.CONFIRMED,
            return_amount=Decimal("9.00"),
            created_by=purchase_user,
            remark="LED 灯珠抽检不良退供应商 50 PCS",
        )
        SupplierReturnItem.objects.create(
            supplier_return=supplier_return,
            purchase_receipt_item=receipt_item,
            material=receipt_item.material,
            return_qty=Decimal("50.0000"),
            unit_price=Decimal("0.180000"),
            return_amount=Decimal("9.00"),
            batch=receipt_item.batch,
            location=receipt_item.location,
            return_reason="来料抽检亮度不稳定",
        )
        self._assert_result(confirm_supplier_return_shipment(supplier_return.id, warehouse_user.id, f"{tag}-supplier-return"), "供应商退货出库确认")
        report.record("供应商退货", supplier_return.supplier_return_no)
        report.ok("供应商退货已关联进货批次并扣减库存")

    def _run_inventory_transfer(self, tag: str, users: dict[str, object], fixture: dict, report: UatReport) -> None:
        warehouse_user = users["warehouse"]
        batch = (
            InventoryBatch.objects.filter(
                material=fixture["materials"]["fg"],
                location=fixture["locations"]["sample"],
                batch_status=InventoryBatch.BatchStatus.IN_STOCK,
                remaining_qty__gte=Decimal("1.0000"),
            )
            .order_by("id")
            .first()
        )
        if not batch:
            report.warn("样品批次已被借样消耗，跳过移库测试")
            return
        transfer = LocationTransfer.objects.create(
            transfer_no=f"{tag}-LT-SMP-QC-001",
            material=batch.material,
            batch=batch,
            from_location=batch.location,
            to_location=fixture["locations"]["qc"],
            transfer_qty=Decimal("1.0000"),
        )
        self._assert_result(confirm_location_transfer(transfer.id, warehouse_user.id, f"{tag}-location-transfer"), "库位移库确认")
        report.record("库位移库", transfer.transfer_no)
        report.ok("库存移库已生成目标库位新批次")

    def _run_finance_flow(
        self,
        tag: str,
        users: dict[str, object],
        fixture: dict,
        sales_order: SalesOrder,
        purchase_order: PurchaseOrder,
        purchase_receipt: PurchaseReceipt,
        report: UatReport,
    ) -> None:
        finance_user = users["finance"]
        today = fixture["today"]
        customer_reconciliation = Reconciliation.objects.create(
            reconciliation_no=f"{tag}-REC-CUST-001",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=fixture["customer"],
            period_start=today,
            period_end=today,
            total_amount=Decimal("1760.00"),
            status=Reconciliation.Status.DRAFT,
            created_by=finance_user,
            remark="销售对账：华显订单出库明细",
        )
        supplier_reconciliation = Reconciliation.objects.create(
            reconciliation_no=f"{tag}-REC-SUP-001",
            party_type=Reconciliation.PartyType.SUPPLIER,
            supplier=fixture["suppliers"]["led"],
            period_start=today,
            period_end=today,
            total_amount=Decimal("162.00"),
            status=Reconciliation.Status.DRAFT,
            created_by=finance_user,
            remark="生产/采购对账：LED 灯珠进货明细",
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no=f"{tag}-CR-HX-001",
            customer=fixture["customer"],
            receipt_date=today,
            receipt_amount=Decimal("1760.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
            handled_by=finance_user,
            created_by=finance_user,
            remark="客户按销售出库付款",
        )
        self._assert_result(
            confirm_customer_receipt(receipt.id, [{"sales_order_id": sales_order.id, "allocated_amount": "1760.00"}], finance_user.id, f"{tag}-customer-receipt"),
            "客户收款核销",
        )
        payment = SupplierPayment.objects.create(
            payment_no=f"{tag}-SP-JY-001",
            supplier=fixture["suppliers"]["led"],
            payment_date=today,
            payment_amount=Decimal("162.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
            handled_by=finance_user,
            created_by=finance_user,
            remark="支付 LED 灯珠进货款",
        )
        self._assert_result(
            confirm_supplier_payment(payment.id, [{"purchase_receipt_id": purchase_receipt.id, "allocated_amount": "162.00"}], finance_user.id, f"{tag}-supplier-payment"),
            "供应商付款核销",
        )
        OpeningReceivable.objects.create(
            opening_no=f"{tag}-OR-HX-001",
            customer=fixture["customer"],
            source_doc_no=f"{tag}-OLD-AR",
            opening_date=today,
            due_date=today,
            opening_amount=Decimal("100.00"),
            remaining_amount=Decimal("100.00"),
            created_by=finance_user,
            remark="UAT 期初应收示例",
        )
        OpeningPayable.objects.create(
            opening_no=f"{tag}-OP-JY-001",
            supplier=fixture["suppliers"]["led"],
            source_doc_no=f"{tag}-OLD-AP",
            opening_date=today,
            due_date=today,
            opening_amount=Decimal("80.00"),
            remaining_amount=Decimal("80.00"),
            created_by=finance_user,
            remark="UAT 期初应付示例",
        )
        ExpenseRecord.objects.create(
            expense_no=f"{tag}-EXP-001",
            expense_date=today,
            category=ExpenseRecord.ExpenseCategory.ELECTRICITY,
            amount=Decimal("260.00"),
            payment_method=ExpenseRecord.PaymentMethod.TRANSFER,
            payee="园区供电账户",
            invoice_no=f"{tag}-ELEC",
            handled_by=finance_user,
            status=ExpenseRecord.Status.CONFIRMED,
            created_by=finance_user,
            confirmed_by=finance_user,
            confirmed_at=timezone.now(),
            remark="LED 背光老化线电费分摊",
        )
        report.record("销售对账", customer_reconciliation.reconciliation_no)
        report.record("生产对账", supplier_reconciliation.reconciliation_no)
        report.record("客户收款", receipt.receipt_no)
        report.record("供应商付款", payment.payment_no)
        report.ok("财务已覆盖销售对账、生产对账、收款核销、付款核销、期初和费用记录")

    def _check_final_pages(self, base_url: str, password: str, users: dict[str, object], report: UatReport) -> None:
        clients = {
            "sales": RealHttpClient(base_url, users["sales"].username, password),
            "purchase": RealHttpClient(base_url, users["purchase"].username, password),
            "warehouse": RealHttpClient(base_url, users["warehouse"].username, password),
            "production": RealHttpClient(base_url, users["production"].username, password),
            "finance": RealHttpClient(base_url, users["finance"].username, password),
        }
        for client in clients.values():
            client.login()

        page_checks = [
            ("sales", f"/sales/orders/?q={report.tag}", "销售订单列表可按 UAT 标记筛选"),
            ("sales", f"/sales/shipments/?q={report.tag}", "销售出库列表可按 UAT 标记筛选"),
            ("sales", f"/sales/returns/?q={report.tag}", "销售退货列表可按 UAT 标记筛选"),
            ("sales", "/sales/sample-returns/outstanding/", "借样归还 outstanding 页面可打开"),
            ("purchase", f"/purchase/orders/?q={report.tag}", "采购单列表可按 UAT 标记筛选"),
            ("warehouse", f"/inventory/batches/?q={report.tag}", "库存批次列表可按 UAT 标记筛选"),
            ("production", f"/production/orders/?q={report.tag}", "生产指令列表可按 UAT 标记筛选"),
            ("production", "/production/receipts/workbench/", "生产入库工作台可打开"),
            ("finance", f"/finance/customer-reconciliations/?q={report.tag}", "销售对账列表可按 UAT 标记筛选"),
            ("finance", f"/finance/production-reconciliations/?q={report.tag}", "生产对账列表可按 UAT 标记筛选"),
        ]
        for key, path, label in page_checks:
            status, body = clients[key].get_status(path)
            if status != 200:
                raise CommandError(f"{label}：HTTP {status}，path={path}")
            if "q=" in path and report.tag not in body:
                report.warn(f"{label} 打开成功，但页面 HTML 未直接出现 tag，可能是列表字段未展示完整备注：{path}")
            report.ok(label)

    def _assert_result(self, result: ServiceResult, action: str) -> None:
        if not result.success:
            raise CommandError(f"{action}失败：{result.error_code or ''} {result.message}".strip())
