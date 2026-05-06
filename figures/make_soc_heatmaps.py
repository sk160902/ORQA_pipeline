"""Build SOC-major-group and mega-group accuracy heatmaps.

Older companion to heatmap_soc_figure.py: uses an O*NET title-to-SOC
mapping rather than the BLS OEWS file, and produces both a 23-major-group
heatmap and a 6-mega-group heatmap with a viridis colormap.

Inputs (override via env vars):
  ORQA_ONET_OCC  O*NET 'Occupation Data.txt' (tab-separated)
                 (default: /tmp/onet/Occupation Data.txt)
  ORQA_QA_CSV    question_model_responses CSV
                 (default: <repo-parent>/question_model_responses - question_model_responses.csv)

Outputs land alongside this script in ORQA_pipeline/figures/.
"""
import csv
import os
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import Normalize

csv.field_size_limit(sys.maxsize)

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent
ROOT = Path(os.environ.get("ORQA_DATA_DIR", DEFAULT_ROOT))
ONET_PATH = Path(os.environ.get("ORQA_ONET_OCC", "/tmp/onet/Occupation Data.txt"))
QA_CSV = Path(os.environ.get(
    "ORQA_QA_CSV",
    ROOT / "question_model_responses - question_model_responses.csv",
))
OUT_DIR = HERE
OUT_DIR.mkdir(exist_ok=True)

MAJOR_LABELS = {
    "11": "Management",
    "13": "Business & Finance",
    "15": "Computer & Math",
    "17": "Architecture & Engineering",
    "19": "Life/Physical/Social Science",
    "21": "Community & Social Service",
    "23": "Legal",
    "25": "Education & Library",
    "27": "Arts/Design/Entertainment/Media",
    "29": "Healthcare Practitioners",
    "31": "Healthcare Support",
    "33": "Protective Service",
    "35": "Food Prep & Serving",
    "37": "Building & Grounds",
    "39": "Personal Care",
    "41": "Sales",
    "43": "Office & Admin",
    "45": "Farming/Fishing/Forestry",
    "47": "Construction & Extraction",
    "49": "Install/Maintenance/Repair",
    "51": "Production",
    "53": "Transportation",
    "55": "Military",
}

MEGA = {
    "Mgmt / Business / Legal": ["11", "13", "23"],
    "STEM": ["15", "17", "19"],
    "Healthcare": ["29", "31"],
    "Education, Arts & Social": ["21", "25", "27"],
    "Service & Sales": ["33", "35", "37", "39", "41", "43"],
    "Trades / Production / Transport": ["45", "47", "49", "51", "53", "55"],
}

# Models sorted by overall accuracy (desc) from earlier analysis
MODELS_ORDERED = [
    "gpt_5_4",
    "claude_opus_4_6",
    "claude_sonnet_4_6",
    "gpt_5_2",
    "gemini_3_1_pro",
    "gpt_5_3_chat_latest",
    "o3",
    "claude_haiku_4_5",
    "gemini_2_5_flash",
    "gpt_4o",
    "DeepSeek_V3",
    "llama_3_3_70B",
    "gpt_4o_mini",
    "Qwen2_5_7B_Instruct_Turbo",
    "gpt_3_5_turbo",
]


