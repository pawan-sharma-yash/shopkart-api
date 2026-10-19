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

def ensure_order_address_reference(target_engine: Engine = engine) -> None:
    """Add the nullable saved-address reference to existing order tables."""
    inspector = inspect(target_engine)
    tables = set(inspector.get_table_names())
    if "orders" not in tables or "addresses" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("orders")}
    foreign_keys = inspector.get_foreign_keys("orders")
    has_address_fk = any(
        foreign_key.get("constrained_columns") == ["shipping_address_id"]
        and foreign_key.get("referred_table") == "addresses"
        for foreign_key in foreign_keys
    )
    indexes = {index["name"] for index in inspector.get_indexes("orders")}
    index_name = "ix_orders_shipping_address_id"

    with target_engine.begin() as connection:
        if "shipping_address_id" not in columns:
            if target_engine.dialect.name == "mysql":
                connection.execute(text(
                    "ALTER TABLE orders ADD COLUMN shipping_address_id INTEGER NULL"
                ))
            else:
                connection.execute(text("""
                    ALTER TABLE orders ADD COLUMN shipping_address_id INTEGER NULL
                    REFERENCES addresses(id) ON DELETE SET NULL
                """))
            has_address_fk = target_engine.dialect.name != "mysql"

        if not has_address_fk and target_engine.dialect.name in {"mysql", "postgresql"}:
            connection.execute(text("""
                ALTER TABLE orders
                ADD CONSTRAINT fk_orders_shipping_address_id_addresses
                FOREIGN KEY (shipping_address_id) REFERENCES addresses(id)
                ON DELETE SET NULL
            """))

        if index_name not in indexes:
            if target_engine.dialect.name == "mysql":
                connection.execute(text(
                    f"CREATE INDEX {index_name} ON orders (shipping_address_id)"
                ))
            else:
                connection.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    "ON orders (shipping_address_id)"
                ))
def ensure_refresh_session_keys() -> None:
    """Repair legacy PostgreSQL refresh-session keys without dropping sessions.

    Older Neon databases may have the SQLAlchemy sequence default but lack the
    primary-key and unique-index constraints declared by the current model.
    Duplicate IDs are reassigned so every existing refresh session is retained.
    """
    if engine.dialect.name != "postgresql":
        return

    inspector = inspect(engine)
    if "refresh_sessions" not in inspector.get_table_names():
        return

    with engine.begin() as connection:
        connection.execute(text(
            "LOCK TABLE public.refresh_sessions IN ACCESS EXCLUSIVE MODE"
        ))

        primary_key_columns = connection.execute(text("""
            SELECT array_agg(a.attname ORDER BY key_column.ordinality)
            FROM pg_constraint AS constraint_row
            CROSS JOIN LATERAL unnest(constraint_row.conkey)
                WITH ORDINALITY AS key_column(attnum, ordinality)
            JOIN pg_attribute AS a
              ON a.attrelid = constraint_row.conrelid
             AND a.attnum = key_column.attnum
            WHERE constraint_row.conrelid = 'public.refresh_sessions'::regclass
              AND constraint_row.contype = 'p'
            GROUP BY constraint_row.oid
        """)).scalar_one_or_none()

        if primary_key_columns not in (None, ["id"]):
            raise RuntimeError(
                "refresh_sessions has a primary key other than id; "
                "the refresh-session key migration stopped without changing rows."
            )

        if primary_key_columns is None:
            duplicate_or_null_ids = connection.execute(text("""
                SELECT COUNT(*)
                FROM (
                    SELECT id
                    FROM public.refresh_sessions
                    GROUP BY id
                    HAVING id IS NULL OR COUNT(*) > 1
                ) AS invalid_ids
            """)).scalar_one()

            if duplicate_or_null_ids:
                connection.execute(text("""
                    WITH ranked AS (
                        SELECT ctid, id,
                               row_number() OVER (
                                   PARTITION BY id ORDER BY ctid
                               ) AS duplicate_rank
                        FROM public.refresh_sessions
                    ), rows_to_rekey AS (
                        SELECT ctid,
                               row_number() OVER (
                                   ORDER BY id NULLS FIRST, ctid
                               ) AS new_rank
                        FROM ranked
                        WHERE id IS NULL OR duplicate_rank > 1
                    ), current_max AS (
                        SELECT COALESCE(MAX(id), 0) AS max_id
                        FROM public.refresh_sessions
                    )
                    UPDATE public.refresh_sessions AS session_row
                    SET id = current_max.max_id + rows_to_rekey.new_rank
                    FROM rows_to_rekey, current_max
                    WHERE session_row.ctid = rows_to_rekey.ctid
                """))

            connection.execute(text("""
                ALTER TABLE public.refresh_sessions
                ADD CONSTRAINT refresh_sessions_pkey PRIMARY KEY (id)
            """))

        unique_token_index = connection.execute(text("""
            SELECT EXISTS (
                SELECT 1
                FROM pg_index AS index_row
                JOIN pg_attribute AS indexed_column
                  ON indexed_column.attrelid = index_row.indrelid
                 AND indexed_column.attnum = index_row.indkey[0]
                WHERE index_row.indrelid = 'public.refresh_sessions'::regclass
                  AND index_row.indisunique
                  AND index_row.indisvalid
                  AND index_row.indnkeyatts = 1
                  AND indexed_column.attname = 'token_id'
            )
        """)).scalar_one()

        if not unique_token_index:
            duplicate_tokens = connection.execute(text("""
                SELECT COUNT(*)
                FROM (
                    SELECT token_id
                    FROM public.refresh_sessions
                    GROUP BY token_id
                    HAVING token_id IS NULL OR COUNT(*) > 1
                ) AS invalid_tokens
            """)).scalar_one()
            if duplicate_tokens:
                raise RuntimeError(
                    "refresh_sessions contains duplicate or null token IDs; "
                    "the unique-index migration stopped without deleting sessions."
                )
            connection.execute(text("""
                CREATE UNIQUE INDEX uq_refresh_sessions_token_id
                ON public.refresh_sessions (token_id)
            """))

        sequence_exists = connection.execute(text("""
            SELECT pg_get_serial_sequence('public.refresh_sessions', 'id')
        """)).scalar_one()
        if sequence_exists:
            connection.execute(text("""
                SELECT setval(
                    pg_get_serial_sequence('public.refresh_sessions', 'id'),
                    COALESCE((SELECT MAX(id) FROM public.refresh_sessions), 1),
                    EXISTS (SELECT 1 FROM public.refresh_sessions)
                )
            """))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
