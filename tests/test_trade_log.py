from datetime import date

from bot.trade_log import Fill, flag_wash_sales, read_trade_log, write_trade_log


def fills():
    return [
        Fill("b1", date(2026, 1, 5), "AAPL", "buy", 10, 100.0, "s", "entry"),
        Fill("s1", date(2026, 2, 2), "AAPL", "sell", 10, 90.0, "s", "stop", lot_id="b1", realized_pnl=-100.0),
        Fill("b2", date(2026, 2, 20), "AAPL", "buy", 10, 92.0, "s", "entry"),         # 18 days later
        Fill("b3", date(2026, 1, 5), "MSFT", "buy", 5, 300.0, "s", "entry"),
        Fill("s3", date(2026, 2, 2), "MSFT", "sell", 5, 320.0, "s", "exit", lot_id="b3", realized_pnl=100.0),
        Fill("b4", date(2026, 2, 10), "MSFT", "buy", 5, 310.0, "s", "entry"),         # gain sale: no flag
        Fill("b5", date(2026, 1, 5), "JPM", "buy", 5, 200.0, "s", "entry"),
        Fill("s5", date(2026, 2, 2), "JPM", "sell", 5, 190.0, "s", "exit", lot_id="b5", realized_pnl=-50.0),
        Fill("b6", date(2026, 3, 10), "JPM", "buy", 5, 180.0, "s", "entry"),          # 36 days later
    ]


def test_loss_sale_and_rebuy_within_30_days_flagged():
    f = {x.fill_id: x for x in flag_wash_sales(fills())}
    assert f["s1"].possible_wash_sale and f["b2"].possible_wash_sale
    assert not f["b1"].possible_wash_sale  # the lot's own opening buy


def test_gain_sale_and_late_rebuy_not_flagged():
    f = {x.fill_id: x for x in flag_wash_sales(fills())}
    assert not f["s3"].possible_wash_sale and not f["b4"].possible_wash_sale
    assert not f["s5"].possible_wash_sale and not f["b6"].possible_wash_sale


def test_csv_round_trip(tmp_path):
    p = tmp_path / "log.csv"
    write_trade_log(fills(), p)
    back = read_trade_log(p)
    assert len(back) == 9 and "possible_wash_sale" in p.read_text().splitlines()[0]
