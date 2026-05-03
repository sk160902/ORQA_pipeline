"""Export v13 diversity100 unfiltered bank, filtered to SOCs with ≥5 items.

Inputs:
  output/diversity50_20260430_133152/items_unfiltered.jsonl

Outputs:
  output/diversity50_20260430_133152/diversity100_v13_bank_min5.csv
  output/diversity50_20260430_133152/source_distribution_diversity100_v13.png
  output/diversity50_20260430_133152/source_distribution_diversity100_v13.pdf
  output/diversity50_20260430_133152/source_distribution_diversity100_v13.json
"""
from __future__ import annotations
import csv
import json
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "output" / "diversity50_20260430_133152"
ITEMS_PATH = RUN_DIR / "items_unfiltered.jsonl"
MIN_ITEMS_PER_SOC = 5

BASELINE = {"osha.gov", "bls.gov", "cdc.gov"}


def host_of(url: str) -> str:
    h = urllib.parse.urlparse(url).netloc.lower()
    if h.startswith("www."):
        h = h[4:]
    for b in BASELINE:
        if h == b or h.endswith("." + b):
            return b
    org_map = {
        "stacks.cdc.gov": "cdc.gov",
        "bankingjournal.aba.com": "aba.com",
        "myimanetwork.imanet.org": "imanet.org",
        "queue.acm.org": "acm.org",
        "cacm.acm.org": "acm.org",
        "dl.acm.org": "acm.org",
        "rpc.cfainstitute.org": "cfainstitute.org",
        "storage.aanp.org": "aanp.org",
        "amplify.asce.org": "asce.org",
        "pubs.asce.org": "asce.org",
        "cedb.asce.org": "asce.org",
        "asmedigitalcollection.asme.org": "asme.org",
        "dl.astm.org": "astm.org",
        "store.astm.org": "astm.org",
        "mcsdocs.astm.org": "astm.org",
        "news.awwa.org": "awwa.org",
        "dl.ashrae.org": "ashrae.org",
        "aimehq.org": "aimehq.org",
        "aade.org": "aade.org",
        "spe.org": "spe.org",
    }
    return org_map.get(h, h)


def categorize(host: str) -> str:
    if host == "cdc.gov" or host == "bls.gov" or host == "osha.gov":
        return "Federal .gov baseline (BLS/OSHA/CDC)"
    if host.endswith(".gov"):
        if any(host.endswith(s) for s in (".ca.gov", ".tx.gov", ".texas.gov", ".fl.gov", ".ny.gov", ".state.gov")):
            return "State licensing boards"
        return "Federal .gov (other)"
    if host.endswith(".edu") or "ncbi.nlm.nih.gov" in host:
        return "Academic / .edu"
    return "Professional societies & associations"


def write_bank_csv(items: list[dict], out_path: Path):
    cols = ["onet_soc_code", "occupation_title", "source_url",
            "Questions", "option_A", "option_B", "option_C", "option_D",
            "option_E", "option_F", "correct_answer", "publisher", "source_tier", "quality_tier"]
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for it in items:
            opts = it.get("options") or {}
            row = {
                "onet_soc_code": it.get("onet_soc_code", ""),
                "occupation_title": it.get("occupation_title", ""),
                "source_url": (it.get("source_url") or "").strip(),
                "Questions": it.get("question", ""),
                "option_A": opts.get("A", "").strip(),
                "option_B": opts.get("B", "").strip(),
                "option_C": opts.get("C", "").strip(),
                "option_D": opts.get("D", "").strip(),
                "option_E": opts.get("E", "All of the above").strip(),
                "option_F": opts.get("F", "None of the above").strip(),
                "correct_answer": (it.get("correct_answer") or "").strip(),
                "publisher": it.get("publisher", ""),
                "source_tier": it.get("source_tier", ""),
                "quality_tier": it.get("quality_tier", ""),
            }
            w.writerow(row)
    print(f"Wrote {out_path}  ({len(items)} rows)")


