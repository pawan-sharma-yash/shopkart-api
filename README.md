# ShopKart API

ShopKart is a FastAPI service for authentication, product catalog browsing, carts, and wishlists. It uses SQLAlchemy for persistence and supports SQLite, PostgreSQL, and MySQL connection URLs.

## Architecture

```mermaid
flowchart LR
    Client[Web or mobile client] -->|HTTP / JSON| API[FastAPI application\nCORS middleware]
    API --> Routes[API v1 routers]
    Routes --> Auth[Authentication routes]
    Routes --> Catalog[Catalog and product routes]
    Routes --> Cart[Cart routes]
    Routes --> Wishlist[Wishlist routes]
    Routes --> Orders[Order placement and history routes]

    Auth --> Security[JWT, password, and OTP helpers]
    Auth --> Mail[Email service\nSMTP via FastMail]
    Auth --> DBSession[SQLAlchemy session dependency]
    Catalog --> DBSession
    Cart --> DBSession
    Wishlist --> DBSession
    Orders --> DBSession
    DBSession --> ORM[SQLAlchemy models]
    ORM --> Database[(SQLite / PostgreSQL / MySQL)]

    Startup[Application startup] --> ORM
    Startup --> CatalogSeed[Catalog seed and legacy variant migration]
    CatalogSeed --> Database
    CatalogSeed -. reads .-> ProductJSON[app/data/products.json]
    Catalog --> StaticCatalog[Categories and best-deal configuration]
    StaticCatalog -. reads .-> ProductJSON
```

### Main code areas

| Area | Responsibility |
| --- | --- |
| `app/main.py` | Loads configuration, creates tables, runs catalog migrations/seeding, configures CORS, and mounts the API router. |
| `app/api/v1/endpoints/` | HTTP endpoints for auth, catalog, products, cart, wishlist, and order operations. |
| `app/api/deps.py` | Shared authentication dependencies, including the current-user dependency. |
| `app/schemas/` | Pydantic request and response schemas. |
| `app/models/` | SQLAlchemy table models. |
| `app/db/session.py` | Database engine, session factory, and database compatibility helpers. |
| `app/core/security.py` | JWT and OTP creation, hashing, and expiry settings. |
| `app/services/` | Catalog serialization/seeding/migrations and email configuration. |

## Database tables and relationships

The diagram below documents the nine SQLAlchemy tables currently declared by the application. `users` and `products` are parent tables; saved addresses, cart and wishlist rows, refresh sessions, and orders belong to users. Orders contain item snapshots. Product variants are embedded as JSON in `products.variants`, so they are not a separate current table.

```mermaid
erDiagram
    USERS ||--o{ CART_ITEMS : owns
    PRODUCTS ||--o{ CART_ITEMS : appears_in
    USERS ||--o{ WISHLIST_ITEMS : owns
    PRODUCTS ||--o{ WISHLIST_ITEMS : appears_in
    USERS ||--o{ REFRESH_SESSIONS : has
    USERS ||--o{ ADDRESSES : saves
    USERS ||--o{ ORDERS : places
    ORDERS ||--|{ ORDER_ITEMS : contains
    PRODUCTS o|--o{ ORDER_ITEMS : referenced_by

    USERS {
        int id PK
        string email UK
        string full_name "nullable"
        string password_hash "nullable"
        int gender "nullable"
        string phone_number "nullable"
        string address "nullable"
        string pin "nullable"
        boolean is_active
        boolean profile_completed
        datetime created_at
    }
    OTP_CODES {
        int id PK
        string email "indexed; no FK"
        string code_hash
        datetime expires_at
        int attempts
        boolean is_used
    }
    REFRESH_SESSIONS {
        int id PK
        int user_id FK
        string token_id UK
        datetime expires_at
        boolean is_revoked
    }
    ADDRESSES {
        int id PK
        int user_id FK
        string label "nullable"
        string address
        string street "nullable"
        string city "nullable"
        string state "nullable"
        string pin "nullable"
        datetime created_at
    }
    ORDERS {
        int id PK
        int user_id FK
        string status
        decimal total_amount
        string shipping_address "nullable snapshot"
        string shipping_pin "nullable snapshot"
        datetime created_at
    }
    ORDER_ITEMS {
        int id PK
        int order_id FK
        int product_id FK "nullable"
        string product_name "snapshot"
        string variant_id
        json variant_snapshot
        decimal unit_price
        int quantity
        decimal line_total
    }
    PRODUCTS {
        int id PK
        string name "indexed"
        string description "nullable"
        string category "indexed"
        string subCategory "nullable"
        string brand "nullable; indexed"
        float rating "nullable"
        int reviews
        json offers
        json variants
        string badge "nullable"
    }
    CART_ITEMS {
        int id PK
        int user_id FK
        int product_id FK
        string variant_id
        int quantity
        string unique_user_product_variant "user_id + product_id + variant_id"
    }
    WISHLIST_ITEMS {
        int id PK
        int user_id FK
        int product_id FK
        string variant_id "nullable"
        string unique_user_product "user_id + product_id"
    }
```

