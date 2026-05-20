"""Balance OCCUPATIONS per SOC group, starting from the already-evaluated
final_bank.csv (1,385 items, no re-eval needed).

Rule:
  - Merge SOC 29 + 31 into a single HEALTH bucket
  - Cap occupations per SOC group at K (default 7) — picks top K occupations
    by item count within the group
  - Cap items per occupation at M (default 7) — keeps within-group balance
  - Optional domain cap to keep CDC under threshold

Outputs go to v2_pipeline/output/balanced_occupations/ (separate from the
items-balance run in v2_pipeline/output/rebalanced/).
"""
from __future__ import annotations
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import matplotlib.pyplot as plt
from matplotlib.patches import Patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_filters import load_occ_to_group, parent_domain

REPO = Path(__file__).resolve().parents[2]
BANK_CSV = REPO / "v2_pipeline/final_bank_results/final_bank.csv"
OUT_DIR = REPO / "v2_pipeline/output/balanced_occupations"
OUT_CSV = OUT_DIR / "bank_balanced_occupations.csv"
OUT_FIG_SOC = OUT_DIR / "soc_group_distribution_balanced.png"
OUT_FIG_SRC = OUT_DIR / "source_distribution_balanced.png"

# Knobs
K_OCCS_PER_GROUP = 7
M_ITEMS_PER_OCC = 7
DOMAIN_CAP_PCT = 0.08

GROUP_NAMES = {
    "11": "Management", "13": "Business & Financial", "15": "Computer & Math",
    "17": "Architecture & Engineering", "19": "Life/Phys/Soc Science",
    "23": "Legal", "25": "Educ/Library", "27": "Arts/Design/Media",
    "33": "Protective Service", "35": "Food Prep", "39": "Personal Care",
    "41": "Sales", "43": "Office/Admin Support", "47": "Construction",
    "51": "Production", "53": "Transportation",
    "HEALTH": "Healthcare (Practitioner + Support merged)",
}

MANUAL_OCC_TO_GROUP = {
    "Special Education Teachers, All Other": ("25", "Educational Instruction & Library"),
}


def load_bank_rows():
    with BANK_CSV.open() as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    return fieldnames, rows


def major_group_merged(occupation, occ_to_group):
    if occupation in occ_to_group:
        code = occ_to_group[occupation][0]
    else:
        code = ""
    return "HEALTH" if code in ("29", "31") else (code or "?")


def build_balanced_bank(rows, occ_to_group, k_occs, m_items):
    # Group rows by (SOC_group, occupation), preserving original order
    by_group_occ = defaultdict(lambda: defaultdict(list))
    for row in rows:
        occ = row["occupation"]
        if occ not in occ_to_group:
            occ_to_group[occ] = MANUAL_OCC_TO_GROUP.get(occ, ("?", "?"))
        g = major_group_merged(occ, occ_to_group)
        by_group_occ[g][occ].append(row)

    kept_rows = []
    for g, occ_dict in by_group_occ.items():
        # Pick top k_occs occupations within this group by item count
        ranked_occs = sorted(occ_dict.items(), key=lambda kv: -len(kv[1]))[:k_occs]
        for occ, occ_rows in ranked_occs:
            # Take first m_items items per occupation
            kept_rows.extend(occ_rows[:m_items])
    return kept_rows


def apply_domain_cap(rows, cap_pct, occ_to_group):
    """Iteratively trim the most over-represented parent domain so no domain
    exceeds cap_pct of the resulting bank. Within an over-cap domain, drop
    from largest SOC groups first (preserves rarer groups' source diversity).
    """
    rows = list(rows)
    while True:
        n = len(rows)
        if n == 0:
            return rows
        target = int(cap_pct * n)
        by_dom = defaultdict(list)
        for r in rows:
            by_dom[parent_domain(r["source_url"])].append(r)
        worst = max(by_dom.items(), key=lambda kv: len(kv[1]))
        if len(worst[1]) <= target:
            return rows
        excess = len(worst[1]) - target
        by_g = defaultdict(list)
        for r in worst[1]:
            by_g[major_group_merged(r["occupation"], occ_to_group)].append(r)
        groups_desc = sorted(by_g, key=lambda g: -len(by_g[g]))
        cur = {g: len(by_g[g]) - 1 for g in groups_desc}
        to_drop = set()
        while excess > 0:
            progressed = False
            for g in groups_desc:
                if cur[g] >= 0:
                    to_drop.add(id(by_g[g][cur[g]]))
                    cur[g] -= 1
                    excess -= 1
                    progressed = True
                    if excess <= 0:
                        break
            if not progressed:
                break
        rows = [r for r in rows if id(r) not in to_drop]


