# LEDs One — Stock Level Dashboard

A real-time inventory management dashboard for LEDs One e-commerce operations. Tracks stock levels, sales velocity, and remap suggestions across UK, US, and Germany Amazon marketplaces.

---

## Overview

This system syncs inventory and listing data from a remote MySQL database into a local PostgreSQL database every 20 minutes. A Flask web application reads from PostgreSQL and serves a real-time stock dashboard and remap engine to portfolio holders.

```
MySQL (remote 149.28.134.54:3307)
  └── db_sync.py (every 20 min via OpenClaw cron)
        └── PostgreSQL (local stock_level db)
              └── Flask app (port 8080)
                    ├── /                  Stock Dashboard
                    ├── /remap             Remap Engine
                    └── /product-detail-card  Product Detail
```

---

## Project Structure

```
/opt/openclaw/stock_level/
├── scripts/
│   ├── dashboard_server.py      Flask app — stock dashboard UI + API
│   ├── remap_server.py          Remap engine — OOS suggestions + product detail
│   ├── product_detail_card.py   Full product detail page (landscape view)
│   └── db_sync.py               MySQL → PostgreSQL sync (runs every 20 min)
├── config/
│   └── stock_dashboard.yaml     Database credentials + threshold config
└── logs/
    ├── db_sync.log
    └── stock_dashboard.log
```

---

## Features

### Stock Dashboard (`/`)
- Live stock levels per SKU across UK, US, Germany, All Markets
- Status classification: CRITICAL / LOW / HEALTHY / OVERSTOCKED / NO DATA
- Portfolio holder filtering with critical count badges
- Search by SKU or product name
- Auto-refresh every 20 minutes with server-based stale detection
- gzip compressed API responses (87% payload reduction)
- SessionStorage caching for instant page navigation

### Remap Engine (`/remap`)
- Shows OOS (out of stock) SKUs that need remapping
- Suggests best same-holder sibling SKU with available stock
- 3-level sibling matching priority:
  - P1: Same base SKU + same parts count (same product, different color)
  - P2: Different base SKU + same parts count (same bundle type)
  - P3: Any sibling with stock (last resort)
- Sibling suggestions are same-holder only (no cross-holder remaps)
- Location-specific (UK / US / Germany)

### Product Detail Card (`/product-detail-card`)
- Full landscape comparison of OOS product vs suggested replacement
- Images, bullet points, pricing, stock by location
- Amazon listing links per marketplace

---

## Status Classification Logic

```python
# Sales velocity waterfall
if sold_7d  >= 7: avg = sold_7d  / 7
if sold_14d >= 7: avg = sold_14d / 14
if sold_30d >= 7: avg = sold_30d / 30
else:             avg = 0  # NO DATA

# Status from days remaining
days = stock / avg
if avg == 0:      status = "NO DATA"
if days <= 7:     status = "CRITICAL"
if days <= 21:    status = "LOW"
if days <= 90:    status = "HEALTHY"
else:             status = "OVERSTOCKED"
```

---

## Database Architecture

### MySQL Source (149.28.134.54:3307)

| Database | Table | Description |
|---|---|---|
| centralizer | location_wise_inv_stock | Stock per SKU per location |
| listing_management | ebay_products | Product listings (Amazon/eBay/Shopify) |
| order_management | order_item_info | Order line items |
| order_management | ph_cate_products | Portfolio holder assignments |
| order_management | ph_categories | Holder category groups |

### PostgreSQL (local stock_level db)

| Table | Description |
|---|---|
| location_wise_inv_stock | Stock per SKU per location (synced) |
| orders | Orders last 30 days |
| order_item_info | Order line items last 30 days |
| ebay_products | Active listings — one row per effective_sku |
| ebay_product_titles | Site-specific titles (UK / US / Germany) |
| ph_mapping | SKU to holder name mapping |
| amazon_variants | Product family groupings for remap |
| dashboard_cache | Pre-computed status per SKU per location |
| dashboard_cache_staging | Zero-downtime swap staging table |
| product_bullet_points | Bullet points for product detail page |
| product_sub_images | Sub images for product detail page |

---

## effective_sku Pattern

Products in MySQL `ebay_products` can have `mapped_sku` (warehouse SKU) or only `sku` (listing SKU). The system uses `effective_sku` for all lookups:

```
effective_sku = mapped_sku  if mapped_sku exists and not empty
              = sku          if mapped_sku is NULL (fallback)
```

This is implemented as two separate MySQL queries (not COALESCE) for performance — MySQL cannot use indexes on computed expressions.

---

## Sync Architecture

Sync runs every 20 minutes via OpenClaw cron. Steps run sequentially:

| Step | Function | Time | Notes |
|---|---|---|---|
| 1 | sync_location_wise_inv_stock | 8–16s | 129,166 rows |
| 2 | sync_orders | 2–4s | Last 30 days |
| 3 | sync_order_item_info | 2–5s | Last 30 days |
| 4 | sync_ebay_products | 80–100s | 49,284 rows — 2 fast queries |
| 5 | sync_ebay_product_titles | 15–20s | 42,557 rows — 2 fast queries |
| 6 | sync_ph_mapping | 2–4s | 12,732 rows |
| 7 | sync_amazon_variants | 8–12s | 33,459 rows — 2 fast queries |
| 8 | **sync_dashboard_cache** | 2–4s | **Dashboard fresh at ~66s** |
| 9 | sync_bullet_points | 55–72s | Product detail only |
| 10 | sync_sub_images | 35–45s | Product detail only |
| **Total** | | **210–280s** | |

Dashboard and Remap pages are fully up to date after Step 8 (~66s). Steps 9–10 only affect the product detail card.

---

## Amazon Variants Parent SKU Selection

When a product appears in multiple Amazon families, the correct family is selected using this priority:

1. **Site rank** — UK beats Ireland/other sites (UK=0, US=1, Germany=2, Ireland=9)
2. **Most same-parts single-unit siblings** — family with most color variants of same product type wins. Multi-pack SKUs (2PK, 3PK, 5PK) are excluded from count
3. **Has color** — tiebreaker

This prevents Ireland listings from contaminating UK parent SKU groupings, and prevents bundle/multi-pack families from winning over the correct single-unit family.

---

## Remap Sibling Matching

The remap engine uses parts count (number of `+` segments in SKU) to match product types:

| OOS SKU | Parts | Matched with |
|---|---|---|
| `PLTEBC` | 1 | Other parts=1 siblings (same lamp, different color) |
| `PLTEBC+WCWYBM` | 2 | Other parts=2 siblings (lamp + accessory bundle) |
| `PLTEBC+WCWYBM+ICST64E27` | 3 | Other parts=3 siblings (lamp + accessory + bulb) |

**Same-holder rule** — suggested SKU must belong to the same portfolio holder as the OOS product. Cross-holder remaps are not permitted.

---

## Installation

### Requirements

```
Python 3.8+
PostgreSQL 13+
OpenClaw (for cron scheduling)
```

### Python Dependencies

```bash
pip install flask psycopg2-binary pymysql pyyaml
```

### Config File

```yaml
# /opt/openclaw/stock_level/config/stock_dashboard.yaml
postgres:
  host: localhost
  port: 5432
  dbname: stock_level
  user: digit_web
  password: your_password

mysql:
  host: 149.28.134.54
  port: 3307
  user: your_mysql_user
  password: your_mysql_password

stock_dashboard:
  days_critical_threshold: 7
  days_low_threshold: 21
  days_overstock_threshold: 90
```

### First Run

```bash
# Create PostgreSQL tables and run initial sync
/opt/openclaw/venv/bin/python3 /opt/openclaw/stock_level/scripts/db_sync.py

# Verify
PGPASSWORD='your_password' psql -U digit_web -d stock_level -h localhost -c "
SELECT location, COUNT(*) AS skus FROM dashboard_cache GROUP BY location;"
```

### Start Dashboard

```bash
sudo systemctl start stock-dashboard-web
```

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/stock?marketplace=UK` | SKU table data (gzip compressed) |
| `GET /api/all-summary?marketplace=UK` | Status counts + holder list + sync timestamp |
| `GET /api/remap-suggestions?location=UK` | OOS SKUs with sibling suggestions |
| `GET /api/remap-summary` | Critical counts per location (lightweight) |
| `GET /api/product-detail?sku=X&location=UK` | Full product detail from PostgreSQL |

---

## Manual Operations

```bash
# Manual sync
time /opt/openclaw/venv/bin/python3 /opt/openclaw/stock_level/scripts/db_sync.py

# Restart dashboard
sudo systemctl restart stock-dashboard-web

# Check sync logs
tail -f /opt/openclaw/stock_level/logs/db_sync.log

# Full re-sync (schema changes)
PGPASSWORD='your_password' psql -U digit_web -d stock_level -h localhost \
  -c "DROP TABLE IF EXISTS ebay_products; DROP TABLE IF EXISTS dashboard_cache;"
time /opt/openclaw/venv/bin/python3 /opt/openclaw/stock_level/scripts/db_sync.py
```

---

## OpenClaw Cron

```
ID:       dc75e7ef-b4a5-40cb-b36f-27a9c4eab984
Schedule: */20 * * * * @ Asia/Kolkata
Session:  main
Slack:    C0AV1DS5TR8
```

---

## SKU Counts (Current)

| Table | Rows |
|---|---|
| dashboard_cache | 22,928 (5,732 SKUs × 4 locations) |
| ebay_products | 49,284 |
| ph_mapping | 12,732 |
| amazon_variants | 33,459 |
| ebay_product_titles | 42,557 |

---

## Developer

**G. Sarujanan** — LEDs One Operations  
Project Code: `ospm`  
Server: `192.168.18.94:8080`