### Relationship details

| Parent | Child | Cardinality | Foreign key / constraint |
| --- | --- | --- | --- |
| `users` | `cart_items` | One user to many cart rows | `cart_items.user_id → users.id`; deleting a user cascades to its cart rows. |
| `products` | `cart_items` | One product to many cart rows | `cart_items.product_id → products.id`; deleting a product cascades to its cart rows. |
| `users` | `wishlist_items` | One user to many wishlist rows | `wishlist_items.user_id → users.id`; no delete cascade is declared on this FK. |
| `products` | `wishlist_items` | One product to many wishlist rows | `wishlist_items.product_id → products.id`; deleting a product cascades to its wishlist rows. |
| `users` | `refresh_sessions` | One user to many refresh sessions | `refresh_sessions.user_id → users.id`; no delete cascade is declared on this FK. |
| `users` | `addresses` | One user to many saved addresses | `addresses.user_id → users.id`; deleting a user cascades to saved addresses. Registration creates a `Home` address, and existing legacy user addresses are copied into this table at startup when missing. |
| `users` | `orders` | One user to many orders | `orders.user_id → users.id`; order history is scoped to the authenticated user. |
| `orders` | `order_items` | One order to one or more line items | `order_items.order_id → orders.id`; deleting an order cascades to its items. |
| `products` | `order_items` | One product may be referenced by many order items | Nullable `order_items.product_id → products.id` with `ON DELETE SET NULL`; order item names, variant, and prices are snapshots. |
| — | `otp_codes` | No relational FK | OTP records are looked up by email; the schema does not link them to `users.id`. |

`cart_items` enforces one row per `(user_id, product_id, variant_id)`. `wishlist_items` enforces one row per `(user_id, product_id)`; its `variant_id` is optional metadata and does not change that uniqueness rule. Both `cart_items` and `wishlist_items` contain foreign keys to `users` and `products`, although only the product model declares an ORM relationship property.

### API routes

Routes are mounted directly on the FastAPI application (there is no `/api/v1` URL prefix). All routes below use the listed paths. Endpoints marked **Bearer** require an access token in `Authorization: Bearer <token>`.

| Area | Routes | Notes |
| --- | --- | --- |
| Authentication | `POST /auth/register`, `/auth/login`, `/auth/send-otp`, `/auth/verify-otp`, `/auth/create-profile`, `/auth/refresh`, `/auth/logout`; `GET /auth/me`; `PATCH /auth/Edit-profile` | Password registration/login and email OTP flows; token refresh/logout and profile operations. |
| Catalog | `GET /home`; `GET /products`; `GET /products/{product_id}` | Home content includes categories, best deals, and paginated products. Product listing supports search, category, brand, price, rating, sorting, and pagination filters. |
| Cart (**Bearer**) | `GET /cart`; `POST /cart/items`; `PATCH /cart/items/{item_id}`; `DELETE /cart/items/{item_id}` | Cart rows identify a product variant and enforce available stock. |
| Wishlist (**Bearer**) | `GET /wishlist`; `POST /wishlist`; `DELETE /wishlist/{product_id}` | One entry per product per user, with the selected variant retained as metadata. |
| Addresses (**Bearer**) | `GET /addresses`; `POST /addresses`; `PATCH /addresses/{address_id}`; `DELETE /addresses/{address_id}` | Saved addresses are scoped to the authenticated user; create accepts a full address or structured street, city, and state fields. |
| Orders (**Bearer**) | `POST /orders`; `GET /orders` | Placement validates the cart, address, selected variants, stock, and total in one transaction. History supports `page`, `pageSize`, and either `duration` (1–36 months) or `year`. |

