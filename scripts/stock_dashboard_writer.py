#!/usr/bin/env python3
"""
LEDs One — stock_dashboard_writer.py
Reads PostgreSQL → calculates stock status → writes Google Sheets.

Deploy to: /opt/openclaw/stock_level/scripts/stock_dashboard_writer.py
Config:     /opt/openclaw/stock_level/config/stock_dashboard.yaml
Logs:       /opt/openclaw/stock_level/logs/stock_dashboard.log

FIXED:
- Uses location_wise_inv_stock (correct table) not inv_final_stock
- PH mapping from ph_mapping table (synced from MySQL) not Excel
- Product info from ebay_products only (deduped, no shopify join)
- Skips UNASSIGNED holders to match leader dashboard behaviour
- order_item_info uses correct column names (synced via fixed db_sync.py)

Usage:
  python3 stock_dashboard_writer.py --config /opt/openclaw/stock_level/config/stock_dashboard.yaml
  python3 stock_dashboard_writer.py --config /opt/openclaw/stock_level/config/stock_dashboard.yaml --dry-run
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime
from typing import Dict, List, Tuple

import psycopg2
import psycopg2.extras
import yaml

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_OK = True
except ImportError:
    GSPREAD_OK = False

LOG_PATH = "/opt/openclaw/stock_level/logs/stock_dashboard.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dashboard_writer] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("dashboard_writer")

# ── Status constants ──────────────────────────────────────────────────────────
STATUS_CRITICAL  = "CRITICAL"
STATUS_LOW       = "LOW"
STATUS_HEALTHY   = "HEALTHY"
STATUS_OVERSTOCK = "OVERSTOCKED"
STATUS_NO_DATA   = "NO DATA"

COLOUR_MAP = {
    STATUS_CRITICAL:  {"red": 0.96, "green": 0.80, "blue": 0.80},
    STATUS_LOW:       {"red": 1.00, "green": 0.90, "blue": 0.70},
    STATUS_HEALTHY:   {"red": 0.82, "green": 0.96, "blue": 0.82},
    STATUS_OVERSTOCK: {"red": 0.80, "green": 0.90, "blue": 1.00},
    STATUS_NO_DATA:   {"red": 0.93, "green": 0.93, "blue": 0.93},
}

HEADERS = [
    "Date", "SKU", "Product Name", "Platform", "Item ID",
    "Stock", "Inbound", "Avg/Day", "Days Remaining", "Status",
    "Reorder Point", "Action", "Holder",
]

# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

def pg_conn(cfg: dict):
    pg = cfg["postgres"]
    args = {
        "host":   pg.get("host", "localhost"),
        "port":   pg.get("port", 5432),
        "dbname": pg["dbname"],
        "user":   pg["user"],
    }
    if pg.get("password"):
        args["password"] = pg["password"]
    return psycopg2.connect(**args)

# ── Main SQL — FIXED ──────────────────────────────────────────────────────────
# Uses location_wise_inv_stock (correct) and ph_mapping table (from MySQL sync)
# No shopify join — product info from ebay_products only, already deduped by db_sync

MAIN_QUERY = """
WITH
-- CTE 1: Total stock per SKU across all locations
stock_agg AS (
    SELECT sku,
           SUM(stock) AS total_stock
    FROM   location_wise_inv_stock
    GROUP  BY sku
),

-- CTE 2: Sales velocity — 7d / 14d / 30d windows
velocity AS (
    SELECT
        oii.sku,
        COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '7 days'
                          THEN oii.quantity END), 0) AS sold_7d,
        COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '14 days'
                          THEN oii.quantity END), 0) AS sold_14d,
        COALESCE(SUM(oii.quantity), 0)                AS sold_30d
    FROM   order_item_info oii
    JOIN   orders          o   ON o.internal_id = oii.order_id
    GROUP  BY oii.sku
),

-- CTE 3: Product info — from ebay_products only (already deduped by db_sync)
product_info AS (
    SELECT mapped_sku AS sku,
           title      AS product_name,
           which_channel AS platform,
           item_id
    FROM   ebay_products
    WHERE  mapped_sku IS NOT NULL
),

-- CTE 4: Holder mapping (synced from MySQL ph_cate_products by db_sync)
holders AS (
    SELECT mapped_sku, holder_name
    FROM   ph_mapping
),

-- CTE 5: Manual overrides
extras AS (
    SELECT sku, inbound_units, reorder_point
    FROM   sku_extras
)

SELECT
    sa.sku,
    COALESCE(pi.product_name,  '')  AS product_name,
    COALESCE(pi.platform,      '')  AS platform,
    COALESCE(pi.item_id,       '')  AS item_id,
    COALESCE(sa.total_stock,   0)   AS stock,
    COALESCE(ex.inbound_units, 0)   AS inbound,
    COALESCE(v.sold_7d,        0)   AS sold_7d,
    COALESCE(v.sold_14d,       0)   AS sold_14d,
    COALESCE(v.sold_30d,       0)   AS sold_30d,
    COALESCE(ex.reorder_point, 0)   AS reorder_point,
    COALESCE(h.holder_name, 'UNASSIGNED') AS holder
