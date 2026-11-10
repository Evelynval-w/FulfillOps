-- 002_seed_catalog.sql: the three fixed products and their starting stock (API contract).
-- ON CONFLICT DO NOTHING means a restart or a re-run never recreates stock that was spent.
INSERT INTO products (sku, unit_price_cents, available) VALUES
    ('KIT-100',  1299,  7),
    ('PART-200',  599, 12),
    ('PACK-300',  249,  5)
ON CONFLICT (sku) DO NOTHING;
