import os

IN_MEMORY_DB = "sqlite:///:memory:"

os.environ["DATABASE_URL"] = IN_MEMORY_DB
os.environ.setdefault("SECRET_KEY", "test-secret")

import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.auth import User
from app.models.product import Favorite, Product, ProductRating, ProductVariant

engine = create_engine(
    IN_MEMORY_DB,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


class ProductTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def setUp(self):
        db = TestingSession()
        try:
            db.query(Favorite).delete()
            db.query(ProductRating).delete()
            db.query(ProductVariant).delete()
            db.query(Product).delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()

    def make_user(self, email="user@example.com"):
        db = TestingSession()
        try:
            user = User(email=email)
            db.add(user)
            db.commit()
            db.refresh(user)
            return user.id
        finally:
            db.close()

    def auth_headers(self, user_id):
        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def make_product(self, **kwargs):
        fields = {
            "name": "Phone",
            "description": "A phone",
            "category": "Mobiles",
            "sub_category": "Smartphones",
            "brand": "Generic",
            "price": 499.00,
            "old_price": 799.00,
            "image_urls": ["http://img/1.png", "http://img/2.png"],
            "offers": ["Bank Offer Flat Rs.500 off"],
            "is_active": True,
        }
        fields.update(kwargs)
        db = TestingSession()
        try:
            product = Product(**fields)
            db.add(product)
            db.commit()
            db.refresh(product)
            return product.id
        finally:
            db.close()

    def make_variant(self, product_id, **kwargs):
        fields = {
            "sku": f"SKU-{product_id}-{ProductVariant.__tablename__}",
            "options": [{"type": "color", "label": "Color", "value": "Black"}],
            "price": 499.00,
            "old_price": 799.00,
            "stock": 10,
            "images": ["http://img/v1.png"],
            "is_active": True,
        }
        fields.update(kwargs)
        db = TestingSession()
        try:
            variant = ProductVariant(product_id=product_id, **fields)
            db.add(variant)
            db.commit()
            db.refresh(variant)
            return variant.id
        finally:
            db.close()


class ProductListingTest(ProductTestBase):
    def test_empty_database_returns_empty_list(self):
        response = self.client.get("/products")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"total": 0, "skip": 0, "limit": 20, "items": []}
        )

    def test_lists_products_with_expected_fields(self):
        self.make_product()

        response = self.client.get("/products")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        item = body["items"][0]
        self.assertEqual(
            set(item.keys()),
            {
                "id",
                "name",
                "price",
                "old_price",
                "images",
                "category",
                "sub_category",
                "brand",
                "rating",
                "reviews",
                "is_favorite",
            },
        )
        self.assertEqual(item["name"], "Phone")
        self.assertEqual(item["price"], 499.0)
        self.assertEqual(item["old_price"], 799.0)
        self.assertEqual(item["images"], ["http://img/1.png", "http://img/2.png"])
        self.assertEqual(item["category"], "Mobiles")
        self.assertEqual(item["sub_category"], "Smartphones")
        self.assertEqual(item["brand"], "Generic")

    def test_product_without_images_returns_empty_list(self):
        self.make_product(image_urls=[])

        item = self.client.get("/products").json()["items"][0]

        self.assertEqual(item["images"], [])

    def test_rating_and_reviews_default_to_zero(self):
        self.make_product()

        item = self.client.get("/products").json()["items"][0]

        self.assertEqual(item["rating"], 0.0)
        self.assertEqual(item["reviews"], 0)

    def test_rating_and_reviews_are_computed(self):
        user_id = self.make_user()
        product_id = self.make_product()
        db = TestingSession()
        try:
            db.add_all(
                [
                    ProductRating(product_id=product_id, user_id=user_id, rating=5),
                    ProductRating(product_id=product_id, user_id=user_id, rating=3),
                ]
            )
            db.commit()
        finally:
            db.close()

        item = self.client.get("/products").json()["items"][0]

        self.assertEqual(item["rating"], 4.0)
        self.assertEqual(item["reviews"], 2)

    def test_inactive_products_are_excluded(self):
        self.make_product(name="Active")
        self.make_product(name="Hidden", is_active=False)

        body = self.client.get("/products").json()

        self.assertEqual(body["total"], 1)
        self.assertEqual([i["name"] for i in body["items"]], ["Active"])

    def test_pagination(self):
        for index in range(5):
            self.make_product(name=f"Product {index}")

        body = self.client.get("/products?skip=2&limit=2").json()

        self.assertEqual(body["total"], 5)
        self.assertEqual(body["skip"], 2)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(
            [i["name"] for i in body["items"]], ["Product 2", "Product 3"]
        )

    def test_is_favorite_false_for_anonymous_and_invalid_token(self):
        self.make_product()

        anonymous = self.client.get("/products").json()["items"][0]
        invalid = self.client.get(
            "/products", headers={"Authorization": "Bearer invalid"}
        ).json()["items"][0]

        self.assertFalse(anonymous["is_favorite"])
        self.assertFalse(invalid["is_favorite"])

    def test_is_favorite_true_for_favoriting_user(self):
        user_id = self.make_user()
        other_id = self.make_user(email="other@example.com")
        product_id = self.make_product()
        db = TestingSession()
        try:
            db.add(Favorite(user_id=user_id, product_id=product_id))
            db.commit()
        finally:
            db.close()

        mine = self.client.get(
            "/products", headers=self.auth_headers(user_id)
        ).json()["items"][0]
        other = self.client.get(
            "/products", headers=self.auth_headers(other_id)
        ).json()["items"][0]

        self.assertTrue(mine["is_favorite"])
        self.assertFalse(other["is_favorite"])