FROM       stock_agg   sa
LEFT JOIN  velocity    v   ON v.sku        = sa.sku
LEFT JOIN  product_info pi ON pi.sku       = sa.sku
LEFT JOIN  holders     h   ON h.mapped_sku = sa.sku
LEFT JOIN  extras      ex  ON ex.sku       = sa.sku
-- Only include SKUs that have a listing AND an assigned holder
WHERE pi.sku       IS NOT NULL
  AND h.mapped_sku IS NOT NULL
  AND h.holder_name != 'UNASSIGNED'
ORDER BY sa.sku
"""

# ── Classification ────────────────────────────────────────────────────────────

def classify(row: dict, cfg: dict) -> dict:
    thresholds = cfg.get("stock_dashboard", {})
    crit     = thresholds.get("days_critical_threshold",  7)
    low      = thresholds.get("days_low_threshold",       21)
    over     = thresholds.get("days_overstock_threshold", 90)
    max_name = thresholds.get("max_product_name_length",  60)

    s7, s14, s30 = row["sold_7d"], row["sold_14d"], row["sold_30d"]
    stock        = row["stock"]

    # Velocity waterfall
    if s7  >= 7: avg = round(s7  / 7,  2)
    elif s14 >= 7: avg = round(s14 / 14, 2)
    elif s30 >= 7: avg = round(s30 / 30, 2)
    else:          avg = 0.0

    days = int(stock / avg) if avg > 0 else None

    if avg == 0 or days is None:
        status = STATUS_NO_DATA
        action = "Check sales data feed for this SKU"
    elif days <= crit:
        status = STATUS_CRITICAL
        action = "Alert inventory team immediately"
    elif days <= low:
        status = STATUS_LOW
        action = "Raise reorder request within 48 hours"
    elif days <= over:
        status = STATUS_HEALTHY
        action = "No action required"
    else:
        status = STATUS_OVERSTOCK
        action = "Consider promotions or price reduction"

    pname = row["product_name"][:max_name] if row["product_name"] else ""

    return {
        **row,
        "product_name":   pname,
        "avg_per_day":    avg,
        "days_remaining": days if days is not None else "—",
        "status":         status,
        "action":         action,
    }

# ── Sheet helpers ─────────────────────────────────────────────────────────────

def build_row(r: dict, today_str: str) -> list:
    return [
        today_str,
        r["sku"],
        r["product_name"],
        r["platform"],
        r["item_id"],
        r["stock"],
        r["inbound"],
        r["avg_per_day"],
        r["days_remaining"],
        r["status"],
        r["reorder_point"],
        r["action"],
        r["holder"],
    ]

def get_gc(creds_path: str):
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    return gspread.authorize(creds)

def colour_for(status: str) -> dict:
    return COLOUR_MAP.get(status, COLOUR_MAP[STATUS_NO_DATA])

def write_tab(ws, rows_data: List[list], statuses: List[str]):
    """Clear tab, write header + data, apply colour formatting."""
    ws.clear()
    all_rows = [HEADERS] + rows_data
    ws.update("A1", all_rows, value_input_option="USER_ENTERED")

    requests = []
    sheet_id = ws._properties["sheetId"]

    # Header row
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId":          sheet_id,
                "startRowIndex":    0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": len(HEADERS),
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
                    "textFormat": {
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        "bold": True,
                    },
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    })

    # Data rows — colour by status
    for i, status in enumerate(statuses):
        bg = colour_for(status)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId":          sheet_id,
                    "startRowIndex":    i + 1, "endRowIndex": i + 2,
                    "startColumnIndex": 0,     "endColumnIndex": len(HEADERS),
                },
                "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    if requests:
        ws.spreadsheet.batch_update({"requests": requests})

def write_to_sheets(
    gc,
    sheet_id: str,
    holder_data: Dict[str, Tuple[List[list], List[str]]],
    master_rows: List[list],
    master_statuses: List[str],
    dry_run: bool,
):
    if dry_run:
        log.info("[DRY RUN] Would write to Google Sheets — skipping.")
        return

    sh = gc.open_by_key(sheet_id)
    existing_tabs = {ws.title: ws for ws in sh.worksheets()}

    for holder, (rows_data, statuses) in holder_data.items():
        log.info(f"  Writing tab: {holder} ({len(rows_data)} SKUs) ...")
        ws = existing_tabs.get(holder)
        if ws is None:
            ws = sh.add_worksheet(title=holder, rows=max(len(rows_data) + 5, 100), cols=len(HEADERS))
        try:
            write_tab(ws, rows_data, statuses)
        except Exception as e:
            log.warning(f"  Tab {holder} error: {e} — retrying ...")
            time.sleep(90)
            write_tab(ws, rows_data, statuses)
        time.sleep(3)

    log.info(f"  Writing MASTER tab ({len(master_rows)} SKUs) ...")
    ws = existing_tabs.get("MASTER")
    if ws is None:
        ws = sh.add_worksheet(title="MASTER", rows=max(len(master_rows) + 5, 100), cols=len(HEADERS))
    write_tab(ws, master_rows, master_statuses)
    log.info("Google Sheets write complete.")

def summarise(all_rows: List[dict]) -> dict:
    counts = {
        STATUS_CRITICAL:  0,
        STATUS_LOW:       0,
        STATUS_HEALTHY:   0,
        STATUS_OVERSTOCK: 0,
        STATUS_NO_DATA:   0,
    }
    critical_skus = []
    for r in all_rows:
        st = r["status"]
        counts[st] = counts.get(st, 0) + 1
        if st == STATUS_CRITICAL:
            critical_skus.append(r["sku"])
    return {"counts": counts, "critical_skus": critical_skus[:10]}

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LEDs One Stock Dashboard Writer")
    parser.add_argument("--config",  required=True, help="Path to stock_dashboard.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Skip Google Sheets write")
    args = parser.parse_args()

    start     = time.time()
    today_str = date.today().strftime("%d/%m/%Y")
    log.info("=" * 60)
    log.info(f"stock_dashboard_writer starting — {datetime.utcnow().isoformat()}Z")

    cfg        = load_config(args.config)
    gs_cfg     = cfg.get("google_sheets", {})
    sheet_id   = gs_cfg.get("stock_dashboard_id")
    creds_path = gs_cfg.get("credentials_path", "/opt/openclaw/stock_level/config/google_service_account.json")

    # ── Query PostgreSQL ──────────────────────────────────────────────────────
    log.info("Querying PostgreSQL ...")
    pg = pg_conn(cfg)
    with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(MAIN_QUERY)
        raw_rows = cur.fetchall()
    pg.close()
    log.info(f"Fetched {len(raw_rows):,} SKUs from PostgreSQL.")

    # ── Classify ──────────────────────────────────────────────────────────────
    all_enriched: List[dict] = [classify(dict(row), cfg) for row in raw_rows]

    # ── Group by holder ───────────────────────────────────────────────────────
    holder_groups: Dict[str, list] = {}
    for r in all_enriched:
        holder_groups.setdefault(r["holder"], []).append(r)

    log.info(f"Holders: {sorted(holder_groups.keys())}")

    # ── Build sheet data ──────────────────────────────────────────────────────
    holder_data: Dict[str, Tuple[List[list], List[str]]] = {}
    master_rows:     List[list] = []
    master_statuses: List[str]  = []

    for holder, rows in sorted(holder_groups.items()):
        tab_rows     = [build_row(r, today_str) for r in rows]
        tab_statuses = [r["status"] for r in rows]
        holder_data[holder] = (tab_rows, tab_statuses)
        master_rows.extend(tab_rows)
        master_statuses.extend(tab_statuses)

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = summarise(all_enriched)
    counts  = summary["counts"]
    log.info(
        f"Summary → CRITICAL:{counts[STATUS_CRITICAL]}  LOW:{counts[STATUS_LOW]}  "
        f"HEALTHY:{counts[STATUS_HEALTHY]}  OVERSTOCKED:{counts[STATUS_OVERSTOCK]}  "
        f"NO DATA:{counts[STATUS_NO_DATA]}"
    )

    # ── Write Google Sheets ───────────────────────────────────────────────────
    if not GSPREAD_OK:
        log.error("gspread / google-auth not installed. Cannot write to Sheets.")
    elif args.dry_run:
        log.info("[DRY RUN] Sheet write skipped.")
    else:
        gc = get_gc(creds_path)
        write_to_sheets(gc, sheet_id, holder_data, master_rows, master_statuses, dry_run=False)

    elapsed = time.time() - start
    log.info(f"stock_dashboard_writer SUCCESS — {elapsed:.1f}s total")

    # ── Slack alert ───────────────────────────────────────────────────────────
    try:
        slack_msg = (
            f"*Stock Dashboard Alert — {today_str}*\n"
            f"🔴 CRITICAL: {counts[STATUS_CRITICAL]} SKUs need immediate attention\n"
            f"🟠 LOW: {counts[STATUS_LOW]} SKUs need reorder within 48hrs\n"
            f"🟢 HEALTHY: {counts[STATUS_HEALTHY]} SKUs\n"
            f"📊 TOTAL: {len(all_enriched)} SKUs processed\n"
            f"🔗 Dashboard: http://192.168.18.94:8080"
        )
        if counts[STATUS_CRITICAL] > 0:
            top = ", ".join(summary["critical_skus"][:5])
            slack_msg += f"\n⚠️ Top critical: {top}"

        import subprocess
        result = subprocess.run([
            "openclaw", "message", "send",
            "--channel", "slack",
            "--target",  "C0AV1DS5TR8",
            "--message", slack_msg
        ], capture_output=True, text=True)
        if result.returncode == 0:
            log.info("Slack alert sent successfully.")
        else:
            log.warning(f"Slack alert failed: {result.stderr}")
    except Exception as e:
        log.warning(f"Slack alert error: {e}")


if __name__ == "__main__":
    main()