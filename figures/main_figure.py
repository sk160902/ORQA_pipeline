"""Main results figure for NeurIPS 2026 ORQA paper.

Three-panel dot-and-whisker plot. Each panel is one metric:
  1) Multiple-choice accuracy (mean over 3 seeds)
  2) Open-ended accuracy (LLM-judge majority vote)
  3) Wage-weighted multiple-choice accuracy

Inputs (override via env vars):
  ORQA_BANK_CSV  bank with per-seed model responses + correct_answer
                 (default: <repo-parent>/may6_bank.csv)
  ORQA_WAGE_XLSX BLS OEWS national_M2024_dl.xlsx
                 (default: <repo-parent>/oesm24nat/national_M2024_dl.xlsx)

Outputs land alongside this script in ORQA_pipeline/figures/.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import patheffects as pe

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent  # occupation_task/ when installed at ORQA_pipeline/figures/
ROOT = Path(os.environ.get("ORQA_DATA_DIR", DEFAULT_ROOT))
DATA = Path(os.environ.get("ORQA_BANK_CSV", ROOT / "may6_bank.csv"))
WAGE = Path(os.environ.get("ORQA_WAGE_XLSX", ROOT / "oesm24nat" / "national_M2024_dl.xlsx"))
OUT_DIR = HERE
OUT_DIR.mkdir(exist_ok=True)


MODELS = [
    ("gpt_5_4", "GPT-5.4"),
    ("gpt_5_3_chat_latest", "GPT-5.3"),
    ("gpt_5_2", "GPT-5.2"),
    ("o3", "o3"),
    ("gpt_4o", "GPT-4o"),
    ("gpt_4o_mini", "GPT-4o mini"),
    ("gpt_3_5_turbo", "GPT-3.5 Turbo"),
    ("claude_opus_4_6", "Claude Opus 4.6"),
    ("claude_sonnet_4_6", "Claude Sonnet 4.6"),
    ("claude_haiku_4_5", "Claude Haiku 4.5"),
    ("gemini_3_1_pro", "Gemini 3.1 Pro"),
    ("gemini_2_5_flash", "Gemini 2.5 Flash"),
    ("DeepSeek_V3", "DeepSeek V3"),
    ("llama_3_3_70B", "Llama 3.3 70B"),
    ("Qwen2_5_7B_Instruct_Turbo", "Qwen2.5 7B"),
]


# 8 occupations have no exact match in BLS OEWS national 2024. Mapped to
# closest SOC parent/peer.
WAGE_OVERRIDES = {
    "Biofuels Processing Technicians": "Chemical Plant and System Operators",
    "Environmental Economists": "Economists",
    "Environmental Restoration Planners":
        "Environmental Scientists and Specialists, Including Health",
    "Freight Forwarders": "Cargo and Freight Agents",
    "Mental Health Counselors":
        "Substance Abuse, Behavioral Disorder, and Mental Health Counselors",
    "Remote Sensing Scientists and Technologists":
        "Environmental Scientists and Geoscientists",
    "Substance Abuse and Behavioral Disorder Counselors":
        "Substance Abuse, Behavioral Disorder, and Mental Health Counselors",
    "Transportation Planners": "Urban and Regional Planners",
}


def build_wage_lookup(occupations: list[str]) -> dict[str, float]:
    wages = pd.read_excel(WAGE)
    a_mean = pd.to_numeric(wages["A_MEAN"], errors="coerce")
    h_mean = pd.to_numeric(wages["H_MEAN"], errors="coerce")
    # Hourly-only occupations (Actors, Musicians) have suppressed A_MEAN.
    # BLS convention: annualize at 2080 hrs/yr.
    wages["WAGE"] = a_mean.fillna(h_mean * 2080)
    wages = wages.dropna(subset=["WAGE"])
    wage_by_title = wages.groupby("OCC_TITLE")["WAGE"].mean().to_dict()
    out: dict[str, float] = {}
    for occ in occupations:
        title = WAGE_OVERRIDES.get(occ, occ)
        if title in wage_by_title:
            out[occ] = float(wage_by_title[title])
    missing = [o for o in occupations if o not in out]
    if missing:
        raise RuntimeError(f"unmapped occupations: {missing}")
    return out


def compute_mc(df: pd.DataFrame, model: str) -> np.ndarray:
    cols = [f"{model}_seed{i}" for i in range(3)]
    correct = df["correct_answer"].astype(str).values
    return np.stack(
        [df[c].astype(str).values == correct for c in cols], axis=1
    ).astype(float)


def compute_open(df: pd.DataFrame, model: str) -> np.ndarray:
    col = f"{model}_majority_vote"
    return (df[col].astype(str).str.upper() == "YES").astype(float).values


def metrics_with_se(df: pd.DataFrame, wage: np.ndarray) -> pd.DataFrame:
    rows = []
    n_q = len(df)
    w = wage / wage.sum()
    n_eff = 1.0 / (w ** 2).sum()
    for key, label in MODELS:
        seed_mat = compute_mc(df, key)
        per_q_mc = seed_mat.mean(axis=1)

        mc_mean = per_q_mc.mean()
        mc_se = per_q_mc.std(ddof=1) / math.sqrt(n_q)

        oe = compute_open(df, key)
        oe_mean = oe.mean()
        oe_se = oe.std(ddof=1) / math.sqrt(n_q)

        ww_mean = float((per_q_mc * w).sum())
        var = (w * (per_q_mc - ww_mean) ** 2).sum()
        ww_se = math.sqrt(var / n_eff)

        rows.append(dict(
            model=key, label=label,
            mc=mc_mean, mc_se=mc_se,
            oe=oe_mean, oe_se=oe_se,
            ww=ww_mean, ww_se=ww_se,
        ))
    return pd.DataFrame(rows)


# ---- Style ----------------------------------------------------------------
BG = "#FFFFFF"
TEXT = "#222222"
MUTED = "#7A7A7A"
GRID = "#E6E6E6"
RULE = "#111111"
DOT_EDGE = "#FFFFFF"
PALETTE = {
    "mc": "#2F5C8A",   # navy
    "oe": "#C0322B",   # Economist red
    "ww": "#5C7A30",   # olive
}
PANEL_TITLES = [
    ("mc", "Multiple choice"),
    ("oe", "Open ended"),
    ("ww", "Multiple choice, wage-weighted"),
]

plt.rcParams.update({
    "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 10,
    "axes.edgecolor": TEXT,
    "axes.labelcolor": TEXT,
    "xtick.color": TEXT,
    "ytick.color": TEXT,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
})


def draw_panel(ax, results, key, color, *, show_y, x_lo, x_hi):
    n = len(results)
    # `results` is sorted ascending by MC accuracy, so iloc[0] is the worst.
    # Plotting at y = arange(n) puts iloc[0] at y=0 (bottom of axis), and
    # the best model at y=n-1 (top). Leaderboard reads top-down from best.
    y = np.arange(n)

    val = results[key].values
    err = 1.96 * results[f"{key}_se"].values

    # Whisker
    ax.hlines(y, val - err, val + err,
              color=color, linewidth=1.2, zorder=3)
    # End caps
    cap = 0.07
    ax.vlines(val - err, y - cap, y + cap,
              color=color, linewidth=1.0, zorder=3)
    ax.vlines(val + err, y - cap, y + cap,
              color=color, linewidth=1.0, zorder=3)
    # Point flush with the whisker (no white edge), 25% smaller.
    ax.scatter(val, y, s=26, color=color,
               edgecolor="none", linewidth=0, zorder=4)

    # Numeric labels at the right edge
    for yi, v, e in zip(y, val, err):
        ax.text(v + e + 0.012, yi, f"{v*100:.1f}",
                va="center", ha="left",
                fontsize=8.5, color=TEXT, zorder=5)

    # x_lo coincides with the random=25% baseline, so the left spine itself
    # acts as the reference. Subtle vertical gridlines elsewhere.
    for x in (0.30, 0.40, 0.50, 0.60, 0.70):
        ax.axvline(x, color=GRID, linewidth=0.6, zorder=0)

    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(-0.7, n - 0.3)
    ax.set_yticks(np.arange(n))

    if show_y:
        ax.set_yticklabels(results["label"].values,
                           fontsize=10.5, color=TEXT)
    else:
        ax.set_yticklabels([])

    ax.set_xticks([0.25, 0.30, 0.40, 0.50, 0.60, 0.70])
    ax.set_xticklabels(["25", "30", "40", "50", "60", "70"],
                       fontsize=9, color=TEXT)

    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(TEXT)
    ax.spines["bottom"].set_linewidth(0.7)
    ax.tick_params(axis="x", length=3, width=0.7, color=TEXT, pad=4)
    ax.tick_params(axis="y", length=0, pad=6)
    ax.set_axisbelow(True)
    ax.set_facecolor(BG)


def render(results: pd.DataFrame, out_path: Path) -> None:
    # Sort by MC accuracy descending (best at top in display).
    results = (
        results.sort_values("mc", ascending=True)
        .reset_index(drop=True)
    )

    fig = plt.figure(figsize=(11.5, 7.6), dpi=200, facecolor=BG)
    gs = fig.add_gridspec(
        nrows=1, ncols=3,
        wspace=0.18,
        left=0.13, right=0.985,
        top=0.86, bottom=0.09,
    )
    axes = [fig.add_subplot(gs[0, i]) for i in range(3)]

    # Cut off below the 25% random baseline; leave headroom for labels.
    x_lo, x_hi = 0.25, 0.78

    for i, (key, title) in enumerate(PANEL_TITLES):
        draw_panel(
            axes[i], results, key,
            color=PALETTE[key],
            show_y=(i == 0),
            x_lo=x_lo, x_hi=x_hi,
        )
        # Panel header: small color block + label, Economist style.
        # Block sits in figure coords above the axis.
        bb = axes[i].get_position()
        block_x = bb.x0
        block_y = bb.y1 + 0.045
        fig.add_artist(plt.Rectangle(
            (block_x, block_y + 0.012),
            0.012, 0.018,
            transform=fig.transFigure,
            facecolor=PALETTE[key], edgecolor="none",
        ))
        fig.text(
            block_x + 0.018, block_y + 0.021,
            title,
            fontsize=11.5, color=TEXT, weight="bold",
            ha="left", va="center",
        )
        # Sub-label: % units
        fig.text(
            block_x + 0.018, block_y + 0.003,
            "% correct",
            fontsize=9.2, color=MUTED, ha="left", va="center",
        )


    # Bottom-axis label (units indicated above panels, so omit here).
    fig.savefig(out_path, dpi=300, bbox_inches="tight", facecolor=BG)
    fig.savefig(out_path.with_suffix(".pdf"),
                bbox_inches="tight", facecolor=BG)
    plt.close(fig)


def main() -> None:
    df = pd.read_csv(DATA)
    wage_lookup = build_wage_lookup(df["occupation"].unique().tolist())
    df["wage"] = df["occupation"].map(wage_lookup)
    assert df["wage"].notna().all()

    results = metrics_with_se(df, df["wage"].values)
    results.to_csv(OUT_DIR / "main_results.csv", index=False)

    render(results, OUT_DIR / "main_figure.png")
    print(results.to_string(index=False))


if __name__ == "__main__":
    main()
