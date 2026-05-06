"""Heatmap figure: model x occupation MC accuracy.

Three vertically stacked panels showing the 15 occupations where models do
best, the 15 in the middle, and the 15 where models do worst. Occupations
with fewer than 3 questions are dropped.

Inputs (override via env vars):
  ORQA_BANK_CSV  bank with per-seed model responses + correct_answer
                 (default: <repo-parent>/may6_bank.csv)

Outputs land alongside this script in ORQA_pipeline/figures/.
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, Normalize

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent
ROOT = Path(os.environ.get("ORQA_DATA_DIR", DEFAULT_ROOT))
DATA = Path(os.environ.get("ORQA_BANK_CSV", ROOT / "may6_bank.csv"))
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
MIN_QUESTIONS = 3

# Display-only shortenings for occupation names longer than 25 chars.
# Keep them informative; aim for under ~30 chars when possible.
OCC_SHORT = {
    "Administrative Law Judges, Adjudicators, and Hearing Officers":
        "Admin. Law Judges & Adjudicators",
    "Biofuels Processing Technicians": "Biofuels Process. Technicians",
    "Bookkeeping, Accounting, and Auditing Clerks":
        "Bookkeeping & Auditing Clerks",
    "Bus and Truck Mechanics and Diesel Engine Specialists":
        "Bus & Truck/Diesel Mechanics",
    "Career/Technical Education Teachers, Middle School":
        "Career/Tech Teachers, Mid. School",
    "Cement Masons and Concrete Finishers": "Cement & Concrete Finishers",
    "Chemical Equipment Operators and Tenders":
        "Chemical Equipment Operators",
    "Clinical and Counseling Psychologists":
        "Clinical/Counseling Psychologists",
    "Cooks, Institution and Cafeteria": "Cooks, Cafeteria",
    "Correctional Officers and Jailers": "Correctional Officers",
    "Detectives and Criminal Investigators":
        "Detectives & Criminal Investigators",
    "Educational, Guidance, and Career Counselors and Advisors":
        "Educational & Career Counselors",
    "Electrical and Electronics Repairers, Commercial and Industrial Equipment":
        "Electrical/Electronics Repairers",
    "Emergency Management Directors": "Emergency Mgmt. Directors",
    "English Language and Literature Teachers, Postsecondary":
        "English Lang. & Lit. Profs.",
    "Environmental Restoration Planners": "Environmental Restoration Planners",
    "First-Line Supervisors of Construction Trades and Extraction Workers":
        "First-Line Construction Supervisors",
    "Heavy and Tractor-Trailer Truck Drivers": "Heavy Truck Drivers",
    "Industrial-Organizational Psychologists":
        "Industrial-Org. Psychologists",
    "Insurance Claims and Policy Processing Clerks":
        "Insurance Claims Clerks",
    "Janitors and Cleaners, Except Maids and Housekeeping Cleaners":
        "Janitors and Cleaners",
    "Judges, Magistrate Judges, and Magistrates":
        "Judges & Magistrates",
    "Kindergarten Teachers, Except Special Education":
        "Kindergarten Teachers",
    "Laborers and Freight, Stock, and Material Movers, Hand":
        "Freight & Material Movers, Hand",
    "Landscaping and Groundskeeping Workers": "Landscaping Workers",
    "Laundry and Dry-Cleaning Workers": "Laundry & Dry-Cleaning Workers",
    "Library Science Teachers, Postsecondary": "Library Science Profs.",
    "Maids and Housekeeping Cleaners": "Maids & Housekeeping Cleaners",
    "Maintenance and Repair Workers, General":
        "Maintenance & Repair Workers",
    "Manicurists and Pedicurists": "Manicurists & Pedicurists",
    "Marriage and Family Therapists": "Marriage & Family Therapists",
    "News Analysts, Reporters, and Journalists":
        "Reporters & Journalists",
    "Nursing Instructors and Teachers, Postsecondary":
        "Nursing Profs.",
    "Occupational Health and Safety Specialists":
        "Occupational Health & Safety",
    "Painters, Construction and Maintenance":
        "Painters, Construction",
    "Paralegals and Legal Assistants": "Paralegals & Legal Assistants",
    "Parking Enforcement Workers": "Parking Enforcement Workers",
    "Personal Financial Advisors": "Personal Financial Advisors",
    "Plumbers, Pipefitters, and Steamfitters":
        "Plumbers & Pipefitters",
    "Police and Sheriff's Patrol Officers": "Police & Patrol Officers",
    "Preschool Teachers, Except Special Education":
        "Preschool Teachers",
    "Public Safety Telecommunicators": "Public Safety Telecom.",
    "Remote Sensing Scientists and Technologists":
        "Remote Sensing Scientists",
    "Slaughterers and Meat Packers": "Slaughterers & Meat Packers",
    "Special Education Teachers, All Other": "Special Education Teachers",
    "Special Education Teachers, Preschool":
        "Special Ed. Teachers, Preschool",
    "Stockers and Order Fillers": "Stockers & Order Fillers",
    "Tire Repairers and Changers": "Tire Repairers & Changers",
    "Transportation Security Screeners": "Transp. Security Screeners",
    "Welders, Cutters, Solderers, and Brazers":
        "Welders, Cutters & Solderers",
}


def short_label(occ: str) -> str:
    if len(occ) <= 25:
        return occ
    return OCC_SHORT.get(occ, occ)


# ---- Style ----------------------------------------------------------------
BG = "#FFFFFF"
TEXT = "#222222"
MUTED = "#7A7A7A"
GRID = "#E6E6E6"
PANEL_LABEL_COLOR = "#222222"

# Diverging red to cream to blue, centered at 50%. Red signals
# below-chance-ish performance, blue signals strong performance; cream is
# the neutral middle. Colorblind-safe (matches Brewer RdBu family).
HEAT_CMAP = LinearSegmentedColormap.from_list(
    "econ_rdbu",
    [
        "#7B1A1A",  # 0   deep red
        "#B23A2C",  # 12.5
        "#D87060",  # 25
        "#EFC2A8",  # 37.5
        "#F4EEE3",  # 50  cream center
        "#BFD0DE",  # 62.5
        "#7FA1BD",  # 75
        "#3E6E97",  # 87.5
        "#163A60",  # 100 deep blue
    ],
    N=256,
)

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


def per_question_mc(df: pd.DataFrame, model_key: str) -> np.ndarray:
    cols = [f"{model_key}_seed{i}" for i in range(3)]
    correct = df["correct_answer"].astype(str).values
    seed_correct = np.stack(
        [df[c].astype(str).values == correct for c in cols], axis=1
    ).astype(float)
    return seed_correct.mean(axis=1)


def build_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """occupation x model accuracy matrix, restricted to occupations with
    at least MIN_QUESTIONS questions."""
    counts = df.groupby("occupation").size()
    keep = counts[counts >= MIN_QUESTIONS].index
    df = df[df["occupation"].isin(keep)].copy()

    rows = {}
    for occ, sub in df.groupby("occupation"):
        rows[occ] = {
            label: per_question_mc(sub, key).mean()
            for key, label in MODELS
        }
    mat = pd.DataFrame(rows).T  # occupations x models
    mat = mat[[label for _, label in MODELS]]  # column order
    return mat


def select_three_groups(mat: pd.DataFrame) -> tuple[pd.DataFrame, ...]:
    """Top 15, middle 15, bottom 15 occupations by mean accuracy across
    models. Each panel sorted descending so highest-accuracy row is on top."""
    overall = mat.mean(axis=1).sort_values(ascending=False)
    n = len(overall)

    top = overall.iloc[:15].index
    mid_lo = (n - 15) // 2
    middle = overall.iloc[mid_lo:mid_lo + 15].index
    bottom = overall.iloc[-15:].index

    return mat.loc[top], mat.loc[middle], mat.loc[bottom]


def draw_panel(ax, sub: pd.DataFrame, *, vmin, vmax, show_x):
    data = sub.values
    norm = Normalize(vmin=vmin, vmax=vmax)
    ax.imshow(
        data, aspect="auto", cmap=HEAT_CMAP, norm=norm,
        interpolation="nearest",
    )
    n_rows, n_cols = data.shape
    ax.set_xticks(np.arange(n_cols))
    ax.set_yticks(np.arange(n_rows))
    ax.set_yticklabels([short_label(o) for o in sub.index],
                       fontsize=9.2, color=TEXT)
    if show_x:
        ax.set_xticklabels([c for c in sub.columns],
                           fontsize=9.2, color=TEXT,
                           rotation=40, ha="right",
                           rotation_mode="anchor")
    else:
        ax.set_xticklabels([])

    # Cell annotations: white on the dark ends of the diverging scale,
    # dark on the cream middle.
    span = vmax - vmin
    lo_thr = vmin + 0.20 * span
    hi_thr = vmin + 0.80 * span
    for i in range(n_rows):
        for j in range(n_cols):
            v = data[i, j]
            dark_cell = (v <= lo_thr) or (v >= hi_thr)
            txt_color = "#FFFFFF" if dark_cell else "#1F1F1F"
            ax.text(j, i, f"{v*100:.0f}",
                    ha="center", va="center",
                    fontsize=8.0, color=txt_color)

    # Subtle white gridlines between cells.
    ax.set_xticks(np.arange(n_cols + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(n_rows + 1) - 0.5, minor=True)
    ax.grid(which="minor", color=BG, linewidth=1.4)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", axis="both", length=0, pad=4)
    for s in ax.spines.values():
        s.set_visible(False)


def render(top: pd.DataFrame, mid: pd.DataFrame, bot: pd.DataFrame,
           out_path: Path) -> None:
    vmin, vmax = 0.0, 1.0

    fig = plt.figure(figsize=(11.0, 13.5), dpi=200, facecolor=BG)
    gs_panels = fig.add_gridspec(
        nrows=3, ncols=1,
        hspace=0.18,
        left=0.215, right=0.985, top=0.965, bottom=0.07,
    )
    ax_top = fig.add_subplot(gs_panels[0])
    ax_mid = fig.add_subplot(gs_panels[1])
    ax_bot = fig.add_subplot(gs_panels[2])

    panels = [
        (ax_top, top, "Highest accuracy", "#2F5C8A", False),
        (ax_mid, mid, "Median accuracy", "#7A8C3C", False),
        (ax_bot, bot, "Lowest accuracy", "#C0322B", True),
    ]

    for ax, sub, title, accent, show_x in panels:
        draw_panel(ax, sub, vmin=vmin, vmax=vmax, show_x=show_x)
        # Panel header: small color tab + title, just above the axis.
        bb = ax.get_position()
        block_x = bb.x0
        block_y = bb.y1 + 0.012
        fig.add_artist(plt.Rectangle(
            (block_x, block_y),
            0.011, 0.013,
            transform=fig.transFigure,
            facecolor=accent, edgecolor="none",
        ))
        fig.text(
            block_x + 0.018, block_y + 0.006,
            title,
            fontsize=12.5, color=PANEL_LABEL_COLOR, weight="black",
            ha="left", va="center",
        )

    fig.savefig(out_path, dpi=300, bbox_inches="tight", facecolor=BG)
    fig.savefig(out_path.with_suffix(".pdf"),
                bbox_inches="tight", facecolor=BG)
    plt.close(fig)


def main() -> None:
    df = pd.read_csv(DATA)
    mat = build_matrix(df)
    top, mid, bot = select_three_groups(mat)
    mat.to_csv(OUT_DIR / "heatmap_full_matrix.csv")

    print(f"matrix: {mat.shape[0]} occupations x {mat.shape[1]} models")
    print(f"top range:    {top.mean(axis=1).min():.3f}  "
          f"to {top.mean(axis=1).max():.3f}")
    print(f"middle range: {mid.mean(axis=1).min():.3f}  "
          f"to {mid.mean(axis=1).max():.3f}")
    print(f"bottom range: {bot.mean(axis=1).min():.3f}  "
          f"to {bot.mean(axis=1).max():.3f}")

    render(top, mid, bot, OUT_DIR / "heatmap_figure.png")


if __name__ == "__main__":
    main()
