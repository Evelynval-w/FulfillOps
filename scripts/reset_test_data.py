#!/usr/bin/env python3
"""Reset the TEST database to its starting state: no orders, no jobs, seed stock.

Refuses to run unless APP_ENV=test, so it can never wipe a real deployment (threat model T6).

    export DATABASE_URL='postgresql://postgres:test@localhost:55432/fulfillops_test'
    APP_ENV=test python scripts/reset_test_data.py --yes

It keeps the schema and the migration history, so the API does not need to be restarted.
Stop the workers first, or they may be holding a job while the tables are emptied.
"""

import argparse
import os
import sys

import psycopg

# must match db/migrations/002_seed_catalog.sql (tests/test_reset_script.py checks this)
SEED_STOCK = {"KIT-100": 7, "PART-200": 12, "PACK-300": 5}


def reset(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("TRUNCATE jobs, order_items, orders")
        for sku, available in SEED_STOCK.items():
            conn.execute("UPDATE products SET available = %s WHERE sku = %s", (available, sku))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="confirm that data may be deleted")
    args = parser.parse_args()

    if os.environ.get("APP_ENV") != "test":
        print("Refusing to run: APP_ENV must be 'test' (this script deletes all orders).")
        return 1
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("Set DATABASE_URL to the test database first.")
        return 1
    if not args.yes:
        print("This deletes all orders and jobs and restores the seed stock. Add --yes to confirm.")
        return 1
    with psycopg.connect(url) as conn:
        reset(conn)
    print("Test data reset: no orders, no jobs, seed stock restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
