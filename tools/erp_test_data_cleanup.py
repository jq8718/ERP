"""Standalone Windows UI for removing marked ERP test data.

This tool intentionally talks to SQLite directly. It is not a Django view and
does not need the ERP web server to be running. Keep this file next to the ERP
installation or use the generated launcher which supplies the default paths.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, X, Y, W, StringVar, Tk, filedialog, messagebox
from tkinter import ttk


MARKERS = ("UATLED", "SMK-")
CONFIRMATION = "删除测试数据"
DEFAULT_APP_DIR = Path(os.environ.get("ERP_APP_DIR", r"D:\ERP\app"))
DEFAULT_DB = DEFAULT_APP_DIR / "db.sqlite3"
DEFAULT_BACKUP_DIR = DEFAULT_APP_DIR / "backups"
BUSINESS_TABLES = {
    "approvals",
    "approval_rules",
    "approval_logs",
    "boms",
    "bom_items",
    "attachments",
    "attachment_access_logs",
    "import_jobs",
    "initialization_jobs",
    "export_logs",
    "print_logs",
    "reconciliations",
    "reconciliation_items",
    "customer_invoices",
    "customer_invoice_items",
    "customer_receipts",
    "customer_receipt_allocations",
    "customer_receipt_reversals",
    "customer_credit_balances",
    "customer_credit_balance_transactions",
    "opening_receivables",
    "opening_payables",
    "supplier_payments",
    "supplier_payment_allocations",
    "supplier_payment_reversals",
    "supplier_credit_balances",
    "supplier_credit_balance_transactions",
    "expense_records",
    "inventory_batches",
    "inventory",
    "inventory_transactions",
    "warehouse_locations",
    "location_transfers",
    "stock_counts",
    "stock_count_items",
    "materials",
    "material_unit_conversions",
    "customers",
    "customer_products",
    "customer_addresses",
    "suppliers",
    "material_supplier_prices",
    "system_messages",
    "production_orders",
    "production_material_requisitions",
    "production_material_requisition_items",
    "production_receipts",
    "production_receipt_items",
    "purchase_requests",
    "purchase_request_items",
    "purchase_orders",
    "purchase_order_items",
    "purchase_receipts",
    "purchase_receipt_items",
    "supplier_returns",
    "supplier_return_items",
    "sales_orders",
    "sales_order_items",
    "sales_order_change_logs",
    "sales_order_change_logs",
    "customer_returns",
    "customer_return_items",
    "sample_loans",
    "sample_loan_items",
    "sample_loan_returns",
    "sample_loan_return_items",
    "sales_shipments",
    "sales_shipment_items",
    "shortage_alerts",
}
TABLE_LABELS = {
    "materials": "物料",
    "customers": "客户",
    "suppliers": "供应商",
    "warehouse_locations": "仓位",
    "inventory_batches": "库存批次",
    "inventory": "库存汇总",
    "inventory_transactions": "库存流水",
    "sales_orders": "销售订单",
    "sales_order_items": "销售订单明细",
    "sales_shipments": "销售出库",
    "sales_shipment_items": "销售出库明细",
    "customer_returns": "客户退货",
    "customer_return_items": "客户退货明细",
    "sample_loans": "借样单",
    "sample_loan_items": "借样明细",
    "sample_loan_returns": "借样归还",
    "sample_loan_return_items": "借样归还明细",
    "purchase_requests": "采购需求",
    "purchase_request_items": "采购需求明细",
    "purchase_orders": "采购单",
    "purchase_order_items": "采购单明细",
    "purchase_receipts": "进货单",
    "purchase_receipt_items": "进货单明细",
    "supplier_returns": "供应商退货",
    "supplier_return_items": "供应商退货明细",
    "production_orders": "生产单",
    "production_material_requisitions": "生产领料",
    "production_material_requisition_items": "生产领料明细",
    "production_receipts": "生产入库",
    "production_receipt_items": "生产入库明细",
    "boms": "BOM",
    "bom_items": "BOM明细",
    "reconciliations": "对账单",
    "reconciliation_items": "对账明细",
    "warehouse_locations": "仓库库位",
}


def quote(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise ValueError(f"非法数据库标识符: {identifier}")
    return f'"{identifier}"'


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=8)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows} & BUSINESS_TABLES


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    """Check a table outside the business-table allowlist (for example users)."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def table_info(connection: sqlite3.Connection, table: str):
    return list(connection.execute(f"PRAGMA table_info({quote(table)})"))


