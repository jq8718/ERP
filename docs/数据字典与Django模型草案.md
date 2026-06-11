# 数据字典与 Django Model 草案

本文档是 `ERP模块建设计划.md` 的开发附件，用于正式建表、写 Django migration 和评审字段口径。

当前版本定位为“建表前草案”：先明确核心表、字段、外键、状态、唯一约束和索引。正式编码前，每张表还需要在本文件基础上补齐字段长度、默认值、null/blank、verbose_name、help_text 和 migration 说明。

## 1. 通用约定

### 1.1 命名约定

- 数据库表名使用小写复数形式，例如 `sales_orders`。
- 字段名使用小写下划线，例如 `created_at`。
- 业务状态保存英文枚举码，页面显示中文。
- 金额字段后缀使用 `_amount`，单价字段后缀使用 `_price`。
- 数量字段后缀使用 `_qty`，日期字段后缀使用 `_date`。
- 业务单号字段后缀使用 `_no`。

### 1.2 通用字段

核心业务表建议包含以下审计字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BigAutoField | 主键 |
| `created_at` | DateTimeField | 创建时间 |
| `created_by` | ForeignKey(User) | 创建人 |
| `updated_at` | DateTimeField | 修改时间 |
| `updated_by` | ForeignKey(User) | 修改人 |
| `approved_at` | DateTimeField, nullable | 审核时间 |
| `approved_by` | ForeignKey(User), nullable | 审核人 |
| `voided_at` | DateTimeField, nullable | 作废时间 |
| `voided_by` | ForeignKey(User), nullable | 作废人 |
| `status` | CharField | 业务状态英文编码 |
| `version` | PositiveIntegerField | 乐观锁版本号 |
| `remark` | TextField, blank | 备注 |

建议抽象基类：

```python
class AuditModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True
```

### 1.3 精度约定

| 类型 | Django 字段建议 | 说明 |
| --- | --- | --- |
| 金额 | DecimalField(max_digits=14, decimal_places=2) | 人民币金额 |
| 销售单价 | DecimalField(max_digits=14, decimal_places=4) | 销售价格 |
| 采购单价 / 成本 | DecimalField(max_digits=14, decimal_places=6) | 成本精度 |
| 数量 | DecimalField(max_digits=14, decimal_places=4) | 展示按物料精度 |
| BOM 用量 | DecimalField(max_digits=14, decimal_places=6) | 精细用量 |
| 损耗率 | DecimalField(max_digits=7, decimal_places=6) | 例如 0.020000 |
| 换算比例 | DecimalField(max_digits=18, decimal_places=8) | 单位换算 |

### 1.4 删除策略

- 核心业务单据不物理删除，使用状态 `voided` 或逻辑删除字段。
- 基础资料停用使用 `status=inactive`，历史单据仍可查询。
- 附件使用逻辑删除，已确认单据附件删除需要权限或审批。

## 2. Django App 与表分组

| App | 主要表 |
| --- | --- |
| `accounts` | `users`、`roles`、`permissions`、`user_sessions` |
| `masterdata` | `customers`、`customer_products`、`customer_addresses`、`suppliers`、`materials`、`material_unit_conversions`、`material_supplier_prices` |
| `bom` | `boms`、`bom_items` |
| `sales` | `sales_orders`、`sales_order_items`、`sales_order_change_logs`、`customer_returns`、`customer_return_items`、`sample_loans`、`sample_loan_items`、`sample_loan_returns`、`sales_shipments`、`shortage_alerts` |
| `purchase` | `purchase_requests`、`purchase_request_items`、`purchase_orders`、`purchase_receipts`、`supplier_returns`、`supplier_return_items` |
| `inventory` | `warehouse_locations`、`inventory`、`inventory_batches`、`inventory_transactions`、`location_transfers`、`stock_counts` |
| `production` | `production_orders`、`production_material_requisitions`、`production_receipts` |
| `finance` | `customer_receipts`、`supplier_payments`、`credit_balances`、`reconciliations` |
| `approvals` | `approval_rules`、`approvals`、`approval_logs` |
| `notifications` | `system_messages` |
| `files` | `attachments`、`import_jobs`、`initialization_jobs`、`export_logs`、`print_logs` |
| `system` | `document_sequences`、`pending_events`、`background_jobs`、`system_settings`、`saved_filters`、`audit_logs`、`backups`、`release_records` |

