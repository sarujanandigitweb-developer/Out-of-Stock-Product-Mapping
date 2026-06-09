#!/bin/bash
echo "Starting DB sync at $(date)"
/opt/openclaw/venv/bin/python3 /opt/openclaw/stock_level/scripts/db_sync.py
echo "Starting dashboard writer at $(date)"
/opt/openclaw/venv/bin/python3 /opt/openclaw/stock_level/scripts/stock_dashboard_writer.py \
  --config /opt/openclaw/stock_level/config/stock_dashboard.yaml
echo "Done at $(date)"
