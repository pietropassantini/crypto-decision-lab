#!/usr/bin/env bash
set -uo pipefail

ROOT="/home/pietro/crypto-decision-lab"
REPORT_DIR="$ROOT/data/monitoring/forward"
PYTHON="$ROOT/.venv/bin/python"

mkdir -p "$REPORT_DIR"
cd "$ROOT" || exit 1

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="$REPORT_DIR/audit-${STAMP}-$$.json"

echo "CP08-07 MONITOR START=$STAMP"

"$PYTHON" -m crypto_decision_lab.forward_monitoring \
    --root "$ROOT/data/forward/BTCUSDT-1h-forward-v1" \
    --max-lag-hours 2 \
    --report "$REPORT"

RC=$?

if [ -f "$REPORT" ]; then
    TMP="$REPORT_DIR/.latest-audit-$$.tmp"
    if cp "$REPORT" "$TMP" && mv -f "$TMP" "$REPORT_DIR/latest-audit.json"; then
        echo "REPORT=$REPORT"
    else
        rm -f "$TMP"
        echo "ERROR: latest report update failed" >&2
        exit 1
    fi
else
    echo "ERROR: audit report missing" >&2
    exit 1
fi

echo "CP08-07 MONITOR EXIT=$RC"
exit "$RC"
