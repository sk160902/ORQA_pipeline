# Figures

Plot scripts that produce the figures in the NeurIPS 2026 ORQA paper from a
merged eval bank (`may6_bank.csv` and friends).

## Scripts

| Script | Output | What it draws |
|---|---|---|
| `main_figure.py` | `main_figure.png/.pdf`, `main_results.csv` | Three-panel dot-and-whisker leaderboard: MC accuracy, open-ended accuracy, and wage-weighted MC accuracy with 95% CIs across the 15 evaluated models |
| `heatmap_figure.py` | `heatmap_figure.png/.pdf`, `heatmap_full_matrix.csv` | 3-panel occupation x model heatmap (top-15, middle-15, bottom-15 occupations by mean accuracy across models). Drops occupations with fewer than 3 questions |
| `heatmap_soc_figure.py` | `heatmap_soc_figure.png/.pdf`, `heatmap_soc_matrix.csv` | SOC major-group x model heatmap (23 BLS major groups), with the per-group occupation count annotated on each row |
| `make_soc_heatmaps.py` | `heatmap_soc_major_groups.png`, `heatmap_soc_mega_groups.png` | Older companion: SOC major-group + 6-bucket "mega-group" heatmaps with a viridis cmap. Uses an O*NET title-to-SOC mapping rather than the BLS OEWS file |

The first three share an Economist-style aesthetic (RdBu cmap, Helvetica
Neue, navy/red/olive accents) and a single `MODELS` registry. They each
embed a `correct_answer` lookup against `<model>_seedN` columns in the
input CSV (3 seeds each). `main_figure.py` additionally reads
`<model>_majority_vote` for the open-ended panel and BLS OEWS wages for
the wage-weighted panel.

## Inputs

Each script resolves its inputs through environment variables, falling
back to the convention that data lives one directory above the repo root
(i.e. `<parent-of-ORQA_pipeline>/...`).

| Env var | Used by | Default |
|---|---|---|
| `ORQA_DATA_DIR` | all | `<parent-of-ORQA_pipeline>` |
| `ORQA_BANK_CSV` | `main_figure.py`, `heatmap_figure.py`, `heatmap_soc_figure.py` | `$ORQA_DATA_DIR/may6_bank.csv` |
| `ORQA_WAGE_XLSX` | `main_figure.py`, `heatmap_soc_figure.py` | `$ORQA_DATA_DIR/oesm24nat/national_M2024_dl.xlsx` |
| `ORQA_ONET_OCC` | `make_soc_heatmaps.py` | `/tmp/onet/Occupation Data.txt` |
| `ORQA_QA_CSV` | `make_soc_heatmaps.py` | `$ORQA_DATA_DIR/question_model_responses - question_model_responses.csv` |

## Run

```bash
# from ORQA_pipeline/figures/
python main_figure.py
python heatmap_figure.py
python heatmap_soc_figure.py
python make_soc_heatmaps.py
```

To point at a custom data directory:

```bash
ORQA_DATA_DIR=/path/to/dropbox python main_figure.py
```

Or override individual files:

```bash
ORQA_BANK_CSV=/path/to/may6_bank.csv \
ORQA_WAGE_XLSX=/path/to/oesm24nat/national_M2024_dl.xlsx \
python main_figure.py
```

## Dependencies

`matplotlib`, `numpy`, `pandas`, `openpyxl` (for reading the BLS xlsx).
