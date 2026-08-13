from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from kazekoshi.schedule_lateness import MonthlyLatenessStat


LATENESS_CHART_USER_LIMIT = 20
_JAPANESE_FONT_CANDIDATES = (
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "IPAexGothic",
    "IPAGothic",
    "Yu Gothic",
    "Hiragino Sans",
)


def _member_display_name(guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    display_name = (
        getattr(member, "display_name", None)
        or getattr(member, "name", None)
        or f"User {user_id}"
    )
    normalized = " ".join(str(display_name).splitlines()).strip()
    normalized = normalized.replace("$", "＄")
    if len(normalized) > 24:
        normalized = normalized[:23] + "…"
    return normalized


def _find_japanese_font(font_manager):
    for family in _JAPANESE_FONT_CANDIDATES:
        try:
            path = font_manager.findfont(family, fallback_to_default=False)
        except ValueError:
            continue
        return font_manager.FontProperties(fname=path)
    return None


def build_lateness_chart(
    stats: list[MonthlyLatenessStat],
    *,
    guild,
    period_label: str,
) -> BytesIO:
    """合計遅刻時間の上位20人を横棒グラフのPNGとして返す。"""
    if not stats:
        raise ValueError("lateness chart requires at least one row")

    project_root = Path(__file__).resolve().parents[1]
    matplotlib_cache = project_root / "temp" / "matplotlib-cache"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache.resolve()))
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot as plt

    chart_stats = stats[:LATENESS_CHART_USER_LIMIT]
    japanese_font = _find_japanese_font(font_manager)
    if japanese_font is None:
        labels = [f"#{index}" for index in range(1, len(chart_stats) + 1)]
        title = f"Total lateness - {period_label}"
        x_label = "Total lateness (minutes)"
    else:
        labels = [
            _member_display_name(guild, stat.user_id)
            for stat in chart_stats
        ]
        title = f"ユーザー別 合計遅刻・欠席時間 — {period_label}"
        x_label = "合計時間（分）"

    totals_in_minutes = [stat.total_seconds / 60 for stat in chart_stats]
    figure_height = max(4.0, len(chart_stats) * 0.46 + 1.8)
    figure, axis = plt.subplots(figsize=(10, figure_height))
    try:
        positions = list(range(len(chart_stats)))
        bars = axis.barh(positions, totals_in_minutes, color="#f39c4a")
        axis.set_yticks(positions, labels)
        axis.invert_yaxis()
        axis.grid(axis="x", linestyle="--", alpha=0.3)
        axis.set_axisbelow(True)
        axis.spines[["top", "right"]].set_visible(False)
        if japanese_font is None:
            axis.set_title(title, pad=14)
            axis.set_xlabel(x_label)
        else:
            axis.set_title(title, pad=14, fontproperties=japanese_font)
            axis.set_xlabel(x_label, fontproperties=japanese_font)
            for label in axis.get_yticklabels():
                label.set_fontproperties(japanese_font)

        maximum_minutes = max(totals_in_minutes)
        axis.set_xlim(0, max(1, maximum_minutes * 1.18))
        for bar, minutes in zip(bars, totals_in_minutes):
            axis.text(
                bar.get_width() + max(1, maximum_minutes * 0.015),
                bar.get_y() + bar.get_height() / 2,
                f"{minutes:.1f}",
                va="center",
                fontsize=9,
            )
        figure.tight_layout()
        image = BytesIO()
        figure.savefig(
            image,
            format="png",
            dpi=150,
            bbox_inches="tight",
            facecolor="white",
        )
        image.seek(0)
        return image
    finally:
        plt.close(figure)