def write_csv(fieldnames, rows):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {OUT_CSV}")


def write_soc_figure(rows, occ_to_group):
    per_g_items = Counter()
    per_g_occs = defaultdict(set)
    per_occ_items = Counter()
    for r in rows:
        g = major_group_merged(r["occupation"], occ_to_group)
        per_g_items[g] += 1
        per_g_occs[g].add(r["occupation"])
        per_occ_items[r["occupation"]] += 1
    n_occs_total = len(per_occ_items)
    n_occs_ge5 = sum(1 for v in per_occ_items.values() if v >= 5)

    sorted_groups = sorted(per_g_items.items(), key=lambda kv: -kv[1])
    labels, counts, occ_counts = [], [], []
    for g, c in sorted_groups:
        name = GROUP_NAMES.get(g, g)
        labels.append(name if g == "HEALTH" else f"SOC {g}  {name}")
        counts.append(c)
        occ_counts.append(len(per_g_occs[g]))

    n = len(rows)
    fig, ax = plt.subplots(figsize=(12, 8))
    y = list(range(len(labels)))
    colors = ["#3b82f6"] * len(counts)
    bars = ax.barh(y, counts, color=colors)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("Items in bank", fontsize=11)
    ax.set_title(
        f"SOC group distribution — Occupation-balanced bank\n"
        f"{n} items · {len(per_g_items)} SOC groups · {sum(occ_counts)} occupations  "
        f"({n_occs_ge5} of {n_occs_total} occupations have ≥5 items; "
        f"occs/group ≤ {K_OCCS_PER_GROUP}, items/occ ≤ {M_ITEMS_PER_OCC})",
        fontsize=11,
    )
    for bar, c, occs in zip(bars, counts, occ_counts):
        pct = 100 * c / n
        ax.text(
            bar.get_width() + max(counts) * 0.012,
            bar.get_y() + bar.get_height() / 2,
            f"{c}  ({pct:.1f}%) · {occs} occupations",
            va="center", fontsize=9,
        )
    ax.set_xlim(0, max(counts) * 1.45)
    ax.grid(axis="x", alpha=0.2, linestyle="--")
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(OUT_FIG_SOC, dpi=150, bbox_inches="tight")
    print(f"wrote figure -> {OUT_FIG_SOC}")