## 3. accounts

### 3.1 users

建议基于 Django `AbstractUser` 扩展。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `username` | CharField | 登录名 |
| `display_name` | CharField | 展示姓名 |
| `department` | CharField | 部门 |
| `position` | CharField | 岗位 |
| `security_level` | CharField | L1/L2/L3/L4 |
| `status` | CharField | active/inactive/locked |
| `is_deleted` | BooleanField | 是否逻辑删除，默认 false |

索引：

- `username` 唯一。
- `status`。

### 3.2 roles / permissions

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `roles` | `role_code`、`role_name`、`status` | 角色 |
| `permissions` | `permission_code`、`permission_name`、`permission_type` | 权限码 |

唯一约束：

- `roles.role_code`。
- `permissions.permission_code`。

### 3.3 user_sessions

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user` | FK(User) | 用户 |
| `session_key` | CharField | Django Session Key |
| `ip_address` | GenericIPAddressField | IP |
| `user_agent` | TextField | 浏览器信息 |
| `created_at` | DateTimeField | 创建时间 |
| `last_seen_at` | DateTimeField | 最近访问时间 |
| `revoked_at` | DateTimeField | 强制失效时间 |
| `status` | CharField | active/revoked/expired |

## 4. masterdata

### 4.1 materials

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `material_code` | CharField | 物料编码，允许字母数字，唯一 |
| `material_name` | CharField | 名称 |
| `material_type` | CharField | finished/raw/part/packaging/other |
| `spec` | CharField | 规格 |
| `base_unit` | CharField | 库存基础单位 |
| `qty_precision` | PositiveSmallIntegerField | 数量精度 |
| `default_location` | FK(WarehouseLocation) | 默认库位，可选 |
| `min_stock_qty` | DecimalField | 最低库存 |
| `latest_purchase_price` | DecimalField | 最新采购价，可选 |
| `status` | CharField | active/inactive |

唯一约束：

- `material_code`。

索引：

- `material_type`、`status`。
- `material_code`、`material_name` 模糊查询。

### 4.2 material_unit_conversions

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `material` | FK(Material) | 物料 |
| `source_unit` | CharField | 来源单位 |
| `target_unit` | CharField | 目标单位 |
| `ratio` | DecimalField(18, 8) | 换算比例 |
| `status` | CharField | active/inactive |

唯一约束：

- `material + source_unit + target_unit`。

### 4.3 customers

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `customer_no` | CharField | 客户编号 |
| `customer_name` | CharField | 客户名称 |
| `short_name` | CharField | 简称 |
| `sales_owner` | FK(User) | 业务员 |
| `settlement_method` | CharField | 月结/现结/自定义 |
| `contact_phone_encrypted` | TextField | 加密电话，可选 |
| `status` | CharField | active/inactive/blacklist |

唯一约束：

- `customer_no`。

索引：

- `customer_name`、`sales_owner`、`status`。

### 4.4 customer_products

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `customer` | FK(Customer) | 客户 |
| `customer_product_no` | CharField | 客户产品编号 |
| `customer_product_name` | CharField | 客户产品名称 |
| `finished_material` | FK(Material) | 关联成品编码 |
| `default_sale_price` | DecimalField | 默认销售价 |
| `label_requirements` | JSONField/TextField | 标签要求 |
| `packaging_requirements` | JSONField/TextField | 包装要求 |
| `status` | CharField | active/inactive |

唯一约束：

- `customer + customer_product_no`。

### 4.5 customer_addresses

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `customer` | FK(Customer) | 客户 |
| `receiver_name` | CharField | 收件人 |
| `receiver_phone_encrypted` | TextField | 加密电话 |
| `address_encrypted` | TextField | 加密地址 |
| `is_default` | BooleanField | 是否默认 |
| `status` | CharField | active/inactive |

### 4.6 suppliers

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `supplier_no` | CharField | 供应商编号 |
| `supplier_name` | CharField | 供应商名称 |
| `contact_name` | CharField | 联系人 |
| `contact_phone_encrypted` | TextField | 加密电话 |
| `supplier_type` | CharField | 原料/配件/包装/外协 |
| `payment_method` | CharField | 月结/现结/预付 |
| `status` | CharField | active/inactive/blacklist |

唯一约束：

- `supplier_no`。

### 4.7 material_supplier_prices

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `material` | FK(Material) | 物料 |
| `supplier` | FK(Supplier) | 供应商 |
| `purchase_price` | DecimalField(14, 6) | 采购价 |
| `currency` | CharField | 币种，默认 CNY |
| `effective_from` | DateField | 生效日期 |
| `effective_to` | DateField | 失效日期，可选 |
| `is_default` | BooleanField | 默认价格 |
| `status` | CharField | active/inactive |

## 5. bom

### 5.1 boms

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `bom_no` | CharField | BOM 编号 |
| `finished_material` | FK(Material) | 成品 |
| `bom_version` | CharField | 版本号 |
| `status` | CharField | draft/pending_approval/rejected/enabled/disabled/voided |
| `enabled_at` | DateTimeField | 启用时间 |
| `disabled_at` | DateTimeField | 停用时间 |

唯一约束：

- 条件唯一：`finished_material + bom_version`，限定 `status NOT IN ('rejected', 'voided')`。

### 5.2 bom_items

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `bom` | FK(Bom) | BOM |
| `line_no` | PositiveIntegerField | 行号 |
| `component_material` | FK(Material) | 子件 |
| `usage_qty` | DecimalField(14, 6) | 单件用量 |
| `usage_unit` | CharField | 用量单位 |
| `loss_rate` | DecimalField(7, 6) | 损耗率 |
| `is_required` | BooleanField | 是否必需 |
| `remark` | TextField | 备注 |

唯一约束：

- `bom + line_no`。

## 6. sales

### 6.1 sales_orders

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sales_order_no` | CharField | 销售订单号 |
| `customer` | FK(Customer) | 客户 |
| `customer_address` | FK(CustomerAddress) | 收件地址 |
| `order_date` | DateField | 下单日期 |
| `delivery_date` | DateField | 交期 |
| `status` | CharField | draft/pending_approval/rejected/pending_bom/confirmed/in_production/shipped/completed/voided |
| `total_amount` | DecimalField | 订单金额 |
| `contract_attachment_count` | PositiveIntegerField | 合同附件数 |

