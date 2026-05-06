"""SOC major-group heatmap: model x occupation-group MC accuracy.

Aggregates all questions across each SOC major group. No question-count
filter (small samples are absorbed into the group mean). Y-axis annotates
the number of occupations from our data within each bucket.

Inputs (override via env vars):
  ORQA_BANK_CSV  bank with per-seed model responses + correct_answer
                 (default: <repo-parent>/may6_bank.csv)
  ORQA_WAGE_XLSX BLS OEWS national_M2024_dl.xlsx, used only for
                 occupation -> SOC code mapping
                 (default: <repo-parent>/oesm24nat/national_M2024_dl.xlsx)

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

OVERRIDES = {
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

# Short, chart-friendly major-group names. The official BLS titles are wordy
# (and end in "Occupations"); we trim aggressively for a cleaner y-axis.
SHORT_NAMES = {
    "11": "Management",
    "13": "Business & Financial",
    "15": "Computer & Mathematical",
    "17": "Architecture & Engineering",
    "19": "Life, Physical & Social Science",
    "21": "Community & Social Service",
    "23": "Legal",
    "25": "Education & Library",
    "27": "Arts, Design, Sports & Media",
    "29": "Healthcare Practitioners",
    "31": "Healthcare Support",
    "33": "Protective Service",
    "35": "Food Preparation & Serving",
    "37": "Building & Grounds Cleaning",
    "39": "Personal Care & Service",
    "41": "Sales",
    "43": "Office & Administrative",
    "45": "Farming, Fishing & Forestry",
    "47": "Construction & Extraction",
    "49": "Installation, Maintenance & Repair",
    "51": "Production",
    "53": "Transportation & Material Moving",
    "55": "Military",
}


# ---- Style ----------------------------------------------------------------
BG = "#FFFFFF"
TEXT = "#222222"
MUTED = "#7A7A7A"

HEAT_CMAP = LinearSegmentedColormap.from_list(
    "econ_rdbu",
    [
        "#7B1A1A", "#B23A2C", "#D87060", "#EFC2A8",
        "#F4EEE3",
        "#BFD0DE", "#7FA1BD", "#3E6E97", "#163A60",
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


# ---- Data plumbing ---------------------------------------------------------
def build_occ_to_code() -> dict[str, str]:
    wages = pd.read_excel(WAGE)
    sub = wages[wages["O_GROUP"].isin(["detailed", "broad", "minor"])]
    title_to_code = dict(
        sub.drop_duplicates("OCC_TITLE")[["OCC_TITLE", "OCC_CODE"]].values
    )
    return title_to_code


def per_question_mc(df: pd.DataFrame, model_key: str) -> np.ndarray:
    cols = [f"{model_key}_seed{i}" for i in range(3)]
    correct = df["correct_answer"].astype(str).values
    seed_correct = np.stack(
        [df[c].astype(str).values == correct for c in cols], axis=1
    ).astype(float)
    return seed_correct.mean(axis=1)


def build_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    title_to_code = build_occ_to_code()

    def occ_to_major(occ: str) -> str:
        title = OVERRIDES.get(occ, occ)
        code = title_to_code[title]
        return code.split("-")[0]

    df = df.copy()
    df["major"] = df["occupation"].map(occ_to_major)

    # Count distinct occupations per major group from the underlying data.
    occ_counts = (
        df.groupby("major")["occupation"].nunique().to_dict()
    )

    rows = {}
    for major, sub in df.groupby("major"):
        rows[major] = {
            label: per_question_mc(sub, key).mean()
            for key, label in MODELS
        }
    mat = pd.DataFrame(rows).T
    mat = mat[[label for _, label in MODELS]]

    # Sort by overall mean accuracy descending (best at top).
    mat = mat.assign(_mean=mat.mean(axis=1)) \
             .sort_values("_mean", ascending=False) \
             .drop(columns="_mean")
    return mat, occ_counts


def render(mat: pd.DataFrame, occ_counts: dict[str, int],
           out_path: Path) -> None:
    vmin, vmax = 0.0, 1.0
    n_rows, n_cols = mat.shape

    # Pretty row labels: "Healthcare Practitioners (n=8)".
    row_labels = [
        f"{SHORT_NAMES.get(code, code)}  (n={occ_counts.get(code, 0)})"
        for code in mat.index
    ]

    fig = plt.figure(figsize=(11.0, 8.4), dpi=200, facecolor=BG)
    ax = fig.add_axes([0.30, 0.10, 0.685, 0.85])

    norm = Normalize(vmin=vmin, vmax=vmax)
    ax.imshow(
        mat.values, aspect="auto", cmap=HEAT_CMAP, norm=norm,
        interpolation="nearest",
    )
    ax.set_xticks(np.arange(n_cols))
    ax.set_xticklabels([c for c in mat.columns],
                       fontsize=9.4, color=TEXT,
                       rotation=40, ha="right",
                       rotation_mode="anchor")
    ax.set_yticks(np.arange(n_rows))
    ax.set_yticklabels(row_labels, fontsize=9.6, color=TEXT)

    # Cell annotations
    span = vmax - vmin
    lo_thr = vmin + 0.20 * span
    hi_thr = vmin + 0.80 * span
    for i in range(n_rows):
        for j in range(n_cols):
            v = mat.values[i, j]
            dark_cell = (v <= lo_thr) or (v >= hi_thr)
            txt_color = "#FFFFFF" if dark_cell else "#1F1F1F"
            ax.text(j, i, f"{v*100:.0f}",
                    ha="center", va="center",
                    fontsize=8.2, color=txt_color)

    # White cell separators
    ax.set_xticks(np.arange(n_cols + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(n_rows + 1) - 0.5, minor=True)
    ax.grid(which="minor", color=BG, linewidth=1.4)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", axis="both", length=0, pad=4)
    for s in ax.spines.values():
        s.set_visible(False)

    fig.savefig(out_path, dpi=300, bbox_inches="tight", facecolor=BG)
    fig.savefig(out_path.with_suffix(".pdf"),
                bbox_inches="tight", facecolor=BG)
    plt.close(fig)


def main() -> None:
    df = pd.read_csv(DATA)
    mat, occ_counts = build_matrix(df)
    mat.to_csv(OUT_DIR / "heatmap_soc_matrix.csv")
    print(f"matrix: {mat.shape[0]} major groups x {mat.shape[1]} models")
    for code in mat.index:
        print(f"  {code}  {SHORT_NAMES[code]:<36}  "
              f"n={occ_counts[code]:>3}  mean={mat.loc[code].mean():.3f}")

    render(mat, occ_counts, OUT_DIR / "heatmap_soc_figure.png")


if __name__ == "__main__":
    main()
