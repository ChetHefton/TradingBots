"""Comparison tables (Markdown and CSV) and the equity-curve chart."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from bot.backtest.engine import BacktestResult  # noqa: E402
from bot.backtest.metrics import compute  # noqa: E402

# Fixed color per series (by identity, never by rank). Benchmark is neutral ink.
SERIES_COLORS = {
    "momentum": "#2a78d6",
    "faber": "#eb6834",
    "rsi2": "#1baf7a",
    "buy & hold SPY": "#3d3d3a",
}
FALLBACK = ["#eda100", "#e87ba4", "#008300"]
INK, MUTED, GRID = "#1f1f1d", "#6b6b66", "#e4e3de"


def markdown_table(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    cols = list(rows[0])
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    lines += ["| " + " | ".join(r[c] for c in cols) + " |" for r in rows]
    return "\n".join(lines)


def comparison_rows(results: list[BacktestResult]) -> list[dict[str, str]]:
    return [compute(r).as_row() for r in results]


def save_table(rows: list[dict[str, str]], path_stem: Path) -> str:
    path_stem.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path_stem.with_suffix(".csv"), index=False)
    md = markdown_table(rows)
    path_stem.with_suffix(".md").write_text(md + "\n")
    return md


def save_equity_csv(periods: dict[str, list[BacktestResult]], path: Path) -> None:
    """Long-format equity curves (period, strategy, date, index) for the dashboard."""
    frames = []
    for label, results in periods.items():
        for r in results:
            curve = (r.equity / r.initial_capital * 100).round(3)
            frames.append(pd.DataFrame({"period": label, "strategy": r.name,
                                        "date": curve.index.strftime("%Y-%m-%d"), "index": curve.values}))
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(frames).to_csv(path, index=False)


def equity_chart(periods: dict[str, list[BacktestResult]], path: Path, title_note: str = "") -> None:
    """One panel per period; each curve indexed to 100 at the start."""
    fig, axes = plt.subplots(1, len(periods), figsize=(7.2 * len(periods), 5.0), squeeze=False)
    for ax, (label, results) in zip(axes[0], periods.items()):
        fallback = iter(FALLBACK)
        ends = []
        for r in results:
            color = SERIES_COLORS.get(r.name) or next(fallback)
            curve = r.equity / r.initial_capital * 100
            style = "--" if r.name.startswith("buy & hold") else "-"
            ax.plot(curve.index, curve.values, style, color=color, linewidth=1.6, label=r.name)
            ends.append([float(curve.iloc[-1]), f"{r.name} {curve.iloc[-1]:.0f}", curve.index[-1]])
        # End labels, nudged apart so they never overlap.
        lo, hi = ax.get_ylim()
        gap = (hi - lo) * 0.05
        ends.sort(key=lambda e: e[0])
        for i in range(1, len(ends)):
            ends[i][0] = max(ends[i][0], ends[i - 1][0] + gap)
        for y, text, x in ends:
            ax.annotate(text, (x, y), xytext=(6, 0), textcoords="offset points", fontsize=8,
                        color=INK, va="center", annotation_clip=False)
        ax.set_title(label, loc="left", fontsize=11, color=INK)
        ax.set_ylabel("Equity (start = 100)", fontsize=9, color=MUTED)
        ax.grid(True, color=GRID, linewidth=0.8)
        ax.tick_params(colors=MUTED, labelsize=8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.legend(fontsize=8, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.08),
                  ncol=len(results))
    if title_note:
        fig.suptitle(title_note, fontsize=9, color=MUTED, x=0.01, ha="left")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