唯一约束：

- `sales_order_no`。

索引：

- `customer`、`status`、`delivery_date`、`created_at`。

### 6.2 sales_order_items

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sales_order` | FK(SalesOrder) | 订单 |
| `line_no` | PositiveIntegerField | 行号 |
| `customer_product` | FK(CustomerProduct) | 客户产品 |
| `finished_material` | FK(Material) | 成品 |
| `order_qty` | DecimalField | 订单数量 |
| `shipped_qty` | DecimalField | 已发货数量，服务统一维护 |
| `unit_price` | DecimalField(14, 4) | 单价 |
| `line_amount` | DecimalField | 行金额 |
| `locked_bom` | FK(Bom) | 锁定 BOM |
| `locked_bom_version` | CharField | 锁定 BOM 版本 |
| `line_status` | CharField | draft/pending_approval/confirmed/in_production/shipped/completed |
| `inventory_check_status` | CharField | unchecked/sufficient/pending_bom/shortage/kitted |

唯一约束：

- `sales_order + customer_product`，如需拆行则增加拆分原因字段后再放开。
- `sales_order + line_no`。

### 6.3 sales_order_change_logs

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sales_order` | FK(SalesOrder) | 销售订单 |
| `changed_by` | FK(User) | 变更人 |
| `changed_at` | DateTimeField | 变更时间 |
| `change_reason` | TextField | 变更原因 |
| `before_snapshot` | JSONField | 变更前快照 |
| `after_snapshot` | JSONField | 变更后快照 |
| `approval` | FK(Approval), nullable | 关联审批 |

索引：

- `sales_order + changed_at`。

### 6.4 customer_returns

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `return_no` | CharField | 客户退货单号 |
| `customer` | FK(Customer) | 客户 |
| `sales_order` | FK(SalesOrder) | 来源销售单，可选 |
| `return_date` | DateField | 退货日期 |
| `status` | CharField | draft/pending_approval/rejected/confirmed/received/voided |
| `return_amount` | DecimalField | 退货金额 |