class ProductCreationTest(ProductTestBase):
    def test_requires_authentication(self):
        response = self.client.post(
            "/products", json={"name": "Phone", "price": 499}
        )

        self.assertEqual(response.status_code, 401)

    def test_rejects_invalid_payload(self):
        user_id = self.make_user()
        headers = self.auth_headers(user_id)

        cases = (
            {"price": 499},  # missing name
            {"name": "x", "price": 499},  # name too short
            {"name": "Phone", "price": -5},  # negative price
            {"name": "Phone"},  # missing price
        )
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/products", json=payload, headers=headers
                )
                self.assertEqual(response.status_code, 422)

    def test_creates_product_with_discount(self):
        user_id = self.make_user()

        response = self.client.post(
            "/products",
            json={
                "name": "Phone",
                "description": "Nice",
                "price": 499,
                "old_price": 799,
                "images": ["http://img/1.png", "http://img/2.png", "http://img/3.png"],
            },
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["name"], "Phone")
        self.assertEqual(body["description"], "Nice")
        self.assertEqual(body["price"], 499.0)
        self.assertEqual(body["old_price"], 799.0)
        self.assertEqual(
            body["images"],
            ["http://img/1.png", "http://img/2.png", "http://img/3.png"],
        )
        self.assertTrue(body["is_active"])
        self.assertEqual(body["rating"], 0.0)
        self.assertEqual(body["reviews"], 0)
        self.assertFalse(body["is_favorite"])

    def test_rejects_too_many_images(self):
        user_id = self.make_user()

        response = self.client.post(
            "/products",
            json={"name": "Phone", "price": 499, "images": [f"http://img/{i}.png" for i in range(11)]},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 422)

    def test_creates_product_with_defaults(self):
        user_id = self.make_user()

        response = self.client.post(
            "/products",
            json={"name": "Cable", "price": 199},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsNone(body["old_price"])
        self.assertEqual(body["images"], [])
        self.assertTrue(body["is_active"])

    def test_creates_product_with_category_brand_offers_and_variants(self):
        user_id = self.make_user()

        response = self.client.post(
            "/products",
            json={
                "name": "Galaxy S24",
                "description": "Premium smartphone.",
                "category": "Mobiles",
                "sub_category": "Smartphones",
                "brand": "Samsung",
                "price": 74999,
                "old_price": 81999,
                "images": ["http://img/s24-1.png"],
                "offers": ["Bank Offer Flat Rs.1500 off on select cards"],
                "variants": [
                    {
                        "sku": "S24-BLACK-128",
                        "options": [
                            {"type": "color", "label": "Color", "value": "Onyx Black"},
                            {"type": "storage", "label": "Storage", "value": "128 GB"},
                        ],
                        "price": 74999,
                        "old_price": 81999,
                        "stock": 12,
                        "images": ["http://img/s24-black.png"],
                    },
                    {"sku": "S24-GRAY-256", "price": 79999, "stock": 5},
                ],
            },
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["category"], "Mobiles")
        self.assertEqual(body["sub_category"], "Smartphones")
        self.assertEqual(body["brand"], "Samsung")
        self.assertEqual(body["offers"], ["Bank Offer Flat Rs.1500 off on select cards"])
        self.assertEqual(len(body["variants"]), 2)
        first, second = body["variants"]
        self.assertEqual(first["sku"], "S24-BLACK-128")
        self.assertEqual(
            first["options"],
            [
                {"type": "color", "label": "Color", "value": "Onyx Black"},
                {"type": "storage", "label": "Storage", "value": "128 GB"},
            ],
        )
        self.assertEqual(first["stock"], 12)
        self.assertEqual(first["images"], ["http://img/s24-black.png"])
        self.assertEqual(second["options"], [])
        self.assertEqual(second["stock"], 5)

    def test_rejects_duplicate_variant_sku(self):
        user_id = self.make_user()
        headers = self.auth_headers(user_id)
        payload = {
            "name": "Phone",
            "price": 499,
            "variants": [{"sku": "DUP-1", "price": 499}],
        }

        self.assertEqual(
            self.client.post("/products", json=payload, headers=headers).status_code,
            201,
        )
        response = self.client.post("/products", json=payload, headers=headers)

        self.assertEqual(response.status_code, 409)

    def test_created_product_appears_in_listing(self):
        user_id = self.make_user()
        self.client.post(
            "/products",
            json={"name": "Cable", "price": 199},
            headers=self.auth_headers(user_id),
        )

        body = self.client.get("/products").json()

        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["name"], "Cable")


