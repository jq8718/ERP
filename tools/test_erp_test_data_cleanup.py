import base64
import hashlib
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("erp_test_data_cleanup.py")
SPEC = importlib.util.spec_from_file_location("erp_test_data_cleanup", MODULE_PATH)
CLEANUP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CLEANUP)


def django_password(password: str, salt: str = "cleanup-test-salt") -> str:
    iterations = 720000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    encoded = base64.b64encode(digest).decode()
    return f"pbkdf2_sha256${iterations}${salt}${encoded}"


class CleanupToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username varchar(150) NOT NULL,
                password varchar(128) NOT NULL,
                is_superuser bool NOT NULL,
                is_deleted bool NOT NULL DEFAULT 0
            );
            CREATE TABLE materials (
                id INTEGER PRIMARY KEY,
                material_code varchar(80) NOT NULL,
                material_name text NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE customer_products (
                id INTEGER PRIMARY KEY,
                finished_material_id INTEGER NOT NULL,
                customer_product_no varchar(80) NOT NULL,
                FOREIGN KEY (finished_material_id) REFERENCES materials(id)
            );
            """
        )
        connection.execute(
            "INSERT INTO users(username, password, is_superuser) VALUES (?, ?, 1)",
            ("admin", django_password("Correct!123")),
        )
        connection.execute(
            "INSERT INTO materials(material_code, material_name, quantity) VALUES (?, ?, ?)",
            ("UATLED-MATERIAL", "正式名称", 1),
        )
        connection.execute(
            "INSERT INTO customer_products(finished_material_id, customer_product_no) VALUES (1, ?)",
            ("REAL-CUSTOMER-MODEL",),
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_uses_existing_superuser_and_verifies_erp_password(self):
        connection = CLEANUP.connect(self.db_path)
        self.assertEqual(CLEANUP.admin_usernames(connection), ["admin"])
        self.assertEqual(CLEANUP.default_admin_username(connection), "admin")
        self.assertTrue(CLEANUP.check_admin(connection, "admin", "Correct!123"))
        self.assertFalse(CLEANUP.check_admin(connection, "admin", "wrong"))
        connection.close()

    def test_varchar_and_text_markers_are_detected_and_dependents_are_included(self):
        connection = CLEANUP.connect(self.db_path)
        impact = CLEANUP.build_impact(connection)
        self.assertEqual(impact["direct_total"], 1)
        self.assertEqual(impact["associated_total"], 1)
        self.assertEqual(impact["total"], 2)
        self.assertEqual(impact["by_table"]["materials"], 1)
        self.assertEqual(impact["by_table"]["customer_products"], 1)
        connection.close()

    def test_delete_can_be_rolled_back(self):
        connection = CLEANUP.connect(self.db_path)
        impact = CLEANUP.build_impact(connection)
        connection.execute("BEGIN")
        CLEANUP.delete_impact(connection, impact)
        connection.rollback()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM customer_products").fetchone()[0], 1)
        connection.close()


if __name__ == "__main__":
    unittest.main()