def primary_key_column(connection: sqlite3.Connection, table: str) -> str:
    columns = [row for row in table_info(connection, table) if row[5]]
    if len(columns) != 1:
        raise RuntimeError(f"表 {table} 不是单一主键，已停止清理")
    return columns[0][1]


def foreign_keys(connection: sqlite3.Connection, table: str):
    # PRAGMA foreign_key_list returns: id, seq, table, from, to, on_update...
    return list(connection.execute(f"PRAGMA foreign_key_list({quote(table)})"))


def marked_ids(connection: sqlite3.Connection, table: str) -> set[int]:
    columns = []
    for row in table_info(connection, table):
        field_type = (row[2] or "").upper()
        if field_type.startswith(("TEXT", "VARCHAR", "CHAR")):
            columns.append(row[1])
    if not columns:
        return set()
    primary_key = primary_key_column(connection, table)
    clauses = []
    parameters = []
    for column in columns:
        for marker in MARKERS:
            clauses.append(f"LOWER(COALESCE({quote(column)}, '')) LIKE LOWER(?)")
            parameters.append(f"%{marker}%")
    query = f"SELECT {quote(primary_key)} FROM {quote(table)} WHERE " + " OR ".join(clauses)
    return {int(row[0]) for row in connection.execute(query, parameters)}


def build_impact(connection: sqlite3.Connection):
    tables = table_names(connection)
    selected = {table: set() for table in tables}
    direct = {}
    for table in tables:
        ids = marked_ids(connection, table)
        if ids:
            selected[table] = ids
            direct[table] = len(ids)

    changed = True
    while changed:
        changed = False
        for child in tables:
            child_pk = primary_key_column(connection, child)
            for relation in foreign_keys(connection, child):
                parent = relation[2]
                source_column = relation[3]
                target_column = relation[4] or primary_key_column(connection, parent) if parent in tables else relation[4]
                if parent not in selected or not selected[parent]:
                    continue
                child_ids = {
                    int(row[0])
                    for row in connection.execute(
                        f"SELECT {quote(child_pk)} FROM {quote(child)} WHERE {quote(source_column)} IN ({','.join('?' for _ in selected[parent])})",
                        tuple(selected[parent]),
                    )
                }
                additions = child_ids - selected[child]
                if additions:
                    selected[child].update(additions)
                    changed = True

    counts = {table: len(ids) for table, ids in selected.items() if ids}
    return {
        "selected": selected,
        "by_table": dict(sorted(counts.items())),
        "direct_by_table": dict(sorted(direct.items())),
        "total": sum(counts.values()),
        "direct_total": sum(direct.values()),
        "associated_total": sum(counts.values()) - sum(direct.values()),
    }


def deletion_order(connection: sqlite3.Connection, selected: dict[str, set[int]]) -> list[str]:
    tables = {table for table, ids in selected.items() if ids}
    children = defaultdict(set)
    for child in tables:
        for relation in foreign_keys(connection, child):
            parent = relation[2]
            if parent in tables and parent != child:
                children[parent].add(child)
    ordered = []
    visiting = set()
    visited = set()

    def visit(table):
        if table in visited or table in visiting:
            return
        visiting.add(table)
        for child in sorted(children[table]):
            visit(child)
        visiting.remove(table)
        visited.add(table)
        ordered.append(table)

    for table in sorted(tables):
        visit(table)
    return ordered


def delete_impact(connection: sqlite3.Connection, impact: dict) -> None:
    connection.execute("PRAGMA defer_foreign_keys = ON")
    for table in deletion_order(connection, impact["selected"]):
        primary_key = primary_key_column(connection, table)
        ids = list(impact["selected"][table])
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM {quote(table)} WHERE {quote(primary_key)} IN ({placeholders})",
                chunk,
            )
    violations = list(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise RuntimeError(f"删除后发现 {len(violations)} 条外键异常，已回滚")


def verify_django_password(encoded: str, password: str) -> bool:
    """Verify the default Django pbkdf2_sha256 format without loading Django."""
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        )
        # Django stores the complete Base64 digest, including padding.
        encoded_digest = base64.b64encode(calculated).decode()
        return hmac.compare_digest(encoded_digest, digest)
    except (ValueError, TypeError):
        return False


