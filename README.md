# TradingBots — phase 1

A rule-based stock trading bot, built as a learning project. Phase 1 has three parts: a risk engine, a backtester and a **paper-trading** loop on Alpaca. There is no live trading, no AI, no news data and no intraday strategies.

> This is an educational project, not financial advice. Most active traders lose money. Expect any edge to be smaller than the backtest shows.

## How it fits together

```
config/settings.yaml ──► bot/config.py  (refuses live_trading: true)
                               │
bot/strategies/*  ──signals──► bot/market_filter.py ──► bot/risk.py ──► bot/orders.py ──► bot/broker.py ──► Alpaca PAPER
 (momentum, faber, rsi2)       (SPY > 200-day SMA       (sizes or      (idempotent IDs,   (paper=True,
                                for new buys)            rejects)       check-then-retry)   no URL override)

bot/backtest/engine.py runs the SAME strategy + filter + risk code against historical bars.
bot/trader.py          runs it against the paper broker (scripts/run_paper.py).
```

- **Strategies** only see bars up to the last completed close. They return signals (buy/sell, reason, inputs, stop distance). They never size orders or call the broker.
- **The risk engine** checks and sizes every order. No strategy can get around it.
- **Signals computed on a close execute at the next session.** In the backtest that's the next open. In paper trading it's the first cycle after 9:45 ET.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your paper keys
pytest                      # all tests should pass
```

### Getting Alpaca paper keys

1. Create a free account at <https://alpaca.markets>.
2. In the dashboard, switch to **Paper Trading** (top-left account switcher).
3. Under **API Keys**, generate a key pair. These are paper keys, separate from live keys.
4. Put them in `.env` as `ALPACA_PAPER_API_KEY` and `ALPACA_PAPER_SECRET_KEY`. `.env` is git-ignored. Never commit it.
5. Recommended: in the paper account settings, turn off margin (set max margin multiplier to 1) and disable shorting. The bot already sizes from cash and never shorts, so these settings are a second layer of protection.

Optional: set `ALERT_WEBHOOK_URL` to an [ntfy.sh](https://ntfy.sh) topic URL. Halts, errors and kill-switch trips are then pushed to your phone.

## Running the backtest

```bash
python -m scripts.backtest              # downloads daily bars from Alpaca (cached in data_cache/)
python -m scripts.backtest --refresh    # re-download
python -m scripts.backtest --synthetic  # random data: checks the plumbing, says nothing about markets
```

Output in `reports/`:

- `report.md`: comparison tables for the build period and the held-out test period, plus any kill-switch or halt events.
- `comparison_build.csv`, `comparison_test.csv`: the same tables as CSV.
- `sensitivity_momentum.csv`: momentum rebalanced on the 1st through 6th trading day of the month. Big swings between rows mean the result is timing luck.
- `equity_curves.png`: every strategy against buy-and-hold SPY.
- `trades_<period>_<strategy>.csv`: every fill, with possible wash sales flagged.

**Build vs. test.** Settle on the strategy using the build period only. Run the test period once, and don't change the strategy afterwards to make it look better. If you do, the test period stops being a test.

`--set key=value` overrides a config value for one run (e.g. `--set risk.max_position_pct=0.2`). Use it to understand what a limit does, not to search for a better-looking backtest.

## Paper trading

```bash
python -m scripts.run_paper --dry-run   # log what it would do, place nothing
python -m scripts.run_paper             # run continuously; trades once per day after 9:45 ET
python -m scripts.run_paper --once      # one cycle and exit (for cron, e.g. 10:00 ET on weekdays)
python -m scripts.run_paper --strategy rsi2
python -m scripts.run_paper --reset-kill-switch   # only after reviewing why it tripped
```

Each trading day the loop:

1. Reads the account and starts the risk day from the broker's previous-close equity.
2. Reconciles positions and open orders **from the broker**. The bot's own memory is never trusted over the broker:
   - Unexpected positions are adopted and alerted.
   - Positions that vanished (stopped out) are logged to the trade log.
   - Symbols not on the allowlist are left for you to handle.
3. Makes sure every position has a resting stop order. Alpaca GTC orders expire after 90 days, so this check matters for long holds.
4. Stops for the day if the daily-loss halt or the kill switch is active.
5. Runs the strategy on bars through yesterday's close, then applies the market filter.
6. Submits sells first (cancelling that symbol's stop first), then buys. Each buy is a market order with an attached stop (OTO).

Every five minutes during the session it re-checks equity against the limits, and re-checks that every position still has a stop.

Logs:

- `logs/decisions.jsonl`: one JSON line per decision, with inputs, risk-check results and reasons.
- `logs/trade_log.csv`: fills for tax review, with possible wash sales flagged.
- `state/bot_state.json`: peak equity, halts, the kill switch, and the entry dates of positions.

A dry run uses separate state and trade-log files, so it never affects the real ones.

## Dashboard

```bash
python -m scripts.dashboard              # http://127.0.0.1:8050
python -m scripts.dashboard --no-broker  # files only, no Alpaca calls
```

A green-phosphor terminal with scanlines, glow and Matrix rain. **FX: OFF** in the header turns the effects off, and they are skipped automatically when your OS asks for reduced motion. Tabs:

| Tab | Shows |
|---|---|
| STAT | Equity, today's loss against the daily halt, drawdown against the kill switch, orders used today, risk limits. Shows a banner when a halt or the kill switch is active. |
| INV | Positions with entry, current price, unrealized P/L, stop and the strategy that opened them. |
| DATA | Backtest equity curves (hover for values), scorecard vs. buy-and-hold SPY, and the rebalance-day sensitivity table for the build or test period. |
| TRADES | The paper trade log, with possible wash sales highlighted. |
| LOG | The decision feed from `logs/decisions.jsonl`, filterable. |

The dashboard is **read-only**:

- It listens on 127.0.0.1 only.
- It answers GET requests only.
- It has no order controls; a halted or killed bot can't be restarted from it.
- It reads the files the bot writes. With paper keys present, it also shows an account snapshot, cached for 30 seconds.

## Settings (`config/settings.yaml`)

### Risk (enforced in `bot/risk.py` for every order, in backtests and in paper trading)

| Setting | Default | What it does |
|---|---|---|
| `max_risk_per_trade_pct` | 0.01 | Shares = 1% of equity ÷ (entry − stop). A stopped-out trade loses about 1%. |
| `max_position_pct` | 0.10 | No position worth more than 10% of equity at entry. |
| `daily_loss_limit_pct` | 0.02 | Equity down 2% from the previous close: cancel pending orders (stops stay), place no new orders until the next trading day. |
| `max_drawdown_pct` | 0.10 | Equity down 10% from its peak: kill switch. Nothing trades until you run `--reset-kill-switch`. Stops stay in place. |
| `max_orders_per_day` | 20 | Catches runaway loops. Protective stops are counted but never blocked. |
| `skip_open_minutes` / `skip_close_minutes` | 15 / 15 | No orders in the first or last 15 minutes of the session. Uses Alpaca's calendar, so early closes are handled. |

Also enforced:

- Symbols on the allowlist only.
- Long only; never sell more than is held.
- Size from cash, never margin.
- No adding to an existing position.
- Every entry must have a stop below its price.

### Strategies (published defaults, do not tune)

| Strategy | Rule |
|---|---|
| `momentum` (default) | On the 1st trading day of each month, rank the allowlist by 12-month return excluding the latest month (252 and 21 trading days). Take the top 5. Hold those whose close is above their own 200-day SMA, weighted by inverse 20-day volatility (risk limits still apply). At rebalance, sell holdings that left that set. Hard stop at 2× the 14-day ATR. |
| `faber` | At month end, hold SPY if its close is above the average of the last 10 month-end closes, otherwise hold cash. |
| `rsi2` | Buy when the 2-day RSI closes below 10 and the close is above the 200-day SMA. Sell after a close above the 5-day SMA, or after 5 sessions. Hard stop at 2× ATR. |

The **market filter** (SPY close above its 200-day SMA) blocks new buys in every strategy. It never blocks sells.

`momentum.rebalance_offset` moves the trading day (0 = 1st trading day of the month). The sensitivity report varies it, so don't change it to chase a better result.

### Backtest

- `build_*` and `test_*` set the two periods. The defaults are build 2017-03 to 2021 and test 2022 to the latest data. Alpaca daily history starts around 2016, and the first ~14 months are warm-up.
- Costs: 0.05% slippage per side plus half of a 0.04% spread per side.
- Fills happen at the open of the session after the signal. Stops fill at the stop price, or at the open if the price gapped through it.

## Why not tune parameters

Published strategy returns drop by about half after publication, partly because the originals were overfit. Every parameter you adjust to improve the backtest fits a little more noise. The defaults here are the published ones. If a change looks justified, write down why *before* testing it, and check it on the build period only.

## Known limitations (phase 1)

- **Survivorship bias.** The default allowlist is SPY, QQQ and one sector ETF per sector, which mostly avoids it: ETFs don't go bust or drop out of an index. If you add individual stocks, pick them the way you would have *at the start* of the test period. A list of today's winners makes any backtest look great.
- **The 10% position cap dominates sizing.** Momentum can be at most ~50% invested (5 × 10%), and Faber holds at most 10% in SPY. Both will lag buy-and-hold SPY in a rising market by construction. Compare risk-adjusted numbers (Sharpe, drawdown), not just total return.
- **Backtests fill at the open; paper trading fills after 9:45.** The prices will differ somewhat.
- **Daily bars can't see intraday paths.** The daily-loss halt is checked at the open and the close in backtests, and every 5 minutes when paper trading.
- **Split and dividend-adjusted prices** are used for signals. Stops are computed in today's prices, where adjusted and raw prices match.
- **The wash-sale flag is a reminder, not tax advice.** It only checks the same symbol in this account.

## Roadmap

1. Paper trade phase 1 for 4–8 weeks. Compare paper fills and P&L with what the backtest would have done over the same weeks.
2. Phase 2: regime filter (VIX, volatility, earnings dates) as a brake, backtested against the plain version.
3. Phase 3: LLM news sentiment as a **veto-only** filter, tested only on news after the model's training cutoff.