class ProductUpdateTest(ProductTestBase):
    def test_requires_authentication(self):
        product_id = self.make_product()

        response = self.client.patch(
            f"/products/{product_id}", json={"price": 399}
        )

        self.assertEqual(response.status_code, 401)

    def test_returns_404_for_missing_product(self):
        user_id = self.make_user()

        response = self.client.patch(
            "/products/999",
            json={"price": 399},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 404)

    def test_rejects_empty_and_invalid_payloads(self):
        user_id = self.make_user()
        headers = self.auth_headers(user_id)
        product_id = self.make_product()

        self.assertEqual(
            self.client.patch(
                f"/products/{product_id}", json={}, headers=headers
            ).status_code,
            422,
        )
        for payload in (
            {"price": -1},
            {"name": "x"},
            {"price": None},
            {"name": None},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.client.patch(
                        f"/products/{product_id}", json=payload, headers=headers
                    ).status_code,
                    422,
                )

    def test_partial_update_preserves_other_fields(self):
        user_id = self.make_user()
        product_id = self.make_product()

        response = self.client.patch(
            f"/products/{product_id}",
            json={"price": 399},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["price"], 399.0)
        self.assertEqual(body["name"], "Phone")
        self.assertEqual(body["description"], "A phone")
        self.assertEqual(body["old_price"], 799.0)

    def test_explicit_null_removes_discount(self):
        user_id = self.make_user()
        product_id = self.make_product(old_price=799.00)

        response = self.client.patch(
            f"/products/{product_id}",
            json={"old_price": None},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["old_price"])

    def test_replaces_images(self):
        user_id = self.make_user()
        product_id = self.make_product()

        response = self.client.patch(
            f"/products/{product_id}",
            json={"images": ["http://img/new.png"]},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["images"], ["http://img/new.png"])

    def test_empty_images_clears_all_images(self):
        user_id = self.make_user()
        product_id = self.make_product()

        response = self.client.patch(
            f"/products/{product_id}",
            json={"images": []},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["images"], [])

    def test_replaces_variants(self):
        user_id = self.make_user()
        headers = self.auth_headers(user_id)
        product_id = self.make_product()
        self.make_variant(product_id, sku="OLD-1")

        response = self.client.patch(
            f"/products/{product_id}",
            json={
                "variants": [
                    {"sku": "NEW-1", "price": 399, "stock": 3},
                    {"sku": "NEW-2", "price": 499, "stock": 7},
                ]
            },
            headers=headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [v["sku"] for v in response.json()["variants"]], ["NEW-1", "NEW-2"]
        )

    def test_rejects_null_variants(self):
        user_id = self.make_user()
        product_id = self.make_product()

        response = self.client.patch(
            f"/products/{product_id}",
            json={"variants": None},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 422)

    def test_rejects_duplicate_variant_sku_on_update(self):
        user_id = self.make_user()
        headers = self.auth_headers(user_id)
        first_id = self.make_product(name="First")
        second_id = self.make_product(name="Second")
        self.make_variant(first_id, sku="TAKEN-1")

        response = self.client.patch(
            f"/products/{second_id}",
            json={"variants": [{"sku": "TAKEN-1", "price": 100}]},
            headers=headers,
        )

        self.assertEqual(response.status_code, 409)

    def test_updates_category_brand_and_offers(self):
        user_id = self.make_user()
        product_id = self.make_product()

        response = self.client.patch(
            f"/products/{product_id}",
            json={
                "category": "Electronics",
                "brand": "Acme",
                "offers": ["No Cost EMI available"],
            },
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["category"], "Electronics")
        self.assertEqual(body["brand"], "Acme")
        self.assertEqual(body["offers"], ["No Cost EMI available"])

    def test_deactivated_product_leaves_listing(self):
        user_id = self.make_user()
        product_id = self.make_product()

        response = self.client.patch(
            f"/products/{product_id}",
            json={"is_active": False},
            headers=self.auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_active"])
        self.assertEqual(self.client.get("/products").json()["total"], 0)


class ProductDetailTest(ProductTestBase):
    def test_returns_404_for_missing_product(self):
        response = self.client.get("/products/999")

        self.assertEqual(response.status_code, 404)

    def test_returns_full_detail_with_offers_variants_rating_and_reviews(self):
        user_id = self.make_user()
        product_id = self.make_product()
        self.make_variant(product_id, sku="DET-1", price=499.00, stock=4)
        db = TestingSession()
        try:
            db.add_all(
                [
                    ProductRating(product_id=product_id, user_id=user_id, rating=5),
                    ProductRating(product_id=product_id, user_id=user_id, rating=4),
                ]
            )
            db.add(Favorite(user_id=user_id, product_id=product_id))
            db.commit()
        finally:
            db.close()

        response = self.client.get(
            f"/products/{product_id}", headers=self.auth_headers(user_id)
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["description"], "A phone")
        self.assertEqual(body["category"], "Mobiles")
        self.assertEqual(body["brand"], "Generic")
        self.assertEqual(body["offers"], ["Bank Offer Flat Rs.500 off"])
        self.assertEqual(body["rating"], 4.5)
        self.assertEqual(body["reviews"], 2)
        self.assertTrue(body["is_favorite"])
        self.assertEqual(len(body["variants"]), 1)
        self.assertEqual(body["variants"][0]["sku"], "DET-1")


if __name__ == "__main__":
    unittest.main()
