from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Integer, inspect, select, text

from . import models
from .api.v1.api import api_router
from .db.base import Base
from .db.session import (
    SessionLocal,
    engine,
    ensure_order_address_reference,
    ensure_order_public_id,
    ensure_refresh_session_keys,
    ensure_wishlist_product_foreign_key,
    ensure_wishlist_variant_column,
)
from .models.address import Address
from .models.auth import User
from .services.product_catalog import migrate_legacy_variants, seed_product_catalog

# Register all SQLAlchemy models before creating their tables.
assert models is not None
Base.metadata.create_all(bind=engine)
ensure_order_address_reference()
ensure_order_public_id()
ensure_refresh_session_keys()
migrate_legacy_variants(engine)

with SessionLocal() as catalog_db:
    seed_product_catalog(catalog_db)
ensure_wishlist_product_foreign_key()
ensure_wishlist_variant_column()


def update_existing_user_columns() -> None:
    """Add registration fields and migrate legacy gender strings to numeric codes."""
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    columns = inspector.get_columns("users")
    existing_columns = {column["name"] for column in columns}
    gender_column = next(
        (column for column in columns if column["name"] == "gender"), None
    )
    new_columns = {
        "password_hash": "VARCHAR(512)",
        "phone_number": "VARCHAR(20)",
        "address": "VARCHAR(500)",
        "pin": "VARCHAR(12)",
    }

    with engine.begin() as connection:
        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}")
                )

        if gender_column and not isinstance(gender_column["type"], Integer):
            if engine.dialect.name == "postgresql":
                connection.execute(text("""
                    ALTER TABLE users ALTER COLUMN gender TYPE INTEGER USING
                    CASE LOWER(gender)
                        WHEN 'male' THEN 0
                        WHEN '0' THEN 0
                        WHEN 'female' THEN 1
                        WHEN '1' THEN 1
                        WHEN 'other' THEN 2
                        WHEN 'others' THEN 2
                        WHEN '2' THEN 2
                        ELSE NULL
                    END
                """))
            else:
                connection.execute(text("""
                    UPDATE users SET gender = CASE LOWER(CAST(gender AS TEXT))
                        WHEN 'male' THEN 0
                        WHEN '0' THEN 0
                        WHEN 'female' THEN 1
                        WHEN '1' THEN 1
                        WHEN 'other' THEN 2
                        WHEN 'others' THEN 2
                        WHEN '2' THEN 2
                        ELSE NULL
                    END
                """))


update_existing_user_columns()


def update_existing_address_columns() -> None:
    """Add structured address fields to installations with the older table shape."""
    inspector = inspect(engine)
    if "addresses" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("addresses")
    }
    new_columns = {
        "street": "VARCHAR(255)",
        "city": "VARCHAR(120)",
        "state": "VARCHAR(120)",
    }
    with engine.begin() as connection:
        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE addresses ADD COLUMN {column_name} {column_type}"
                    )
                )


update_existing_address_columns()


def migrate_legacy_user_addresses() -> None:
    """Copy existing registration addresses into the dedicated addresses table."""
    with SessionLocal() as db:
        users = db.scalars(
            select(User).where(User.address.is_not(None), User.address != "")
        ).all()
        for user in users:
            existing = db.scalar(
                select(Address.id).where(
                    Address.user_id == user.id,
                    Address.address == user.address,
                    Address.pin == user.pin,
                )
            )
            if existing is None:
                db.add(
                    Address(
                        user_id=user.id,
                        label="Home",
                        address=user.address,
                        pin=user.pin,
                    )
                )
        db.commit()


migrate_legacy_user_addresses()

app = FastAPI(title="ShopKart Authentication API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://127.0.0.1:8081",
        "http://localhost:8082",
        "http://127.0.0.1:8082",
        "http://localhost:8083",
        "http://127.0.0.1:8083",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8084",
        "http://127.0.0.1:8084",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)
