import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

# SQLAlchemy's QueuePool defaults to 5 connections plus 10 overflow, which caps
# this API at 15 simultaneous database users and turns every request beyond that
# into a 30-second stall. Server databases expose far more concurrency, so their
# pool is sized from the environment instead.
DEFAULT_POOL_SIZE = 20
DEFAULT_MAX_OVERFLOW = 20
DEFAULT_POOL_TIMEOUT = 30

# FastAPI runs synchronous endpoints on AnyIO's threadpool, which defaults to 40
# tokens. Anything above that queues rather than executes, so it has to be raised
# to let the supported concurrency actually reach the database.
DEFAULT_THREAD_LIMIT = 100


def _env_int(name: str, default: int) -> int:
    """Read a positive integer from the environment, ignoring bad values."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def concurrency_thread_limit() -> int:
    """Maximum sync endpoints that may run database work at the same time."""
    return _env_int("DB_THREAD_LIMIT", DEFAULT_THREAD_LIMIT)


def normalise_database_url(database_url: str) -> str:
    """Map supported connection URLs onto the drivers this project installs."""
    # Render may provide the legacy "postgres://" URL scheme.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    # This project installs psycopg2-binary, so select its SQLAlchemy driver explicitly.
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace(
            "postgresql://", "postgresql+psycopg2://", 1
        )

    # SQLAlchemy needs the PyMySQL dialect driver for MySQL connection URLs.
    if database_url.startswith("mysql://"):
        database_url = database_url.replace("mysql://", "mysql+pymysql://", 1)

    return database_url


def _is_sqlite_memory_url(database_url: str) -> bool:
    return database_url in {"sqlite://", "sqlite:///:memory:"}


def _apply_sqlite_pragmas(engine: Engine, *, busy_timeout_ms: int) -> None:
    """Let SQLite serve concurrent readers instead of serialising them.

    The default rollback journal takes an exclusive lock for the whole database
    while writing, so a single write blocks every reader and concurrent requests
    either block or fail. WAL mode removes that conflict: readers never block the
    writer and vice versa. ``busy_timeout`` makes the few genuinely conflicting
    writers wait for the lock instead of raising "database is locked" at once.

    These run per connection because NullPool opens a fresh one per request.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        finally:
            cursor.close()


def build_engine(
    database_url: str,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout: int | None = None,
) -> Engine:
    """Create an engine configured for high read concurrency on any supported database."""
    database_url = normalise_database_url(database_url)

    connect_args: dict = {}
    engine_kwargs: dict = {"pool_pre_ping": True}

    if database_url.startswith("sqlite"):
        # check_same_thread is required because anyio hands each request to a
        # different worker thread.
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = _env_int("DB_SQLITE_BUSY_TIMEOUT", 30)

        if _is_sqlite_memory_url(database_url):
            # An in-memory database is discarded when its connection closes, so
            # every request has to share the single connection that owns it.
            engine_kwargs["poolclass"] = StaticPool
        else:
            # SQLite connections are cheap and the file is the shared resource.
            # A fixed-size pool only adds a ceiling on concurrency, so connect
            # per request and let SQLite arbitrate access through its own locks.
            engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs["pool_size"] = (
            pool_size if pool_size is not None
            else _env_int("DB_POOL_SIZE", DEFAULT_POOL_SIZE)
        )
        engine_kwargs["max_overflow"] = (
            max_overflow if max_overflow is not None
            else _env_int("DB_MAX_OVERFLOW", DEFAULT_MAX_OVERFLOW)
        )
        engine_kwargs["pool_timeout"] = (
            pool_timeout if pool_timeout is not None
            else _env_int("DB_POOL_TIMEOUT", DEFAULT_POOL_TIMEOUT)
        )
        engine_kwargs["pool_recycle"] = _env_int("DB_POOL_RECYCLE", 1800)

    engine = create_engine(database_url, connect_args=connect_args, **engine_kwargs)

    if database_url.startswith("sqlite"):
        _apply_sqlite_pragmas(
            engine, busy_timeout_ms=connect_args["timeout"] * 1000
        )

    return engine


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./auth.db")

engine = build_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


def ensure_wishlist_product_foreign_key() -> None:
    """Add the product FK to existing MySQL/PostgreSQL wishlist tables.

    create_all creates the constraint for new databases but does not alter an
    existing wishlist_items table. SQLite cannot add a foreign key with ALTER
    TABLE, so existing SQLite files require a table rebuild to gain the FK.
    """
    if engine.dialect.name not in {"mysql", "postgresql"}:
        return

    inspector = inspect(engine)
    if "wishlist_items" not in inspector.get_table_names():
        return
    for foreign_key in inspector.get_foreign_keys("wishlist_items"):
        if foreign_key.get("referred_table") == "products":
            return

    with engine.begin() as connection:
        orphan = connection.execute(text("""
            SELECT wishlist_items.product_id
            FROM wishlist_items
            LEFT JOIN products ON products.id = wishlist_items.product_id
            WHERE products.id IS NULL
            LIMIT 1
        """)).first()
        if orphan:
            raise RuntimeError(
                "Cannot add the wishlist product foreign key: "
                f"wishlist product_id {orphan[0]} does not exist in products. "
                "Repair or remove that wishlist row, then restart the API."
            )

        connection.execute(text("""
            ALTER TABLE wishlist_items
            ADD CONSTRAINT fk_wishlist_items_product_id_products
            FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE CASCADE
        """))


def ensure_wishlist_variant_column() -> None:
    """Add variant_id to wishlist rows created by older application versions."""
    inspector = inspect(engine)
    if "wishlist_items" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("wishlist_items")}
    if "variant_id" in columns:
        return

    # Nullable keeps existing wishlist entries valid because their former
    # product-only records did not retain a selected variant.
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE wishlist_items ADD COLUMN variant_id VARCHAR(255) NULL"
        ))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