def admin_usernames(connection: sqlite3.Connection) -> list[str]:
    if not has_table(connection, "users"):
        return []
    columns = {row[1] for row in table_info(connection, "users")}
    if not {"username", "is_superuser"}.issubset(columns):
        return []
    conditions = ["is_superuser = 1"]
    if "is_deleted" in columns:
        conditions.append("COALESCE(is_deleted, 0) = 0")
    if "is_active" in columns:
        conditions.append("COALESCE(is_active, 1) = 1")
    if "status" in columns:
        conditions.append("COALESCE(status, 'active') = 'active'")
    rows = connection.execute(
        f'SELECT username FROM "users" WHERE {" AND ".join(conditions)} ORDER BY username'
    )
    return [row[0] for row in rows]


def default_admin_username(connection: sqlite3.Connection) -> str:
    """Choose an existing ERP administrator; the tool has no separate users."""
    usernames = admin_usernames(connection)
    if "admin" in usernames:
        return "admin"
    return usernames[0] if usernames else ""


def check_admin(connection: sqlite3.Connection, username: str, password: str) -> bool:
    if not has_table(connection, "users"):
        return False
    columns = {row[1] for row in table_info(connection, "users")}
    if not {"username", "password", "is_superuser"}.issubset(columns):
        return False
    conditions = ["username = ?", "is_superuser = 1"]
    if "is_deleted" in columns:
        conditions.append("COALESCE(is_deleted, 0) = 0")
    if "is_active" in columns:
        conditions.append("COALESCE(is_active, 1) = 1")
    if "status" in columns:
        conditions.append("COALESCE(status, 'active') = 'active'")
    row = connection.execute(
        f'SELECT password FROM "users" WHERE {" AND ".join(conditions)}',
        (username.strip(),),
    ).fetchone()
    return bool(row and verify_django_password(row[0], password))


def create_backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"before-test-data-cleanup_{stamp}.sqlite3"
    source = connect(db_path)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return target


def write_operation_log(app_dir: Path, payload: dict) -> Path:
    log_path = app_dir / "logs" / "standalone-test-data-cleanup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return log_path


