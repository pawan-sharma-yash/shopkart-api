import os
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "shopkart-test-secret-key-at-least-32-bytes-long")

from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token
from app.api.v1.endpoints.orders import _new_public_order_id
from app.db.base import Base
from app.db.session import ensure_order_address_reference, ensure_order_public_id, get_db
from app.main import app
from app.models.address import Address
from app.models.auth import User
from app.models.cart import CartItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.services.order_ids import generate_public_order_id


class OrderApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.SessionLocal = sessionmaker(
            bind=cls.engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        app.dependency_overrides[get_db] = self.override_get_db
        self.user_id = self.create_user("buyer@example.com")
        self.token = create_access_token(self.user_id)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.product_id = self.create_product()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)

    def override_get_db(self):
        db = self.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def create_user(
        self,
        email: str,
        *,
        address: str | None = "123 Market Street",
        pin: str | None = "123456",
    ) -> int:
        with self.SessionLocal() as db:
            user = User(email=email, address=address, pin=pin)
            db.add(user)
            db.commit()
            return user.id

    def create_product(self) -> int:
        with self.SessionLocal() as db:
            product = Product(
                id=999999,
                name="Test Product",
                category="Testing",
                reviews=0,
                offers=[],
                variants=[
                    {
                        "variantid": "variant-red",
                        "price": "12.50",
                        "stock": 5,
                        "color": "red",
                    }
                ],
            )
            db.add(product)
            db.commit()
            return product.id

    def add_cart_item(self, *, variant_id: str = "variant-red", quantity: int = 2):
        with self.SessionLocal() as db:
            db.add(
                CartItem(
                    user_id=self.user_id,
                    product_id=self.product_id,
                    variant_id=variant_id,
                    quantity=quantity,
                )
            )
            db.commit()

    def add_address(
        self,
        user_id: int | None = None,
        *,
        address: str = "123 Market Street",
        pin: str | None = "123456",
    ) -> int:
        with self.SessionLocal() as db:
            saved_address = Address(
                user_id=user_id or self.user_id,
                address=address,
                pin=pin,
                label="Home",
            )
            db.add(saved_address)
            db.commit()
            return saved_address.id

    def add_order(self, user_id: int, created_at: datetime, order_number: int = 1):
        with self.SessionLocal() as db:
            order = Order(
                public_order_id=generate_public_order_id(),
                user_id=user_id,
                status="placed",
                total_amount=Decimal("12.50"),
                shipping_address="123 Market Street",
                shipping_pin="123456",
                shipping_address_id=None,
                created_at=created_at,
                items=[
                    OrderItem(
                        product_id=self.product_id,
                        product_name=f"Test Product {order_number}",
                        variant_id="variant-red",
                        variant_snapshot={"variantid": "variant-red", "color": "red"},
                        unit_price=Decimal("12.50"),
                        quantity=1,
                        line_total=Decimal("12.50"),
                    )
                ],
            )
            db.add(order)
            db.commit()
            return order.id

    def test_history_requires_valid_access_token(self):
        self.assertEqual(self.client.get("/orders").status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            self.client.get(
                "/orders",
                headers={"Authorization": "Bearer invalid"},
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_place_order_snapshots_items_decrements_stock_and_clears_cart(self):
        self.add_cart_item(quantity=2)
        address_id = self.add_address()

        response = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.text)
        data = response.json()
        self.assertEqual(data["status"], "placed")
        self.assertEqual(len(data["public_order_id"]), 10)
        self.assertRegex(data["public_order_id"], r"^[A-Z0-9]{10}$")
        self.assertEqual(data["total_amount"], "25.00")
        self.assertEqual(data["shipping_address"], "123 Market Street")
        self.assertEqual(data["shipping_pin"], "123456")
        self.assertEqual(data["shipping_address_id"], address_id)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["product_name"], "Test Product")
        self.assertEqual(data["items"][0]["variant"]["color"], "red")
        self.assertEqual(data["items"][0]["quantity"], 2)
        self.assertEqual(data["items"][0]["line_total"], "25.00")

        with self.SessionLocal() as db:
            self.assertEqual(db.scalar(select(CartItem.id)), None)
            product = db.get(Product, self.product_id)
            self.assertEqual(product.variants[0]["stock"], 3)
            order = db.get(Order, data["id"])
            self.assertEqual(order.user_id, self.user_id)
            self.assertEqual(order.id, data["id"])
            self.assertEqual(order.public_order_id, data["public_order_id"])
            self.assertEqual(order.shipping_address_id, address_id)

    def test_checkout_rejects_empty_cart_and_missing_delivery_address(self):
        address_id = self.add_address()
        empty_cart = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )
        self.assertEqual(empty_cart.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(empty_cart.json()["detail"], "Cannot place an order with an empty cart")

        address_id = self.add_address(pin=None)
        self.add_cart_item()
        response = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("six-digit PIN", response.json()["detail"])

    def test_checkout_rejects_invalid_delivery_pin_without_mutating_cart(self):
        address_id = self.add_address(pin="ABCDEF")
        self.add_cart_item()

        response = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        with self.SessionLocal() as db:
            self.assertEqual(db.scalar(select(Order.id)), None)
            self.assertIsNotNone(db.scalar(select(CartItem.id)))

    def test_checkout_rejects_address_not_owned_by_current_user(self):
        other_user_id = self.create_user("other-address-owner@example.com")
        address_id = self.add_address(other_user_id)
        self.add_cart_item()

        response = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        with self.SessionLocal() as db:
            self.assertEqual(db.scalar(select(Order.id)), None)
            self.assertIsNotNone(db.scalar(select(CartItem.id)))

    def test_deleting_address_preserves_order_snapshot_and_clears_reference(self):
        address_id = self.add_address()
        self.add_cart_item()
        placed = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )
        self.assertEqual(placed.status_code, status.HTTP_201_CREATED, placed.text)

        deleted = self.client.delete(f"/addresses/{address_id}", headers=self.headers)
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)

        history = self.client.get("/orders", headers=self.headers)
        order = history.json()["orders"][0]
        self.assertIsNone(order["shipping_address_id"])
        self.assertEqual(order["shipping_address"], "123 Market Street")
        self.assertEqual(order["shipping_pin"], "123456")

    def test_checkout_rejects_unavailable_variant_and_quantity(self):
        self.add_cart_item(variant_id="missing-variant")
        address_id = self.add_address()
        missing_variant = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )
        self.assertEqual(missing_variant.status_code, status.HTTP_409_CONFLICT)

        with self.SessionLocal() as db:
            db.query(CartItem).filter(CartItem.user_id == self.user_id).delete()
            db.commit()
        self.add_cart_item(quantity=6)
        unavailable_quantity = self.client.post(
            "/orders", headers=self.headers, json={"address_id": address_id}
        )
        self.assertEqual(unavailable_quantity.status_code, status.HTTP_409_CONFLICT)

    def test_history_is_user_scoped_and_paginated(self):
        now = datetime.now(timezone.utc)
        self.add_order(self.user_id, now - timedelta(minutes=2), 1)
        self.add_order(self.user_id, now - timedelta(minutes=1), 2)
        other_user_id = self.create_user("other@example.com")
        self.add_order(other_user_id, now, 3)

        first_page = self.client.get(
            "/orders?page=1&pageSize=1",
            headers=self.headers,
        )
        self.assertEqual(first_page.status_code, status.HTTP_200_OK, first_page.text)
        first_data = first_page.json()
        self.assertEqual(first_data["count"], 2)
        self.assertEqual(first_data["page"], 1)
        self.assertEqual(first_data["pageSize"], 1)
        self.assertEqual(first_data["totalPages"], 2)
        self.assertEqual(first_data["orders"][0]["items"][0]["product_name"], "Test Product 2")
        self.assertRegex(first_data["orders"][0]["public_order_id"], r"^[A-Z0-9]{10}$")
        self.assertIsNone(first_data["orders"][0]["shipping_address_id"])

        second_page = self.client.get(
            "/orders?page=2&pageSize=1",
            headers=self.headers,
        )
        self.assertEqual(second_page.json()["orders"][0]["items"][0]["product_name"], "Test Product 1")

    def test_history_duration_filters_use_rolling_calendar_windows(self):
        now = datetime.now(timezone.utc)
        self.add_order(self.user_id, now - timedelta(days=45), 1)
        self.add_order(self.user_id, now - timedelta(days=120), 2)
        self.add_order(self.user_id, now - timedelta(days=240), 3)
        self.add_order(self.user_id, now - timedelta(days=430), 4)

        expected_counts = {
            2: 1,
            3: 1,
            6: 2,
            12: 3,
        }
        for duration, expected_count in expected_counts.items():
            with self.subTest(duration=duration):
                response = self.client.get(
                    "/orders",
                    params={"duration": duration},
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK, response.text)
                self.assertEqual(response.json()["count"], expected_count)
                self.assertEqual(response.json()["totalPages"], 1)

    def test_history_year_filter_and_mutually_exclusive_filters(self):
        self.add_order(
            self.user_id,
            datetime(2025, 1, 15, tzinfo=timezone.utc),
            1,
        )
        self.add_order(
            self.user_id,
            datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc),
            2,
        )
        self.add_order(
            self.user_id,
            datetime(2024, 12, 31, tzinfo=timezone.utc),
            3,
        )

        response = self.client.get(
            "/orders?year=2025",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.text)
        self.assertEqual(response.json()["count"], 2)

        conflict = self.client.get(
            "/orders?year=2025&duration=6",
            headers=self.headers,
        )
        self.assertEqual(conflict.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)

    def test_history_rejects_invalid_pagination_and_duration(self):
        invalid_page = self.client.get(
            "/orders?page=0",
            headers=self.headers,
        )
        invalid_page_size = self.client.get(
            "/orders?pageSize=101",
            headers=self.headers,
        )
        invalid_duration_below_minimum = self.client.get(
            "/orders?duration=0",
            headers=self.headers,
        )
        invalid_duration_above_maximum = self.client.get(
            "/orders?duration=37",
            headers=self.headers,
        )
        self.assertEqual(invalid_page.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
        self.assertEqual(invalid_page_size.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
        self.assertEqual(invalid_duration_below_minimum.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
        self.assertEqual(invalid_duration_above_maximum.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)

    def test_order_address_migration_preserves_existing_orders(self):
        legacy_engine = create_engine("sqlite://")
        try:
            with legacy_engine.begin() as connection:
                connection.execute(text("CREATE TABLE addresses (id INTEGER PRIMARY KEY)"))
                connection.execute(text("""
                    CREATE TABLE orders (
                        id INTEGER PRIMARY KEY,
                        shipping_address VARCHAR(500),
                        shipping_pin VARCHAR(12)
                    )
                """))
                connection.execute(text(
                    "INSERT INTO orders (id, shipping_address, shipping_pin) "
                    "VALUES (1, '123 Market Street', '123456')"
                ))

            ensure_order_address_reference(legacy_engine)

            inspector = inspect(legacy_engine)
            self.assertIn(
                "shipping_address_id",
                {column["name"] for column in inspector.get_columns("orders")},
            )
            self.assertTrue(any(
                foreign_key.get("constrained_columns") == ["shipping_address_id"]
                and foreign_key.get("referred_table") == "addresses"
                for foreign_key in inspector.get_foreign_keys("orders")
            ))
            with legacy_engine.connect() as connection:
                migrated = connection.execute(text(
                    "SELECT shipping_address_id, shipping_address, shipping_pin "
                    "FROM orders WHERE id = 1"
                )).one()
                self.assertIsNone(migrated.shipping_address_id)
                self.assertEqual(migrated.shipping_address, "123 Market Street")
                self.assertEqual(migrated.shipping_pin, "123456")
        finally:
            legacy_engine.dispose()

    def test_order_public_id_migration_backfills_and_is_idempotent(self):
        legacy_engine = create_engine("sqlite://")
        try:
            with legacy_engine.begin() as connection:
                connection.execute(text("""
                    CREATE TABLE orders (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        status VARCHAR(30) NOT NULL,
                        total_amount NUMERIC(12, 2) NOT NULL
                    )
                """))
                connection.execute(text(
                    "INSERT INTO orders (id, user_id, status, total_amount) "
                    "VALUES (17, 1, 'placed', 12.50)"
                ))

            ensure_order_public_id(legacy_engine)
            ensure_order_public_id(legacy_engine)

            with legacy_engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT id, public_order_id FROM orders WHERE id = 17"
                )).one()
                self.assertEqual(row.id, 17)
                self.assertRegex(row.public_order_id, r"^[A-Z0-9]{10}$")
            self.assertTrue(any(
                index["name"] == "uq_orders_public_order_id" and index["unique"]
                for index in inspect(legacy_engine).get_indexes("orders")
            ))
        finally:
            legacy_engine.dispose()

    def test_public_order_id_generation_retries_existing_value(self):
        self.add_order(self.user_id, datetime.now(timezone.utc))
        with self.SessionLocal() as db:
            existing = db.scalar(select(Order.public_order_id))
            with patch(
                "app.api.v1.endpoints.orders.generate_public_order_id",
                side_effect=[existing, "A1B2C3D4E5"],
            ):
                self.assertEqual(_new_public_order_id(db), "A1B2C3D4E5")


if __name__ == "__main__":
    unittest.main()
