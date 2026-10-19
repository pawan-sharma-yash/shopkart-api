# Load testing

The Locust workload in `loadtest/locustfile.py` exercises product listing,
product details, and the home page. If `LOAD_TEST_TOKEN` is set, it also reads
the cart and order history using that access token. All requests are reads, so
the workload does not place orders or change catalog data.

## Run a 10,000-user load

Install Locust in a separate load-generator environment:

```bash
python -m pip install locust
```

Start the API with its production-like database and configuration. Then run
Locust from a separate machine so load generation does not compete with the API
for CPU and memory:

```bash
locust -f loadtest/locustfile.py \
  --host http://API_HOST:8080 \
  --headless \
  --users 10000 \
  --spawn-rate 500 \
  --run-time 10m \
  --csv loadtest-results/shopkart
```

Create the `loadtest-results` directory before the run. `--spawn-rate` ramps up
simulated users; adjust it to control how quickly the service is loaded. The
10,000 users remain active for the run and each issues a new request as soon as
its previous request completes. This models 10,000 concurrent clients; it is
not a perfectly synchronized, one-time burst of exactly 10,000 requests.

To include authenticated reads, provide a valid access token for a dedicated
load-test account:

```bash
LOAD_TEST_TOKEN=YOUR_TEST_ACCESS_TOKEN locust -f loadtest/locustfile.py \
  --host http://API_HOST:8080 --headless --users 10000 --spawn-rate 500 \
  --run-time 10m --csv loadtest-results/shopkart
```

The default product ID is `1`; set `LOAD_TEST_PRODUCT_ID` if the test database
uses another ID. A missing product returns 404 and will be counted as a failed
request.

Locust reports request rate, failures, and response-time percentiles in its
summary and CSV files. Decide acceptable failure rate and latency thresholds
before the run; this project does not currently define production SLOs. Start
with fewer users and increase gradually. A single machine may not be able to
generate 10,000 clients, in which case use Locust's distributed mode with
multiple load-generator workers. Do not aim this test at a production service
without a planned test window and monitoring, since it intentionally creates
heavy traffic.