def write_source_figure(rows):
    n = len(rows)

    def hostname(url):
        h = urlparse(url).netloc.lower()
        if h.startswith("www."):
            h = h[4:]
        return h

    hosts = Counter(hostname(r["source_url"]) for r in rows)

    FED_BASELINE = {"cdc.gov", "osha.gov", "bls.gov"}
    FED_OTHER_ROOTS = {"nih.gov", "fda.gov", "dol.gov", "ssa.gov", "epa.gov",
                       "ftc.gov", "doe.gov", "dot.gov", "faa.gov", "fra.gov",
                       "fmcsa.gov", "fcc.gov", "fbi.gov", "ferc.gov",
                       "treasury.gov", "usgs.gov", "nasa.gov", "nrc.gov",
                       "uspto.gov", "uscis.gov", "gao.gov", "noaa.gov",
                       "energy.gov", "labor.gov", "commerce.gov",
                       "agriculture.gov", "fdic.gov", "sec.gov", "occ.gov",
                       "cms.gov", "hud.gov", "va.gov", "nist.gov", "ed.gov",
                       "doi.gov"}
    ACADEMIC_HOSTS = {"arxiv.org", "doi.org", "ncbi.nlm.nih.gov",
                      "pmc.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov"}
    ACADEMIC_PREFIXES = ("ncbi.", "pmc.", "pubmed.")

    def categorize(host):
        h = host.lower()
        if any(h == d or h.endswith("." + d) for d in FED_BASELINE):
            return "Federal .gov baseline (BLS/OSHA/CDC)"
        if h in ACADEMIC_HOSTS or any(h.startswith(p) for p in ACADEMIC_PREFIXES):
            return "Academic / .edu"
        if h.endswith(".edu"):
            return "Academic / .edu"
        if any(h == d or h.endswith("." + d) for d in FED_OTHER_ROOTS):
            return "Federal .gov (other)"
        if h.endswith(".gov"):
            return "State licensing boards"
        return "Professional societies & associations"

    cat_color = {
        "Federal .gov baseline (BLS/OSHA/CDC)": "#c44e52",
        "Federal .gov (other)": "#dd8452",
        "State licensing boards": "#8c8c3a",
        "Academic / .edu": "#8172b2",
        "Professional societies & associations": "#4c72b0",
    }

    fig = plt.figure(figsize=(20, 10))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.7, 1], wspace=0.25)
    ax_bar = fig.add_subplot(gs[0, 0])
    ax_donut = fig.add_subplot(gs[0, 1])

    top = hosts.most_common(20)
    labels = [d for d, _ in top]
    counts = [c for _, c in top]
    bar_colors = [cat_color[categorize(d)] for d in labels]
    y = list(range(len(labels)))
    bars = ax_bar.barh(y, counts, color=bar_colors)
    ax_bar.set_yticks(y)
    ax_bar.set_yticklabels(labels, fontsize=10)
    ax_bar.invert_yaxis()
    ax_bar.set_xlabel("Items", fontsize=11)
    ax_bar.set_title(
        f"Source distribution — Occupation-balanced bank\n"
        f"{n} items · {len(hosts)} unique source domains",
        fontsize=12,
    )
    for bar, c in zip(bars, counts):
        ax_bar.text(bar.get_width() + max(counts) * 0.01,
                    bar.get_y() + bar.get_height() / 2,
                    f"{c}  ({100*c/n:.1f}%)", va="center", fontsize=9)
    ax_bar.set_xlim(0, max(counts) * 1.18)

    legend_label = {
        "Federal .gov baseline (BLS/OSHA/CDC)": "Federal .gov baseline (BLS/OSHA/CDC)",
        "Federal .gov (other)": "Federal .gov (other)",
        "State licensing boards": "State licensing boards",
        "Academic / .edu": "Academic / .edu (arXiv, PMC, OA journals)",
        "Professional societies & associations": "Professional societies & associations",
    }
    present = [c for c in legend_label
               if any(categorize(d) == c for d in labels)]
    ax_bar.legend(handles=[Patch(color=cat_color[c], label=legend_label[c]) for c in present],
                  loc="lower right", fontsize=10)

    cats = Counter()
    for d, c in hosts.items():
        cats[categorize(d)] += c
    cat_order = ["Federal .gov baseline (BLS/OSHA/CDC)",
                 "Federal .gov (other)", "State licensing boards",
                 "Academic / .edu", "Professional societies & associations"]
    sizes = [cats.get(c, 0) for c in cat_order]
    colors = [cat_color[c] for c in cat_order]
    nz = [(c, s, col) for c, s, col in zip(cat_order, sizes, colors) if s > 0]
    cat_lbl, sizes, colors = zip(*nz)
    wedges, _ = ax_donut.pie(sizes, colors=colors, startangle=90,
                              wedgeprops={"width": 0.32, "edgecolor": "white", "linewidth": 1})
    ax_donut.set_title("Source category mix", fontsize=12)
    legend_lines = [f"{c}\n{s} ({100*s/n:.1f}%)" for c, s in zip(cat_lbl, sizes)]
    ax_donut.legend(wedges, legend_lines, loc="center left",
                    bbox_to_anchor=(1.05, 0.5), fontsize=10, frameon=False,
                    handlelength=1.5, handleheight=1.5)
    ax_donut.set_aspect("equal")

    fig.tight_layout()
    fig.savefig(OUT_FIG_SRC, dpi=150, bbox_inches="tight")
    print(f"wrote figure -> {OUT_FIG_SRC}")


def main():
    occ_to_group = load_occ_to_group()
    fieldnames, rows = load_bank_rows()
    print(f"loaded {len(rows)} rows from {BANK_CSV.relative_to(REPO)}")

    balanced = build_balanced_bank(rows, occ_to_group, K_OCCS_PER_GROUP, M_ITEMS_PER_OCC)
    print(f"after occ-cap={K_OCCS_PER_GROUP} + items-per-occ-cap={M_ITEMS_PER_OCC}: "
          f"{len(balanced)} items")

    final = apply_domain_cap(balanced, DOMAIN_CAP_PCT, occ_to_group)
    print(f"after domain-cap={int(DOMAIN_CAP_PCT*100)}%: {len(final)} items")

    write_csv(fieldnames, final)
    write_soc_figure(final, occ_to_group)
    write_source_figure(final)


if __name__ == "__main__":
    main()
