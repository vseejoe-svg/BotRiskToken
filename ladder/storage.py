# ladder/storage.py
from __future__ import annotations
import csv, os, math, datetime as dt
from typing import Tuple, Dict

CSV_PATH_DEFAULT = os.environ.get("TRADES_LOG_PATH", "trades_log.csv")

def _read_rows(csv_path: str) -> list[dict]:
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        dr = csv.DictReader(f)
        return list(dr)

def _get_col(row: dict, *names: str, default=None):
    # Case-insensitive Spaltenauflösung mit Fallback
    for n in names:
        for k, v in row.items():
            if k.lower() == n.lower():
                return v
    return default

def _parse_ts(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        # Unix (ms oder s)
        iv = int(float(v))
        if iv > 10_000_000_000:  # ms
            iv //= 1000
        return iv
    except Exception:
        pass
    # ISO
    try:
        return int(dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None

def day_agg_usd(csv_path: str = CSV_PATH_DEFAULT, tz: str = "UTC") -> Tuple[float, float]:
    """
    Summe Realized PnL des aktuellen Kalendertags + approximiertes Tages-MDD.
    Erwartete Spalten (mind. eine der Varianten):
      - Zeit: 'timestamp' | 'ts' | 'time'
      - PnL:  'realized_usd' | 'pnl_usd' | 'pnl'
    """
    rows = _read_rows(csv_path)
    if not rows:
        return 0.0, 0.0

    tzinfo = dt.timezone.utc if tz.upper() == "UTC" else dt.datetime.now().astimezone().tzinfo
    now = dt.datetime.now(tzinfo)
    start = int(dt.datetime(now.year, now.month, now.day, tzinfo=tzinfo).timestamp())
    end   = start + 86400

    # über Tag sortiert kumulieren
    day_vals = []
    for r in rows:
        ts = _parse_ts(_get_col(r, "timestamp", "ts", "time"))
        if ts is None or not (start <= ts < end):
            continue
        pnl = _get_col(r, "realized_usd", "pnl_usd", "pnl", default="0")
        try:
            pnl = float(str(pnl).replace(",", "."))
        except Exception:
            pnl = 0.0
        day_vals.append((ts, pnl))

    if not day_vals:
        return 0.0, 0.0

    day_vals.sort(key=lambda x: x[0])
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for _, pnl in day_vals:
        cum += pnl
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)

    return round(sum(v for _, v in day_vals), 6), round(mdd, 6)

def open_positions_count_from_csv(csv_path: str = CSV_PATH_DEFAULT) -> int:
    """
    Zählt offene Positionen je 'mint'/Symbol über Netto-Stückzahl (Buys - Sells).
    Erwartete Spalten:
      - 'mint' | 'symbol' | 'asset'
      - 'side' in {BUY/SELL} oder {LONG/SHORT}
      - 'qty' | 'quantity' | 'size'
    Falls Spalten fehlen oder Datei nicht existiert, wird 0 zurückgegeben.
    """
    rows = _read_rows(csv_path)
    if not rows:
        return 0

    nets: Dict[str, float] = {}
    for r in rows:
        mint = _get_col(r, "mint", "symbol", "asset")
        side = str(_get_col(r, "side", "action", default="")).upper()
        qty  = _get_col(r, "qty", "quantity", "size", default="0")
        try:
            q = float(str(qty).replace(",", "."))
        except Exception:
            q = 0.0
        if not mint:
            # ohne Kennung nicht zählbar
            continue
        if side in ("BUY", "LONG", "OPEN"):
            nets[mint] = nets.get(mint, 0.0) + q
        elif side in ("SELL", "SHORT", "CLOSE"):
            nets[mint] = nets.get(mint, 0.0) - q

    # Position gilt als offen, wenn Netto-Stückzahl > 0 (kleines Epsilon)
    open_cnt = sum(1 for v in nets.values() if v > 1e-9)
    return int(open_cnt)
