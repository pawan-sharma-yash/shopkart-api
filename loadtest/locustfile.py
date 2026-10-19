"""Locust workload for read-heavy ShopKart API load testing.

Set LOAD_TEST_TOKEN to include authenticated cart and order-history reads.
"""

import os

from locust import HttpUser, constant, task


PRODUCT_ID = os.getenv("LOAD_TEST_PRODUCT_ID", "1")
ACCESS_TOKEN = os.getenv("LOAD_TEST_TOKEN")


class ShopKartUser(HttpUser):
    # Keep each simulated user issuing requests as soon as its previous request
    # completes. The Locust user count controls concurrent simulated clients.
    wait_time = constant(0)

    @task(5)
    def list_products(self):
        self.client.get("/products", name="/products")

    @task(3)
    def load_home_page(self):
        self.client.get("/home", name="/home")

    @task(2)
    def get_product(self):
        self.client.get(f"/products/{PRODUCT_ID}", name="/products/{product_id}")

    @task(1)
    def read_authenticated_data(self):
        if not ACCESS_TOKEN:
            return

        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        self.client.get("/cart", headers=headers, name="/cart (authenticated)")
        self.client.get("/orders", headers=headers, name="/orders (authenticated)")