### 6.5 customer_return_items

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `customer_return` | FK(CustomerReturn) | 客户退货单 |
| `sales_order_item` | FK(SalesOrderItem), nullable | 来源销售订单明细 |
| `material` | FK(Material) | 退货成品 |
| `return_qty` | DecimalField | 退货数量 |
| `unit_price` | DecimalField(14, 4) | 退货单价 |
| `return_amount` | DecimalField(14, 2) | 退货金额 |
| `location` | FK(WarehouseLocation), nullable | 入库库位 |
| `inventory_type` | CharField | available/pending/defective |
| `return_reason` | CharField/TextField | 退货原因 |

唯一约束：

- `customer_return + material + sales_order_item`。

### 6.6 sample_loans

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sample_loan_no` | CharField | 借样单号 |
| `customer` | FK(Customer) | 客户 |
| `loan_date` | DateField | 借出日期 |
| `expected_return_date` | DateField | 预计归还日期 |
| `status` | CharField | pending_approval/out/part_returned/returned/part_sold/sold/voided |
| `is_overdue` | BooleanField | 是否逾期 |
| `overdue_days` | PositiveIntegerField | 逾期天数 |
| `overdue_status` | CharField | none/due_soon/overdue/closed |

### 6.7 sample_loan_items

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sample_loan` | FK(SampleLoan) | 借样单 |
| `line_no` | PositiveIntegerField | 行号 |
| `material` | FK(Material) | 借样成品 |
| `loan_qty` | DecimalField | 借出数量 |
| `returned_qty` | DecimalField | 已归还数量 |
| `sold_qty` | DecimalField | 已转销售数量 |
| `expected_return_date` | DateField | 明细预计归还日期，可覆盖单头日期 |
| `batch` | FK(InventoryBatch), nullable | 借出批次 |
| `location` | FK(WarehouseLocation), nullable | 借出库位 |
| `line_status` | CharField | out/part_returned/returned/part_sold/sold/voided |

唯一约束：

- `sample_loan + line_no`。
- `sample_loan + material` 默认不重复，如需同物料不同批次多行，按 `line_no` 区分。

### 6.8 sample_loan_returns / sample_loan_return_items

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `sample_loan_returns` | `sample_return_no`、`sample_loan`、`customer`、`return_date`、`status` | 借样归还单 |
| `sample_loan_return_items` | `sample_return`、`sample_loan_item`、`material`、`return_qty`、`location`、`inventory_type`、`sample_condition` | 归还明细 |