class CleanupWindow:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("ERP 测试数据清理工具")
        self.root.minsize(900, 620)
        self.impact = None
        self.connection = None
        self._build_ui()
        self._set_initial_window_size()
        self.refresh_accounts()

    def _set_initial_window_size(self):
        """Use a usable initial size while keeping the window inside the screen."""
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1180, max(960, screen_width - 100))
        # Leave room for the title bar and taskbar so the fixed action bar is
        # not placed below the visible client area on short screens.
        height = min(820, max(640, screen_height - 180))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.resizable(True, True)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(outer, text="ERP 测试数据清理工具", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor=W)
        ttk.Label(
            outer,
            text="独立工具：不通过 ERP 网页菜单运行。只处理包含 UATLED 或 SMK- 标识的业务数据及其关联明细。",
            foreground="#7a2e00",
        ).pack(anchor=W, pady=(4, 12))

        path_frame = ttk.LabelFrame(outer, text="数据库")
        path_frame.pack(fill=X, pady=(0, 10))
        self.db_var = StringVar(value=str(DEFAULT_DB))
        self.backup_var = StringVar(value=str(DEFAULT_BACKUP_DIR))
        ttk.Label(path_frame, text="ERP 数据库").grid(row=0, column=0, padx=8, pady=7, sticky=W)
        ttk.Entry(path_frame, textvariable=self.db_var).grid(row=0, column=1, padx=4, pady=7, sticky="ew")
        ttk.Button(path_frame, text="选择", command=self.choose_db).grid(row=0, column=2, padx=8, pady=7)
        ttk.Label(path_frame, text="备份目录").grid(row=1, column=0, padx=8, pady=7, sticky=W)
        ttk.Entry(path_frame, textvariable=self.backup_var).grid(row=1, column=1, padx=4, pady=7, sticky="ew")
        ttk.Button(path_frame, text="选择", command=self.choose_backup).grid(row=1, column=2, padx=8, pady=7)
        path_frame.columnconfigure(1, weight=1)

        action_frame = ttk.Frame(outer)
        action_frame.pack(fill=X, pady=(0, 10))
        ttk.Button(action_frame, text="刷新预览", command=self.refresh_preview).pack(side=LEFT)
        ttk.Label(action_frame, text="  标识：UATLED、SMK-", foreground="#555").pack(side=LEFT, padx=10)
        self.summary_var = StringVar(value="尚未读取数据库")
        ttk.Label(action_frame, textvariable=self.summary_var).pack(side=RIGHT)

        # Keep the table flexible, but reserve the credential and action rows.
        # Mixing an expanding packer with later fixed controls lets the table
        # push those controls below the visible window on short displays.
        content = ttk.Frame(outer)
        content.pack(fill=BOTH, expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        table_frame = ttk.LabelFrame(content, text="清理影响范围")
        table_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        columns = ("table", "direct", "associated", "total")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=15)
        headings = {"table": "业务表", "direct": "直接标记", "associated": "关联明细", "total": "合计"}
        widths = {"table": 300, "direct": 120, "associated": 120, "total": 120}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="center")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        credential_frame = ttk.LabelFrame(content, text="执行确认")
        credential_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        credential_frame.columnconfigure(1, weight=1)
        ttk.Label(credential_frame, text="ERP超级管理员账号").grid(row=0, column=0, padx=8, pady=7, sticky=W)
        self.user_var = StringVar()
        self.user_combo = ttk.Combobox(credential_frame, textvariable=self.user_var, state="readonly", width=28)
        self.user_combo.grid(row=0, column=1, padx=8, pady=7, sticky="ew")
        ttk.Label(credential_frame, text="ERP当前密码").grid(row=1, column=0, padx=8, pady=7, sticky=W)
        self.password_var = StringVar()
        ttk.Entry(credential_frame, textvariable=self.password_var, show="*", width=24).grid(row=1, column=1, padx=8, pady=7, sticky="ew")
        ttk.Label(credential_frame, text=f"确认词：{CONFIRMATION}").grid(row=2, column=0, padx=8, pady=7, sticky=W)
        self.confirm_var = StringVar()
        ttk.Entry(credential_frame, textvariable=self.confirm_var, width=28).grid(row=2, column=1, padx=8, pady=7, sticky="ew")
        # Reserve a dedicated footer. It is outside the expandable table, so
        # the destructive action remains visible on small or scaled displays.
        self.status_var = StringVar(value="请先刷新预览")
        footer = ttk.Frame(content)
        footer.grid(row=2, column=0, sticky="ew", pady=(0, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Separator(footer, orient="horizontal").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(footer, textvariable=self.status_var, foreground="#555", width=70).grid(
            row=1, column=0, padx=(0, 12), sticky=W
        )
        self.execute_button = ttk.Button(
            footer, text="备份并删除测试数据", command=self.start_cleanup, width=24
        )
        self.execute_button.grid(row=1, column=1, padx=0, pady=(0, 2), sticky="e")

    def choose_db(self):
        selected = filedialog.askopenfilename(title="选择 ERP 数据库", filetypes=[("SQLite 数据库", "*.sqlite3;*.db"), ("所有文件", "*.*")])
        if selected:
            self.db_var.set(selected)
            self.refresh_accounts()

    def choose_backup(self):
        selected = filedialog.askdirectory(title="选择备份目录")
        if selected:
            self.backup_var.set(selected)

    def get_connection(self):
        db_path = Path(self.db_var.get().strip())
        if not db_path.is_file():
            raise FileNotFoundError(f"数据库不存在：{db_path}")
        if self.connection:
            self.connection.close()
        self.connection = connect(db_path)
        return self.connection

    def refresh_accounts(self):
        try:
            connection = self.get_connection()
            usernames = admin_usernames(connection)
            self.user_combo["values"] = usernames
            self.user_var.set(default_admin_username(connection))
            self.refresh_preview()
        except Exception as exc:
            self.status_var.set(str(exc))

    def refresh_preview(self):
        try:
            connection = self.get_connection()
            self.impact = build_impact(connection)
            for item in self.tree.get_children():
                self.tree.delete(item)
            direct = self.impact["direct_by_table"]
            for table, total in self.impact["by_table"].items():
                direct_count = direct.get(table, 0)
                self.tree.insert("", END, values=(TABLE_LABELS.get(table, table), direct_count, total - direct_count, total))
            self.summary_var.set(
                f"直接标记 {self.impact['direct_total']} 条，关联明细 {self.impact['associated_total']} 条，共 {self.impact['total']} 条"
            )
            self.status_var.set("预览完成。执行前会自动备份数据库。")
        except Exception as exc:
            self.status_var.set(f"预览失败：{exc}")

    def start_cleanup(self):
        if not self.impact or not self.impact["total"]:
            messagebox.showinfo("无需清理", "当前没有找到带 UATLED 或 SMK- 标识的数据。")
            return
        if self.confirm_var.get().strip() != CONFIRMATION:
            messagebox.showerror("确认词不正确", f"请输入：{CONFIRMATION}")
            return
        if not self.password_var.get():
            messagebox.showerror("缺少密码", "请输入该 ERP 超级管理员平时登录 ERP 使用的当前密码。")
            return
        prompt = (
            f"将清理 {self.impact['total']} 条记录（直接标记 {self.impact['direct_total']} 条，"
            f"关联明细 {self.impact['associated_total']} 条）。\n\n"
            "系统会先生成数据库备份。确定继续吗？"
        )
        if not messagebox.askyesno("高风险操作", prompt, icon="warning"):
            return
        self.execute_button.configure(state="disabled")
        self.status_var.set("正在备份并清理，请不要关闭窗口……")
        threading.Thread(target=self._cleanup_worker, daemon=True).start()

    def _cleanup_worker(self):
        try:
            db_path = Path(self.db_var.get().strip())
            backup_dir = Path(self.backup_var.get().strip())
            connection = connect(db_path)
            if not check_admin(connection, self.user_var.get(), self.password_var.get()):
                raise PermissionError("超级管理员账号或当前密码不正确")
            impact = build_impact(connection)
            if not impact["total"]:
                raise RuntimeError("执行前重新检查时已没有可清理的测试数据")
            backup_path = create_backup(db_path, backup_dir)
            connection.execute("BEGIN IMMEDIATE")
            try:
                delete_impact(connection, impact)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            log_path = write_operation_log(
                db_path.parent,
                {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "database": str(db_path),
                    "backup": str(backup_path),
                    "operator": self.user_var.get(),
                    "impact": {key: value for key, value in impact.items() if key != "selected"},
                },
            )
            self.root.after(0, lambda: self._cleanup_done(True, f"清理完成。备份：{backup_path}\n日志：{log_path}"))
        except Exception as exc:
            detail = f"清理失败，数据库已回滚：{exc}"
            self.root.after(0, lambda: self._cleanup_done(False, detail))

    def _cleanup_done(self, success: bool, message: str):
        self.execute_button.configure(state="normal")
        if success:
            self.status_var.set("清理完成，备份与日志已生成")
            self.password_var.set("")
            self.confirm_var.set("")
            self.refresh_preview()
            messagebox.showinfo("完成", message)
        else:
            self.status_var.set("清理失败，数据库已回滚")
            messagebox.showerror("清理失败", message)


def main():
    if sys.platform == "win32":
        try:
            # Let Tk calculate widget sizes in physical pixels on scaled displays.
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
    root = Tk()
    try:
        style = ttk.Style(root)
        style.theme_use("vista")
        ui_font = ("Microsoft YaHei UI", 10)
        style.configure(".", font=ui_font)
        style.configure("TButton", padding=(12, 7))
        style.configure("TEntry", padding=(6, 5))
        style.configure("TCombobox", padding=(6, 5))
        style.configure("Treeview", font=ui_font, rowheight=28)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
    except Exception:
        pass
    CleanupWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
