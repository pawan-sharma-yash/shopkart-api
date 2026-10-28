Here’s a practical way to run the test. Use a staging or test environment, since the load test deliberately sends continuous traffic.

### 1. Point the API at PostgreSQL

Create a dedicated PostgreSQL database and a test user. Set the API’s DATABASE_URL to a SQLAlchemy PostgreSQL URL:

export DATABASE_URL='postgresql://shopkart_test:YOUR_PASSWORD@DB_HOST:5432/shopkart_test'

The app converts postgresql:// to the installed psycopg2 driver. Keep the password out of source control and shell history where possible; use your deployment platform’s secret settings for a hosted environment.

Set a connection pool that fits your database’s connection limit. For a single API worker, you could start with:

export DB_POOL_SIZE=30
export DB_MAX_OVERFLOW=10
export DB_POOL_TIMEOUT=10

Each worker can open up to DB_POOL_SIZE + DB_MAX_OVERFLOW connections. For example, 3 workers with those settings could use up to 120 connections. Leave room for migrations, monitoring, and other applications in PostgreSQL’s connection limit. The app’s default DB_THREAD_LIMIT is 100; don’t raise it blindly, because more threads can increase CPU and database contention.

Start the API in the same environment where those variables are set:

uvicorn app.main:app --host 0.0.0.0 --port 8080

If you run the API in Docker or a hosted service, configure the variables there instead. Make sure the test database contains the catalog data you want to measure; the app seeds products when the database is empty.

### 2. Run Locust from a separate machine

Install Locust in a separate load-generator environment, not in the API container:

python -m pip install locust
mkdir -p loadtest-results

Then run a five-minute test, ramping up 25 users per second:

locust -f loadtest/locustfile.py \
  --host http://API_HOST:8080 \
  --headless \
  --users 250 \
  --spawn-rate 25 \
  --run-time 5m \
  --csv loadtest-results/postgres-250

Replace API_HOST with the address of the API. Locust will save stats, failures, and history CSV files with that prefix. The current workload repeatedly reads /products, /home, and /products/{product_id}. If you want to include authenticated cart and order-history reads, set LOAD_TEST_TOKEN to a valid token for a dedicated test account.

### 3. Watch the API and database during the run

On the API host, use your platform’s CPU and memory graphs or run:

docker stats

For PostgreSQL, connect with psql and check active connections during the run:

SELECT state, count(*)
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY state
ORDER BY state;

You can also sample PostgreSQL’s cumulative transaction counters before and after each run:

SELECT
  datname,
  xact_commit,
  xact_rollback,
  blks_read,
  blks_hit
FROM pg_stat_database
WHERE datname = current_database();

For a more useful test report, record the PostgreSQL connection count over time, plus CPU and memory for both API and database. Locust’s summary and *_stats.csv give request counts, failures, throughput, and latency percentiles.

### 4. Interpret the result

The Locust workload uses constant(0): each simulated user sends another request immediately after the previous one finishes. So 250 users means 250 simulated clients, not necessarily 250 requests executing inside the API at once. The current API’s sync thread limit is 100, which can cap simultaneous sync endpoint work.

Start with this run and check for failures, latency growth, API CPU saturation, and database connection exhaustion. If it looks healthy, increase users gradually. Run a separate test with an appropriate burst or arrival-rate workload if you specifically need to measure 1,000 users opening the app at nearly the same instant.