`sample_loan_return_items` 字段展开：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sample_return` | FK(SampleLoanReturn) | 归还单 |
| `sample_loan` | FK(SampleLoan) | 借样单 |
| `sample_loan_item` | FK(SampleLoanItem) | 借样明细 |
| `material` | FK(Material) | 成品 |
| `return_qty` | DecimalField | 归还数量 |
| `location` | FK(WarehouseLocation) | 入库库位 |
| `inventory_type` | CharField | available/pending/defective |
| `sample_condition` | CharField | good/damaged/pending_check/missing_part |
| `remark` | TextField | 说明 |

唯一约束：

- `sample_loan_returns.sample_return_no`。
- `sample_loan_return_items.sample_return + sample_loan_item + location`。

### 6.9 sales_shipments / sales_shipment_items

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `sales_shipments` | `shipment_no`、`sales_order`、`customer`、`shipment_date`、`status` | 销售出库单 |
| `sales_shipment_items` | `shipment`、`sales_order_item`、`material`、`shipment_qty`、`batch`、`location` | 出库明细 |

`sales_shipment_items` 字段展开：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `shipment` | FK(SalesShipment) | 出库单 |
| `sales_order_item` | FK(SalesOrderItem) | 销售订单明细 |
| `material` | FK(Material) | 成品 |
| `shipment_qty` | DecimalField | 出库数量 |
| `batch` | FK(InventoryBatch) | 出库批次 |
| `location` | FK(WarehouseLocation) | 库位 |
| `cost_price` | DecimalField(14, 6) | 批次成本快照 |

唯一约束：

- `sales_shipments.shipment_no`。
- `sales_shipment_items.shipment + sales_order_item + batch`。

### 6.10 shortage_alerts

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `shortage_no` | CharField | 欠料提醒编号 |
| `sales_order` | FK(SalesOrder) | 来源销售订单 |
| `sales_order_item` | FK(SalesOrderItem) | 来源销售明细 |
| `material` | FK(Material) | 缺料物料 |
| `required_qty` | DecimalField | 需求数量 |
| `available_qty` | DecimalField | 可用库存 |
| `shortage_qty` | DecimalField | 缺口数量 |
| `is_required` | BooleanField | 是否必需子件 |
| `status` | CharField | unprocessed/purchase_requested/partial_received/kitted/closed |
| `purchase_request` | FK(PurchaseRequest), nullable | 已生成采购需求 |
| `closed_reason` | TextField | 关闭原因 |

唯一约束：

- `sales_order_item + material + status` 可按业务改为部分唯一，避免同一未关闭欠料重复生成。

索引：

- `status + material`。
- `sales_order_item + status`。

## 7. purchase

### 7.1 purchase_requests

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `purchase_request_no` | CharField | 采购需求号 |
| `source_type` | CharField | manual/shortage |
| `status` | CharField | draft/pending_approval/rejected/approved/closed/voided |
| `requested_by` | FK(User) | 申请人 |
| `needed_date` | DateField | 需求日期 |

### 7.2 purchase_request_items

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `purchase_request` | FK(PurchaseRequest) | 采购需求 |
| `line_no` | PositiveIntegerField | 行号 |
| `material` | FK(Material) | 需求物料 |
| `request_qty` | DecimalField | 需求数量 |
| `suggested_supplier` | FK(Supplier), nullable | 建议供应商 |
| `needed_date` | DateField | 需求日期 |
| `source_shortage_alert` | FK(ShortageAlert), nullable | 来源欠料提醒 |
| `source_sales_order_item` | FK(SalesOrderItem), nullable | 来源销售订单明细 |
| `line_status` | CharField | open/ordered/partial_ordered/closed |

唯一约束：

- `purchase_request + line_no`。
- `purchase_request + material + source_shortage_alert`，避免同一欠料重复生成同一需求明细。

### 7.3 purchase_orders / purchase_order_items

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `purchase_orders` | `purchase_order_no`、`supplier`、`status`、`order_date`、`total_amount` | 采购单 |
| `purchase_order_items` | `purchase_order`、`material`、`order_qty`、`received_qty`、`unit_price` | 采购明细 |

`purchase_order_items` 字段展开：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `purchase_order` | FK(PurchaseOrder) | 采购单 |
| `purchase_request_item` | FK(PurchaseRequestItem), nullable | 来源采购需求明细 |
| `material` | FK(Material) | 物料 |
| `order_qty` | DecimalField | 采购数量 |
| `received_qty` | DecimalField | 已到货数量，服务统一维护 |
| `unit_price` | DecimalField(14, 6) | 采购单价 |
| `line_amount` | DecimalField(14, 2) | 行金额 |
| `needed_date` | DateField | 需求日期 |
| `line_status` | CharField | open/partial_received/received/closed |

唯一约束：

- `purchase_orders.purchase_order_no`。
- `purchase_order_items.purchase_order + material`。

### 7.4 purchase_receipts / purchase_receipt_items

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `purchase_receipts` | `purchase_receipt_no`、`purchase_order`、`supplier`、`receipt_date`、`status` | 进货单 |
| `purchase_receipt_items` | `purchase_receipt`、`purchase_order_item`、`material`、`received_qty`、`accepted_qty`、`rejected_qty`、`unit_price` | 进货明细 |

`purchase_receipt_items` 字段展开：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `purchase_receipt` | FK(PurchaseReceipt) | 进货单 |
| `purchase_order_item` | FK(PurchaseOrderItem) | 采购单明细 |
| `material` | FK(Material) | 物料 |
| `received_qty` | DecimalField | 到货数量 |
| `accepted_qty` | DecimalField | 合格数量 |
| `rejected_qty` | DecimalField | 不良数量 |
| `unit_price` | DecimalField(14, 6) | 入库单价 |
| `location` | FK(WarehouseLocation) | 入库库位 |
| `batch` | FK(InventoryBatch), nullable | 生成批次 |

唯一约束：

- `purchase_receipts.purchase_receipt_no`。
- `purchase_receipt_items.purchase_receipt + purchase_order_item + material`。

### 7.5 supplier_returns / supplier_return_items

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `supplier_returns` | `supplier_return_no`、`supplier`、`purchase_receipt`、`return_date`、`status`、`return_amount` | 供应商退货单 |
| `supplier_return_items` | `supplier_return`、`purchase_receipt_item`、`material`、`return_qty`、`unit_price`、`batch`、`location` | 供应商退货明细 |

`supplier_return_items` 字段展开：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `supplier_return` | FK(SupplierReturn) | 供应商退货单 |
| `purchase_receipt_item` | FK(PurchaseReceiptItem), nullable | 来源进货明细 |
| `material` | FK(Material) | 退货物料 |
| `return_qty` | DecimalField | 退货数量 |
| `unit_price` | DecimalField(14, 6) | 退货单价 |
| `return_amount` | DecimalField(14, 2) | 退货金额 |
| `batch` | FK(InventoryBatch), nullable | 出库批次 |
| `location` | FK(WarehouseLocation), nullable | 出库库位 |
| `return_reason` | TextField | 退货原因 |

唯一约束：

- `supplier_returns.supplier_return_no`。
- `supplier_return_items.supplier_return + material + purchase_receipt_item`。

## 8. inventory

### 8.1 warehouse_locations

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `location_code` | CharField | 库位编码 |
| `location_name` | CharField | 库位名称 |
| `status` | CharField | active/inactive |

唯一约束：

- `location_code`。

### 8.2 inventory_batches

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `batch_no` | CharField | 批次号 |
| `material` | FK(Material) | 物料 |
| `location` | FK(WarehouseLocation) | 库位 |
| `inventory_type` | CharField | available/defective/pending/sample |
| `received_at` | DateTimeField | 入库时间 |
| `initial_qty` | DecimalField | 初始数量 |
| `remaining_qty` | DecimalField | 剩余数量 |
| `cost_price` | DecimalField(14, 6) | 批次成本 |
| `batch_status` | CharField | in_stock/frozen/voided/used_up |

唯一约束：

- `batch_no`。

索引：

- `material + location + received_at + batch_no`，用于 FIFO。

### 8.3 inventory

`inventory` 是 `inventory_batches` 的汇总缓存。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `material` | FK(Material) | 物料 |
| `location` | FK(WarehouseLocation) | 库位 |
| `inventory_type` | CharField | 库存类型 |
| `qty` | DecimalField | 当前库存 |

唯一约束：

- `material + location + inventory_type`。

### 8.4 inventory_transactions

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `transaction_no` | CharField | 流水号 |
| `transaction_type` | CharField | purchase_in/sales_out/production_issue/production_receipt/sample_out/sample_return_in/customer_return_in/supplier_return_out/sample_to_sales/location_transfer/stock_adjustment |
| `material` | FK(Material) | 物料 |
| `batch` | FK(InventoryBatch) | 批次 |
| `location` | FK(WarehouseLocation) | 库位 |
| `qty_delta` | DecimalField | 变动数量 |
| `source_doc_type` | CharField | 来源单据类型 |
| `source_doc_id` | PositiveBigIntegerField | 来源单据 ID |
| `source_doc_no` | CharField | 来源单号 |

唯一约束：

- `transaction_no`。

索引：

- `material + created_at`。
- `source_doc_type + source_doc_id`。

### 8.5 location_transfers

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `transfer_no` | CharField | 移库单号 |
| `material` | FK(Material) | 物料 |
| `batch` | FK(InventoryBatch) | 批次 |
| `from_location` | FK(WarehouseLocation) | 原库位 |
| `to_location` | FK(WarehouseLocation) | 目标库位 |
| `transfer_qty` | DecimalField | 移库数量 |
| `status` | CharField | draft/confirmed/voided |

唯一约束：

- `transfer_no`。

### 8.6 stock_counts / stock_count_items

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `stock_counts` | `stock_count_no`、`scope_type`、`scope_value`、`snapshot_at`、`status` | 盘点单 |
| `stock_count_items` | `stock_count`、`material`、`batch`、`location`、`book_qty`、`counted_qty`、`difference_qty`、`difference_reason` | 盘点明细 |

唯一约束：

- `stock_counts.stock_count_no`。
- `stock_count_items.stock_count + material + batch + location`。

## 9. production

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `production_orders` | `production_order_no`、`sales_order_item`、`finished_material`、`production_qty`、`received_qty`、`locked_bom`、`status` | 生产指令 |
| `production_material_requisitions` | `requisition_no`、`production_order`、`status` | 生产领料单 |
| `production_material_requisition_items` | `requisition`、`material`、`required_qty`、`issued_qty`、`batch`、`location` | 领料明细 |
| `production_receipts` | `production_receipt_no`、`production_order`、`status` | 生产入库单 |
| `production_receipt_items` | `production_receipt`、`finished_material`、`receipt_qty`、`location`、`batch_no` | 入库明细 |

`production_material_requisition_items` 字段展开：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `requisition` | FK(ProductionMaterialRequisition) | 领料单 |
| `production_order` | FK(ProductionOrder) | 生产指令 |
| `material` | FK(Material) | 子件 |
| `required_qty` | DecimalField | 应领数量 |
| `issued_qty` | DecimalField | 本次领料数量 |
| `batch` | FK(InventoryBatch) | 出库批次 |
| `location` | FK(WarehouseLocation) | 库位 |
| `adjust_reason` | TextField | 调整原因 |

`production_receipt_items` 字段展开：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `production_receipt` | FK(ProductionReceipt) | 生产入库单 |
| `production_order` | FK(ProductionOrder) | 生产指令 |
| `finished_material` | FK(Material) | 成品 |
| `receipt_qty` | DecimalField | 入库数量 |
| `location` | FK(WarehouseLocation) | 入库库位 |
| `batch` | FK(InventoryBatch), nullable | 生成批次 |
| `quality_status` | CharField | qualified/pending/defective |

唯一约束：

- `production_orders.production_order_no`。
- `production_material_requisitions.requisition_no`。
- `production_receipts.production_receipt_no`。

## 10. finance

### 10.1 customer_receipts / customer_receipt_allocations

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `customer_receipts` | `receipt_no`、`customer`、`receipt_date`、`receipt_amount`、`unallocated_amount`、`status` | 客户收款 |
| `customer_receipt_allocations` | `customer_receipt`、`sales_order`、`reconciliation`、`allocated_amount`、`allocation_type` | 收款核销 |

### 10.2 customer_receipt_reversals

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `reversal_no` | CharField | 红冲单号 |
| `source_receipt` | FK(CustomerReceipt) | 原收款单 |
| `reversal_amount` | DecimalField | 红冲金额 |
| `reason` | TextField | 红冲原因 |
| `status` | CharField | draft/pending_approval/confirmed/voided |
| `idempotency_key` | CharField | 幂等键 |

唯一约束：

- `reversal_no`。
- `source_receipt + idempotency_key`。

### 10.3 customer_credit_balances / customer_credit_balance_transactions

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `customer_credit_balances` | `customer`、`source_doc_type`、`source_doc_id`、`balance_amount`、`used_amount`、`remaining_amount`、`status` | 客户待处理余额 |
| `customer_credit_balance_transactions` | `transaction_no`、`credit_balance`、`action_type`、`amount`、`target_doc_type`、`target_doc_id`、`idempotency_key` | 余额使用流水 |

### 10.4 supplier_payments / supplier_payment_allocations

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `supplier_payments` | `payment_no`、`supplier`、`payment_date`、`payment_amount`、`unallocated_amount`、`status` | 供应商付款 |
| `supplier_payment_allocations` | `supplier_payment`、`purchase_receipt`、`reconciliation`、`allocated_amount`、`allocation_type` | 付款核销 |

### 10.5 supplier_payment_reversals

字段同 `customer_receipt_reversals`，来源字段改为 `source_payment`。

### 10.6 supplier_credit_balances / supplier_credit_balance_transactions

字段同客户余额表，客户改为供应商。

## 11. approvals

### 11.1 approvals

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `approval_no` | CharField | 审批单号 |
| `source_content_type` | FK(ContentType) | 来源模型 |
| `source_object_id` | PositiveBigIntegerField | 来源对象 ID |
| `source_doc_type` | CharField | 来源单据类型 |
| `source_no` | CharField | 来源单号 |
| `source_title` | CharField | 摘要标题 |
| `source_summary` | JSONField | 审批摘要快照 |
| `current_approver` | FK(User) | 当前审批人 |
| `status` | CharField | pending/approved/rejected/transferred/withdrawn |

唯一约束：

- `approval_no`。

索引：

- `current_approver + status + created_at`。
- `source_doc_type + source_object_id`。

### 11.2 approval_rules / approval_logs

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `approval_rules` | `doc_type`、`condition_json`、`level_no`、`approver_role`、`approver_user`、`status` | 审批规则 |
| `approval_logs` | `approval`、`action`、`operator`、`comment`、`created_at` | 审批日志 |

## 12. files

### 12.1 attachments

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `attachment_no` | CharField | 附件编号 |
| `source_doc_type` | CharField | 来源单据类型 |
| `source_doc_id` | PositiveBigIntegerField | 来源对象 ID |
| `original_filename` | CharField | 原文件名 |
| `stored_filename` | CharField | 存储文件名 |
| `file_path` | CharField | 内部路径 |
| `file_size` | PositiveBigIntegerField | 文件大小 |
| `mime_type` | CharField | MIME |
| `checksum_sha256` | CharField | 校验值 |
| `status` | CharField | active/deleted |

### 12.2 import_jobs / initialization_jobs / export_logs / print_logs

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `import_jobs` | `job_no`、`template_type`、`template_version`、`source_file`、`status`、`success_count`、`failed_count`、`error_summary` | 日常导入任务 |
| `initialization_jobs` | `job_no`、`template_type`、`source_file`、`status`、`confirmed_by`、`confirmed_at`、`error_summary` | 上线初始化导入任务 |
| `export_logs` | `export_no`、`module`、`filter_json`、`file_path`、`row_count`、`exported_by`、`created_at` | 导出记录 |
| `print_logs` | `print_no`、`template_type`、`source_doc_type`、`source_doc_id`、`printed_by`、`created_at` | 打印记录 |

唯一约束：

- `import_jobs.job_no`。
- `initialization_jobs.job_no`。
- `export_logs.export_no`。
- `print_logs.print_no`。

## 13. system

### 13.1 document_sequences

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `prefix` | CharField | 单号前缀 |
| `sequence_date` | DateField | 日期 |
| `current_value` | PositiveIntegerField | 当前流水 |

唯一约束：

- `prefix + sequence_date`。

### 13.2 pending_events

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_type` | CharField | 事件类型 |
| `idempotency_key` | CharField | 幂等键 |
| `payload` | JSONField | 事件内容 |
| `status` | CharField | pending/running/success/failed/cancelled |
| `retry_count` | PositiveIntegerField | 重试次数 |
| `next_retry_at` | DateTimeField | 下次重试时间 |
| `last_error` | TextField | 最近错误 |