def load_onet():
    title_to_soc = {}
    with open(ONET_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            title_to_soc[r["Title"]] = r["O*NET-SOC Code"]
    title_to_soc["Plumbers"] = "47-2152.00"  # O*NET lists as combined title
    return title_to_soc


def build_long_frame():
    title_to_soc = load_onet()
    df = pd.read_csv(QA_CSV)
    df["soc_code"] = df["occupation"].map(title_to_soc)
    missing = df[df["soc_code"].isna()]
    if len(missing):
        print(f"WARNING: {len(missing)} rows without SOC code")
    df["soc_major"] = df["soc_code"].str[:2]

    soc_to_mega = {c: m for m, codes in MEGA.items() for c in codes}
    df["mega"] = df["soc_major"].map(soc_to_mega)

    seed_cols = [c for c in df.columns if re.search(r"_seed\d+$", c)]
    id_cols = ["occupation", "soc_major", "mega", "correct_answer"]
    long_df = df.melt(
        id_vars=id_cols, value_vars=seed_cols, var_name="run", value_name="response"
    )
    long_df["model"] = long_df["run"].str.replace(r"_seed\d+$", "", regex=True)
    long_df["correct"] = (
        long_df["response"].astype(str).str.strip().str.upper()
        == long_df["correct_answer"].astype(str).str.strip().str.upper()
    )
    return df, long_df


def draw_heatmap(matrix, row_labels, col_labels, col_counts, title, path,
                 figsize, cell_fontsize=9, rotate=45):
    fig, ax = plt.subplots(figsize=figsize)
    norm = Normalize(vmin=0.15, vmax=0.85)
    im = ax.imshow(matrix, cmap="viridis", aspect="auto", norm=norm)

    ax.set_xticks(range(len(col_labels)))
    ax.set_xticklabels(
        [f"{lbl}\n(n={col_counts[lbl]})" for lbl in col_labels],
        rotation=rotate,
        ha="right" if rotate else "center",
    )
    ax.set_yticks(range(len(row_labels)))
    ax.set_yticklabels(row_labels)

    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            val = matrix[i, j]
            if np.isnan(val):
                ax.text(j, i, "-", ha="center", va="center", color="#888",
                        fontsize=cell_fontsize)
                continue
            color = "white" if val < 0.5 else "black"
            ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                    color=color, fontsize=cell_fontsize)

    ax.set_title(title, fontsize=13, pad=12)
    cbar = fig.colorbar(im, ax=ax, shrink=0.7, label="accuracy")
    cbar.ax.axhline(1 / 6, color="red", linestyle="--", linewidth=1)
    cbar.ax.text(1.8, 1 / 6, "chance", color="red", va="center", fontsize=8)

    plt.tight_layout()
    plt.savefig(path, dpi=160, bbox_inches="tight")
    print("wrote", path)


def main():
    df, long_df = build_long_frame()

    # ---- Heatmap 1: 23 SOC major groups ----
    present_majors = sorted(df["soc_major"].dropna().unique())
    col_order_23 = [m for m in sorted(MAJOR_LABELS) if m in present_majors]
    col_labels_23 = [f"{code} . {MAJOR_LABELS[code]}" for code in col_order_23]
    counts_23_by_code = df["soc_major"].value_counts().to_dict()
    counts_23 = {
        f"{code} . {MAJOR_LABELS[code]}": counts_23_by_code.get(code, 0)
        for code in col_order_23
    }

    pivot23 = (
        long_df.groupby(["model", "soc_major"])["correct"].mean().unstack("soc_major")
    )
    pivot23 = pivot23.reindex(index=MODELS_ORDERED, columns=col_order_23)
    pivot23.columns = col_labels_23
    draw_heatmap(
        pivot23.values,
        row_labels=list(pivot23.index),
        col_labels=col_labels_23,
        col_counts=counts_23,
        title=(
            "Model accuracy by SOC Major Group (2-digit)\n"
            "1,444 questions . 15 models . mean over 3 seeds"
        ),
        path=str(OUT_DIR / "heatmap_soc_major_groups.png"),
        figsize=(18, 9),
        cell_fontsize=8,
        rotate=45,
    )

    # ---- Heatmap 2: 6 mega groups ----
    mega_order = list(MEGA.keys())
    counts_mega = df["mega"].value_counts().to_dict()
    counts_mega = {k: counts_mega.get(k, 0) for k in mega_order}

    pivot_mega = (
        long_df.groupby(["model", "mega"])["correct"].mean().unstack("mega")
    )
    pivot_mega = pivot_mega.reindex(index=MODELS_ORDERED, columns=mega_order)

    draw_heatmap(
        pivot_mega.values,
        row_labels=list(pivot_mega.index),
        col_labels=mega_order,
        col_counts=counts_mega,
        title=(
            "Model accuracy by Occupational Family (SOC-mega)\n"
            "1,444 questions . 15 models . mean over 3 seeds"
        ),
        path=str(OUT_DIR / "heatmap_soc_mega_groups.png"),
        figsize=(11, 8),
        cell_fontsize=10,
        rotate=25,
    )


if __name__ == "__main__":
    main()
