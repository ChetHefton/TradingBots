"""Trade log CSV for tax review, with possible wash sales flagged.

A sale at a loss is flagged when the same symbol was bought within 30 days
before or after the sale (other than the buy that opened the lot being sold).
This is a reminder to check, not tax advice: the IRS rule also covers
substantially identical securities and purchases in other accounts.
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from pathlib import Path

WASH_SALE_DAYS = 30


@dataclass
class Fill:
    fill_id: str
    date: date
    symbol: str
    side: str          # "buy" or "sell"
    qty: int
    price: float
    strategy: str
    reason: str
    lot_id: str = ""   # buy fill_id that opened the lot (sells only)
    realized_pnl: float | None = None
    possible_wash_sale: bool = False
    wash_sale_note: str = ""


def flag_wash_sales(fills: list[Fill]) -> list[Fill]:
    buys = [f for f in fills if f.side == "buy"]
    for f in fills:
        f.possible_wash_sale = False
        f.wash_sale_note = ""
    for sale in fills:
        if sale.side != "sell" or sale.realized_pnl is None or sale.realized_pnl >= 0:
            continue
        window_lo = sale.date - timedelta(days=WASH_SALE_DAYS)
        window_hi = sale.date + timedelta(days=WASH_SALE_DAYS)
        matches = [b for b in buys if b.symbol == sale.symbol and b.fill_id != sale.lot_id
                   and window_lo <= b.date <= window_hi]
        if matches:
            sale.possible_wash_sale = True
            sale.wash_sale_note = "loss sale; same symbol bought " + ", ".join(b.date.isoformat() for b in matches)
            for b in matches:
                b.possible_wash_sale = True
                b.wash_sale_note = f"bought within {WASH_SALE_DAYS} days of loss sale on {sale.date.isoformat()}"
    return fills


def write_trade_log(fills: list[Fill], path: Path) -> None:
    flag_wash_sales(fills)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Fill)])
        writer.writeheader()
        for fill in sorted(fills, key=lambda x: (x.date, x.fill_id)):
            row = asdict(fill)
            row["date"] = fill.date.isoformat()
            writer.writerow(row)


def append_fill(fill: Fill, path: Path) -> None:
    """Append one fill from the paper loop, then re-flag the whole file."""
    fills = read_trade_log(path) if path.exists() else []
    fills.append(fill)
    write_trade_log(fills, path)


def read_trade_log(path: Path) -> list[Fill]:
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out.append(Fill(
                fill_id=row["fill_id"], date=date.fromisoformat(row["date"]), symbol=row["symbol"],
                side=row["side"], qty=int(row["qty"]), price=float(row["price"]),
                strategy=row["strategy"], reason=row["reason"], lot_id=row["lot_id"],
                realized_pnl=float(row["realized_pnl"]) if row["realized_pnl"] else None,
            ))
    return out
