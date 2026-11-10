-- core schema. schema_migrations is created by the migration runner, not here.

-- stock can never go below 0, even if the code has a bug
CREATE TABLE products (
    sku              text    PRIMARY KEY,
    unit_price_cents integer NOT NULL CHECK (unit_price_cents > 0),
    available        integer NOT NULL CHECK (available >= 0)
);

-- one row per accepted order; the unique key stops duplicates across replicas
-- id is text (uuid4 made by the app) so a bad id in GET gives a clean 404
CREATE TABLE orders (
    id              text        PRIMARY KEY,
    customer_id     text        NOT NULL CHECK (char_length(customer_id) BETWEEN 1 AND 64),
    idempotency_key text        NOT NULL UNIQUE CHECK (char_length(idempotency_key) BETWEEN 8 AND 128),
    payload_hash    text        NOT NULL,
    status          text        NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
    total_cents     integer     NOT NULL CHECK (total_cents > 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- lines are already added together per SKU, so the primary key is (order, sku)
CREATE TABLE order_items (
    order_id text    NOT NULL REFERENCES orders (id),
    sku      text    NOT NULL REFERENCES products (sku),
    quantity integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (order_id, sku)
);

-- the queue (ADR-001, D4): one job per order
-- state goes pending -> claimed -> done, or back to pending on retry, or dead
CREATE TABLE jobs (
    id               bigserial   PRIMARY KEY,
    order_id         text        NOT NULL UNIQUE REFERENCES orders (id),
    state            text        NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'claimed', 'done', 'dead')),
    attempts         integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer     NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    next_run_at      timestamptz NOT NULL DEFAULT now(),
    locked_by        text,
    lease_expires_at timestamptz,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- helps the worker find due jobs and jobs whose lease ended
CREATE INDEX jobs_claimable_idx ON jobs (next_run_at) WHERE state IN ('pending', 'claimed');