def make_figure(items: list[dict], out_png: Path, out_pdf: Path, out_json: Path,
                title_suffix: str = ""):
    hosts = Counter(host_of(it.get("source_url", "")) for it in items
                    if (it.get("source_url") or "").startswith("http"))
    total = sum(hosts.values())
    cats = Counter()
    for h, n in hosts.items():
        cats[categorize(h)] += n

    fig = plt.figure(figsize=(14, 8))
    gs = fig.add_gridspec(1, 2, width_ratios=[2.2, 1], wspace=0.30)
    ax_top = fig.add_subplot(gs[0, 0])
    ax_donut = fig.add_subplot(gs[0, 1])

    top = hosts.most_common(20)
    labels = [h for h, _ in top]
    values = [n for _, n in top]
    pct = [100 * v / total for v in values]

    y = np.arange(len(labels))
    colors = ["#3a6ea5"] * len(labels)
    for i, h in enumerate(labels):
        if h in BASELINE:
            colors[i] = "#c0504d"

    bars = ax_top.barh(y, values, color=colors, edgecolor="white")
    ax_top.set_yticks(y)
    ax_top.set_yticklabels(labels, fontsize=9)
    ax_top.invert_yaxis()
    ax_top.set_xlabel("Items")
    ax_top.set_title(f"Source distribution — 20 occupations (≥5 questions per occupation){title_suffix}\n"
                     f"{total} items · {len(hosts)} unique source domains",
                     fontsize=12)
    for i, (b, v, p) in enumerate(zip(bars, values, pct)):
        ax_top.text(v + max(values) * 0.005, b.get_y() + b.get_height()/2,
                    f"{v}  ({p:.1f}%)", va="center", fontsize=8.5)
    ax_top.set_xlim(0, max(values) * 1.20)
    import matplotlib.patches as mpatches
    legend_handles = [
        mpatches.Patch(color="#c0504d", label="Federal .gov baseline (BLS/OSHA/CDC)"),
        mpatches.Patch(color="#3a6ea5", label="Per-occupation associations + state boards"),
    ]
    ax_top.legend(handles=legend_handles, loc="lower right", fontsize=8.5, frameon=True)
    ax_top.grid(axis="x", alpha=0.25)

    cat_order = [
        "Federal .gov baseline (BLS/OSHA/CDC)",
        "Federal .gov (other)",
        "State licensing boards",
        "Academic / .edu",
        "Professional societies & associations",
    ]
    cat_labels, cat_vals, cat_colors = [], [], []
    cat_colors_map = {
        "Federal .gov baseline (BLS/OSHA/CDC)": "#c0504d",
        "Federal .gov (other)": "#e0833e",
        "State licensing boards": "#7f8b1d",
        "Academic / .edu": "#9670bf",
        "Professional societies & associations": "#3a6ea5",
    }
    for cat in cat_order:
        v = cats.get(cat, 0)
        if v == 0: continue
        cat_labels.append(f"{cat}\n{v} ({100*v/total:.1f}%)")
        cat_vals.append(v)
        cat_colors.append(cat_colors_map[cat])

    wedges, _ = ax_donut.pie(
        cat_vals, colors=cat_colors,
        startangle=90, counterclock=False,
        wedgeprops=dict(width=0.36, edgecolor="white", linewidth=2),
    )
    ax_donut.set_title("Source category mix", fontsize=12)
    ax_donut.legend(wedges, cat_labels, loc="center left",
                    bbox_to_anchor=(1.0, 0.5), fontsize=8.5, frameon=False)

    plt.tight_layout()
    fig.savefig(out_png, dpi=160, bbox_inches="tight")
    fig.savefig(out_pdf, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_png}")
    print(f"Wrote {out_pdf}")

    summary = {
        "total_items": total,
        "unique_domains": len(hosts),
        "top_20": [{"host": h, "n": n, "pct": 100 * n / total} for h, n in top],
        "categories": [{"category": k, "n": v, "pct": 100 * v / total}
                       for k, v in cats.most_common()],
    }
    out_json.write_text(json.dumps(summary, indent=2))
    print(f"Wrote {out_json}")


def main():
    items = [json.loads(line) for line in ITEMS_PATH.open() if line.strip()]
    print(f"Loaded {len(items)} items from {ITEMS_PATH}")

    by_soc = defaultdict(list)
    for it in items:
        by_soc[it["onet_soc_code"]].append(it)

    print(f"Total SOCs: {len(by_soc)}")
    surviving = {soc: its for soc, its in by_soc.items() if len(its) >= MIN_ITEMS_PER_SOC}
    dropped = {soc: its for soc, its in by_soc.items() if len(its) < MIN_ITEMS_PER_SOC}
    print(f"SOCs ≥{MIN_ITEMS_PER_SOC} items (kept): {len(surviving)}")
    print(f"SOCs <{MIN_ITEMS_PER_SOC} items (dropped from this export): {len(dropped)}")

    filtered_items = []
    for soc in sorted(surviving.keys()):
        filtered_items.extend(surviving[soc])

    print(f"\nFiltered bank: {len(filtered_items)} items across {len(surviving)} SOCs")

    write_bank_csv(filtered_items, RUN_DIR / "diversity100_v13_bank_min5.csv")
    make_figure(filtered_items,
                out_png=RUN_DIR / "source_distribution_diversity100_v13.png",
                out_pdf=RUN_DIR / "source_distribution_diversity100_v13.pdf",
                out_json=RUN_DIR / "source_distribution_diversity100_v13.json",
                title_suffix="")


if __name__ == "__main__":
    main()