唯一约束：

- `idempotency_key`。

索引：

- `status + event_type + next_retry_at`。

### 13.3 background_jobs / system_settings / backups

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `background_jobs` | `job_type`、`status`、`started_at`、`finished_at`、`result_summary`、`error_message` | 后台任务 |
| `system_settings` | `setting_key`、`setting_value`、`value_type` | 系统配置 |
| `backups` | `backup_no`、`backup_type`、`file_path`、`file_size`、`checksum_sha256`、`status` | 备份记录 |

### 13.4 saved_filters

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user` | FK(User) | 用户 |
| `module` | CharField | 模块 |
| `filter_name` | CharField | 筛选名称 |
| `filter_json` | JSONField | 筛选条件 |
| `is_default` | BooleanField | 是否默认 |

唯一约束：

- `user + module + filter_name`。

### 13.5 audit_logs

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `log_no` | CharField | 日志编号 |
| `operator` | FK(User) | 操作人 |
| `action` | CharField | 操作动作 |
| `source_doc_type` | CharField | 对象类型 |
| `source_doc_id` | PositiveBigIntegerField | 对象 ID |
| `source_doc_no` | CharField | 对象单号 |
| `ip_address` | GenericIPAddressField | IP |
| `user_agent` | TextField | 浏览器 |
| `before_snapshot` | JSONField | 操作前快照，可选 |
| `after_snapshot` | JSONField | 操作后快照，可选 |
| `created_at` | DateTimeField | 操作时间 |

索引：

- `operator + created_at`。
- `source_doc_type + source_doc_id`。
- `action + created_at`。

## 14. 建表前待补充

正式写 migration 前，需要把本草案继续细化为以下内容：

- 每个字段的 `max_length`。
- 每个字段的 `null`、`blank`、`default`。
- 每个状态字段的 Django `TextChoices`。
- 每个外键的 `on_delete` 策略。
- 每个表的 `db_table`、`indexes`、`constraints`。
- 每个金额和数量字段的业务校验。
- 需要加密字段的加密/哈希查询方案。