The `products` API response uses the existing catalog JSON shape (`subCategory`, `parentId`, and related camelCase fields). List endpoints use `page` and `pageSize` query parameters where applicable; product/home pages allow up to 100 items per page.

### Product variants and legacy data

The `POST /orders` endpoint validates the authenticated user's delivery address and PIN, cart contents, selected variants, stock limits, and total before saving an order, decrementing the purchased stock, and clearing the cart in one transaction. `GET /orders` returns only that user's orders, newest first, with item snapshots and pagination (`page`, `pageSize`, maximum 100). It also accepts one date filter at a time:

- `duration=1` through `duration=36` for a rolling window ending now (UTC), where the value is a number of months.
- `year=2025` for orders placed during that calendar year (UTC).

For example: `GET /orders?page=1&pageSize=20&duration=6` or `GET /orders?year=2025`. Values outside the 1–36 month range or supplying both `duration` and `year` return HTTP 422. The total count and page count are calculated after applying the selected filter. Both endpoints require a valid access token in the `Authorization: Bearer <token>` header.

Current product variant data (including variant IDs, price, stock, and images) is stored inside the `products.variants` JSON column. The startup migration contains compatibility logic for a legacy `product_variants` table and copies its contents into that JSON column when appropriate. `product_variants` is not declared as a current SQLAlchemy model and is not part of the current entity relationship diagram.

## Running locally

Install dependencies from `requirements.txt`, set any required environment variables, then start the API with:

```bash
uvicorn app.main:app --reload
```

The default database URL is `sqlite:///./auth.db`. Set `DATABASE_URL` to use another supported database. The product catalog is loaded from `app/data/products.json` when the database has no products. Authentication and email settings are configured with environment variables; see `app/core/security.py` and `app/services/email.py` for the names and defaults.

## Concurrency

The API is tuned to serve 100 simultaneous requests. Three settings make that work,
and all of them are set automatically.

**SQLite uses `NullPool`.** SQLAlchemy 2.x gives file-backed SQLite a default
`QueuePool` of 5 connections plus 10 overflow. That silently caps the API at 15
concurrent database users: request 16 waits 30 seconds and then fails with
`TimeoutError`, which reaches the client as an HTTP 500. SQLite connections are
cheap and the database file is the shared resource, so a connection is opened per
request and SQLite arbitrates access with its own locks.

**SQLite runs in WAL mode.** The default rollback journal holds an exclusive
lock on the whole database while writing, so one write blocks every reader.
WAL mode (`PRAGMA journal_mode=WAL`) lets readers and the writer proceed at the
same time, and `busy_timeout` makes genuinely conflicting writers wait for the
lock instead of raising "database is locked". Both are applied to every new
connection, which matters because `NullPool` opens a fresh one per request.

**The sync-endpoint threadpool is raised to 100.** FastAPI runs `def` endpoints on
AnyIO's threadpool, which defaults to 40 tokens; requests past that limit queue
instead of running.

`sqlite:///:memory:` is the one exception. An in-memory database exists only inside
a single connection, so it uses `StaticPool` to keep its tables alive. Concurrent
sessions then interleave transactions on that one connection, so in-memory URLs are
suitable for tests but not for concurrent load.

Tune these with environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_THREAD_LIMIT` | `100` | Concurrent sync endpoints that may touch the database |
| `DB_POOL_SIZE` | `20` | PostgreSQL/MySQL pooled connections |
| `DB_MAX_OVERFLOW` | `20` | Extra PostgreSQL/MySQL connections above the pool size |
| `DB_POOL_TIMEOUT` | `30` | Seconds to wait for a connection before failing |
| `DB_POOL_RECYCLE` | `1800` | Seconds before a pooled connection is recycled |
| `DB_SQLITE_BUSY_TIMEOUT` | `30` | Seconds SQLite waits on a locked database |

`DB_POOL_SIZE` and `DB_MAX_OVERFLOW` apply to PostgreSQL and MySQL only; SQLite
ignores them. When running multiple workers, total connections are
`workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW)`, which must stay below the database's
`max_connections`.

Exhausting connections under load is a capacity condition rather than a fault, so
pool timeouts and SQLite lock errors are returned as **503 Service Unavailable**
with a `Retry-After` header rather than as 500. Clients can back off and retry.

## Load testing

The Locust workload and instructions for simulating up to 10,000 concurrent clients are in [LOAD_TESTING.md](LOAD_TESTING.md).
