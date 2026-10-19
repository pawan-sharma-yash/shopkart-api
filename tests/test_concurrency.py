import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "shopkart-test-secret-key-at-least-32-bytes-long")
os.environ["MAIL_USERNAME"] = ""
os.environ["MAIL_PASSWORD"] = ""
os.environ["MAIL_FROM"] = ""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from app.db.base import Base
from app.db.session import build_engine, concurrency_thread_limit
from app.main import app
from app.models.product import Product


class EnginePoolConfigTest(unittest.TestCase):
    def test_sqlite_memory_uses_static_pool(self):
        """An in-memory database lives only as long as its connection, so it
        must be shared rather than pooled or it would be empty every checkout."""
        engine = build_engine("sqlite:///:memory:")

        self.assertIsInstance(engine.pool, StaticPool)

    def test_sqlite_file_url_uses_null_pool(self):
        engine = build_engine("sqlite:////tmp/shopkart-pool-test.db")

        self.assertIsInstance(engine.pool, NullPool)

    def test_postgres_uses_configurable_queue_pool(self):
        engine = build_engine(
            "postgresql://user:pass@localhost/shopkart",
            pool_size=42,
            max_overflow=7,
            pool_timeout=11,
        )

        self.assertIsInstance(engine.pool, QueuePool)
        self.assertEqual(engine.pool.size(), 42)
        self.assertEqual(engine.pool._max_overflow, 7)
        self.assertEqual(engine.pool._timeout, 11)

    def test_mysql_uses_configurable_queue_pool(self):
        engine = build_engine(
            "mysql+pymysql://user:pass@localhost/shopkart",
            pool_size=13,
            max_overflow=4,
            pool_timeout=5,
        )

        self.assertIsInstance(engine.pool, QueuePool)
        self.assertEqual(engine.pool.size(), 13)

    def test_sqlite_disables_check_same_thread(self):
        """SQLite connections are opened per request across worker threads."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "threads.db"
            engine = build_engine(f"sqlite:///{db_path}")

            with engine.connect() as connection:
                connection.execute(text("CREATE TABLE t (id INTEGER)"))
                rows = connection.execute(text("SELECT count(*) FROM t")).scalar()

        self.assertEqual(rows, 0)
        self.assertIsInstance(engine.pool, NullPool)

    def test_non_sqlite_dialects_are_unchanged(self):
        """Existing PostgreSQL/MySQL URL normalisation must keep working."""
        from app.db.session import normalise_database_url

        self.assertEqual(
            normalise_database_url("postgres://u:p@h/db"), "postgresql+psycopg2://u:p@h/db"
        )
        self.assertEqual(
            normalise_database_url("mysql://u:p@h/db"), "mysql+pymysql://u:p@h/db"
        )
        self.assertEqual(normalise_database_url("sqlite:///./auth.db"), "sqlite:///./auth.db")


class SqlitePragmaTest(unittest.TestCase):
    def test_wal_and_busy_timeout_applied_to_new_connections(self):
        """Every fresh connection must get WAL plus a non-zero busy timeout."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "pragmas.db"
            engine = build_engine(f"sqlite:///{db_path}")

            with engine.connect() as connection:
                journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
                busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()

        self.assertEqual(journal_mode, "wal")
        self.assertGreater(int(busy_timeout), 0)

    def test_wal_survives_reconnecting(self):
        """NullPool opens a new connection per request; pragmas must re-apply."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "reconnect.db"
            engine = build_engine(f"sqlite:///{db_path}")

            for _ in range(3):
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal"
                    )
                    self.assertGreater(
                        int(connection.exec_driver_sql("PRAGMA busy_timeout").scalar()), 0
                    )

    def test_wal_allows_reader_during_writer_transaction(self):
        """WAL is what lets a read proceed while a write transaction is open."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "wal_reads.db"
            engine = build_engine(f"sqlite:///{db_path}")
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
                connection.execute(text("INSERT INTO t (id) VALUES (1)"))

            writer = engine.connect()
            writer.execute(text("BEGIN"))
            writer.execute(text("INSERT INTO t (id) VALUES (2)"))

            # Without WAL this read would raise "database is locked".
            with engine.connect() as reader:
                self.assertEqual(reader.execute(text("SELECT count(*) FROM t")).scalar(), 1)

            writer.rollback()
            writer.close()


class ConcurrencyLimitTest(unittest.TestCase):
    def test_thread_limit_supports_at_least_100_requests(self):
        self.assertGreaterEqual(concurrency_thread_limit(), 100)

    def test_thread_limit_honours_environment_override(self):
        os.environ["DB_THREAD_LIMIT"] = "250"
        try:
            self.assertEqual(concurrency_thread_limit(), 250)
        finally:
            del os.environ["DB_THREAD_LIMIT"]

    def test_invalid_thread_limit_falls_back_to_default(self):
        os.environ["DB_THREAD_LIMIT"] = "not-a-number"
        try:
            self.assertGreaterEqual(concurrency_thread_limit(), 100)
        finally:
            del os.environ["DB_THREAD_LIMIT"]

    def test_app_raises_threadpool_limiter_on_startup(self):
        """FastAPI defaults to 40 threadpool tokens; 100 requests need more."""
        import anyio.to_thread

        with TestClient(app) as client:
            limiter = client.portal.call(anyio.to_thread.current_default_thread_limiter)

            self.assertGreaterEqual(limiter.total_tokens, 100)


