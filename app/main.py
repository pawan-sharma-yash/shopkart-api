from dotenv import load_dotenv
from fastapi import FastAPI

from . import models
from .api.v1.api import api_router
from .db.base import Base
from .db.session import SessionLocal, engine
from .models.product import Product

load_dotenv()

# models is imported for its side effect: registering SQLAlchemy tables with
# Base.metadata before the tables are created.
assert models is not None

Base.metadata.create_all(bind=engine)


def _seed_products() -> None:
    """Insert a few sample products on a fresh database so the public
    listing endpoint returns useful data out of the box."""
    db = SessionLocal()
    try:
        if db.query(Product).count() > 0:
            return
        samples = [
            # (name, description, price, old_price, image_url)
            # old_price is the pre-discount price; None means no discount.
            ("Classic Cotton T-Shirt", "Soft breathable cotton t-shirt.", 499.00, 799.00, "https://picsum.photos/seed/tshirt/600/600"),
            ("Denim Jeans", "Slim-fit stretch denim jeans.", 1499.00, 1999.00, "https://picsum.photos/seed/jeans/600/600"),
            ("Running Shoes", "Lightweight running shoes.", 2499.00, None, "https://picsum.photos/seed/shoes/600/600"),
            ("Leather Wallet", "Genuine leather bifold wallet.", 899.00, 1199.00, "https://picsum.photos/seed/wallet/600/600"),
            ("Wireless Headphones", "Noise-cancelling over-ear headphones.", 3999.00, 5999.00, "https://picsum.photos/seed/headphones/600/600"),
            ("Smart Watch", "Fitness tracking smart watch.", 5999.00, None, "https://picsum.photos/seed/watch/600/600"),
            ("Backpack", "Water-resistant travel backpack.", 1799.00, 2299.00, "https://picsum.photos/seed/backpack/600/600"),
            ("Sunglasses", "UV-protected aviator sunglasses.", 1299.00, None, "https://picsum.photos/seed/sunglasses/600/600"),
        ]
        for name, description, price, old_price, image_url in samples:
            db.add(Product(name=name, description=description, price=price, old_price=old_price, image_url=image_url))
        db.commit()
    finally:
        db.close()


_seed_products()
app = FastAPI(title="ShopKart Authentication API", version="1.0.0")
app.include_router(api_router)