class DatabaseOverloadResponseTest(unittest.TestCase):
    """Pool exhaustion must surface as 503, not an unhandled 500."""

    def setUp(self):
        from app.db.session import get_db

        self.client = TestClient(app, raise_server_exceptions=False)
        self._get_db = get_db

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        self.client.close()

    def _override_with(self, exception):
        def _raise():
            raise exception

        return _raise

    def test_pool_timeout_returns_503_with_retry_after(self):
        from sqlalchemy.exc import TimeoutError as SATimeoutError

        app.dependency_overrides[self._get_db] = self._override_with(
            SATimeoutError("QueuePool limit reached")
        )

        response = self.client.get("/products")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("Retry-After", response.headers)

    def test_operational_error_returns_503(self):
        from sqlalchemy.exc import OperationalError

        app.dependency_overrides[self._get_db] = self._override_with(
            OperationalError("SELECT 1", {}, Exception("database is locked"))
        )

        response = self.client.get("/products")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_unrelated_errors_still_return_500(self):
        app.dependency_overrides[self._get_db] = self._override_with(
            RuntimeError("something else entirely")
        )

        response = self.client.get("/products")

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)


class StaticPoolMemoryLimitTest(unittest.TestCase):
    """In-memory SQLite cannot support concurrent readers, and that is expected.

    ``sqlite:///:memory:`` stores its data inside a single connection, so
    sharing one connection is the only way to keep the tables alive at all --
    but concurrent sessions then interleave transactions on it and reads become
    unreliable. Production and load-test runs use a file database, which is
    covered by ConcurrentSessionTest below.
    """

    def test_memory_database_keeps_its_tables(self):
        """The reason StaticPool is used at all: data must survive checkouts."""
        engine = build_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE t (id INTEGER)"))
            connection.execute(text("INSERT INTO t (id) VALUES (1)"))

        for _ in range(5):
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(text("SELECT count(*) FROM t")).scalar(), 1
                )


class ConcurrentSessionTest(unittest.TestCase):
    """Regression guard for the reported failure.

    Before this change a file-backed SQLite engine used SQLAlchemy's default
    QueuePool of 5 plus 10 overflow. Past those 15 concurrent requests every
    caller waited 30 seconds and then failed with a pool TimeoutError, which
    surfaced to clients as HTTP 500.
    """

    def _seed(self, engine):
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(bind=engine)
        with session_factory() as session:
            session.add(
                Product(id=1, name="Widget", category="Tools", offers=[], variants=[])
            )
            session.commit()
        return session_factory

    def test_100_concurrent_reads_do_not_exhaust_the_pool(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = build_engine(f"sqlite:///{Path(tmp_dir) / 'concurrent.db'}")
            session_factory = self._seed(engine)
            errors = []
            rows_seen = []
            lock = threading.Lock()

            def _read(_):
                try:
                    with session_factory() as session:
                        count = session.scalar(
                            text("SELECT count(*) FROM products WHERE id = 1")
                        )
                    with lock:
                        rows_seen.append(count)
                except Exception as exc:  # noqa: BLE001 - recorded for assertion
                    with lock:
                        errors.append(f"{type(exc).__name__}: {exc}")

            with ThreadPoolExecutor(max_workers=100) as pool:
                list(pool.map(_read, range(100)))

            self.assertEqual(errors, [])
            self.assertEqual(len(rows_seen), 100)
            self.assertTrue(all(count == 1 for count in rows_seen))

    def test_100_concurrent_writers_are_serialised_without_locking_errors(self):
        """WAL plus busy_timeout must make contending writers wait, not fail."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = build_engine(f"sqlite:///{Path(tmp_dir) / 'writers.db'}")
            session_factory = self._seed(engine)
            errors = []
            lock = threading.Lock()

            def _write(index):
                try:
                    with session_factory() as session:
                        session.execute(
                            text(
                                "INSERT INTO products "
                                "(name, category, reviews, offers, variants) "
                                "VALUES (:n, 'Tools', 0, '[]', '[]')"
                            ),
                            {"n": f"p{index}"},
                        )
                        session.commit()
                except Exception as exc:  # noqa: BLE001 - recorded for assertion
                    with lock:
                        errors.append(f"{type(exc).__name__}: {exc}")

            with ThreadPoolExecutor(max_workers=100) as pool:
                list(pool.map(_write, range(100)))

            with session_factory() as session:
                total = session.scalar(text("SELECT count(*) FROM products"))

        self.assertEqual(errors, [])
        self.assertEqual(total, 101)


if __name__ == "__main__":
    unittest.main()