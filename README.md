# ORQA Pipeline

Two complementary pipelines for generating multiple-choice question (MCQ) benchmark items from real-world professional sources, organised by the kind of source each draws from.

```
ORQA_pipeline/
├── Authoritative/   – generates items from authoritative web sources
│                      (federal regulators, statistical agencies, professional
│                      associations, state licensing boards, academic literature)
│
└── Community/       – generates items from community-thread sources
                       (Reddit, MetaFilter, AllNurses, StackExchange)
```

Both pipelines share the same downstream evaluation format: each item is a six-option MCQ with the same column schema as the published bank.

---

## Authoritative/

The Authoritative pipeline produces MCQs whose correct answer is fully entailed by a quote from a real authoritative document. Sources include CDC, OSHA, BLS, IRS, FDA, professional society publications, state licensing-board pages, and academic literature retrieved through arXiv, PubMed Central, and Crossref/Unpaywall.

### `pipeline/` (importable package)

| Module | Role |
|---|---|
| `orchestrator.py` | Top-level driver that strings the stages together for one occupation and one run |
| `occupation_selection.py` | Wage-bill-weighted selection of occupations across SOC major groups, with primaries + reserves |
| `onet_context.py`, `enrichment.py` | Loads O*NET task descriptions and alternate job titles to seed discovery queries |
| `source_whitelist.py`, `source_selection.py` | Builds and ranks the per-occupation domain whitelist used to constrain search |
| `search_client.py` | Search-engine client used by the discovery rounds |
| `source_fetcher.py` | URL fetching with direct-then-proxy fallback |
| `academic_sources.py` | Round-0 academic discovery: arXiv, PubMed Central efetch, Crossref + Unpaywall |
| `evidence_cards.py` | Section-aware text chunking and evidence-card extraction (situation + recommendation + verbatim quote) |
| `item_generator.py` | Builds the six-option MCQ from an evidence card, with format constraints |
| `item_verifier.py` | Runs the nine sequential verifier passes; six passes have a regenerate-not-reject path |
| `post_replace.py` | In-pass distractor replacement called by the verifier when an option fails plausibility/eliminability |
| `cognitive_type.py`, `difficulty_pretest.py` | Cognitive-type tagging and the three-model difficulty pretest |
| `clients.py`, `config.py`, `schemas.py`, `diagnostics.py` | API clients, run config, pydantic schemas, structured logging |

### Setup / build scripts (root of `Authoritative/`)

| Script | What it does |
|---|---|
| `build_wagebill100_setup.py` | Computes wage-bill share per SOC major group from BLS OEWS and produces the proportional 100-occupation primary list (with reserves) |
| `build_v2_whitelists.py` | Produces the v2 per-occupation domain whitelist (gov + professional society + state licensing board) |
| `build_diversity30_whitelists.py`, `build_diversity600_whitelists.py` | Larger whitelist builds used for diversity-targeted runs |
| `build_diversity50_extras.py`, `build_diversity100_extras.py`, `build_diversity200_extras.py` | Per-occupation extra-domain extension files |
| `build_diversity_reserves.py` | Builds the wage-bill-ranked reserve list per SOC major group used by the replacement phase |

Run any of these with `python <script>.py` from the `Authoritative/` directory. They are idempotent, write CSV/JSON setup files into the project's `output/` directory, and have no API costs.

### Generation runners

| Script | What it does | How to run |
|---|---|---|
| `run_pilot20.py` | Single-process worker that runs the orchestrator over one chunk CSV of occupations and writes per-worker `items.jsonl` + `stats.json` | `python run_pilot20.py --chunk chunk.csv --out-dir runs/foo` |
| `launch_pilot20_parallel.py` | Splits the input list across N workers and spawns N `run_pilot20.py` processes in parallel | `python launch_pilot20_parallel.py --n-workers 8 --input occupations.csv` |
| `launch_diversity50.py` | Larger parallel launcher (canonical for the wage-weighted multi-occupation runs); 32 workers by default | `python launch_diversity50.py` |
| `auto_launch_v19.sh` | Convenience shell wrapper that calls the launcher with the canonical wage-weighted setup files | `bash auto_launch_v19.sh` |

### Recovery / supplemental runs

| Script | What it does |
|---|---|
| `launch_recovery.py` | Re-spawns workers for SOCs that ended with `urls_discovered = 0` due to mid-run search-API exhaustion. Workers process the damaged SOCs from scratch; results are merged in by `evidence_id` deduplication |
| `launch_supplement.py` | Adds an extra round of generation against a fresh occupation list to top up under-yielded categories |

### Replacement and post-filter

| Script | What it does |
|---|---|
| `parallel_replacement.py` | After all primary workers exit, walks SOCs that fell short of the per-SOC floor and re-runs the orchestrator on the next reserve in that SOC major group. The successful reserve replaces the under-yielded primary; if reserves are exhausted, the slot is left vacant |
| `pass8_post_filter.py` | Stand-alone re-run of the eliminability judge over the merged bank, used when the in-pipeline Pass 8 needs to be reapplied with a sharper prompt |
| `post_filter_giveaway.py` | Catches and removes named-source giveaway phrases ("According to the AICPA Code…") that slipped past Pass 6b |
| `topup_to_n_slots.py` | Floor enforcement: takes the merged bank and topup-runs the pipeline on still-short SOCs until each has at least N items, where N is the published floor |
| `merge_unfiltered_bank.py` | Merges all worker `items.jsonl` files into one unfiltered bank JSON for downstream filter passes |

### Export / docs

| Script | What it does |
|---|---|
| `export_pilot5.py`, `export_v13_diversity100.py`, `export_v19_final.py` | Per-run final exporters. Apply the floor filter (drop SOCs with fewer than 5 items), enforce the source-mix gate, normalise option E/F to "All of the above" / "None of the above", randomise correct-letter position, and write the final CSV in the bank schema |
| `finalize_v19_daemon.sh` | Daemon that polls the launcher PIDs and triggers the export step once they all exit (or once enough quota errors accumulate) |
| `generate_v19_documentation.py` | Walks the run logs and produces a human-readable run report (source distribution, per-SOC stats, verifier-pass counts) |

### End-to-end run, in order

A complete authoritative run reproduces the bank by going through these stages — each step's outputs feed the next.

**1. Occupation selection.** `build_wagebill100_setup.py` ingests the BLS Occupational Employment and Wage Statistics release and allocates a target item count to each SOC major group proportional to that group's wage bill, with primaries and a wage-bill-ranked reserve list per major group.

**2. Whitelist construction.** `build_v2_whitelists.py` (or the diversity variants) attaches a per-occupation domain whitelist combining global authoritative defaults (CDC, OSHA, BLS, IRS, FDA, …) with per-occupation professional-society URLs and per-state licensing-board URLs.

**3. Discovery and item generation.** The launcher (`launch_diversity50.py`, or `auto_launch_v19.sh` for the wrapper) shards the occupation list across workers. Each worker calls `pipeline.orchestrator.run` for its occupations. Per occupation, the orchestrator goes through:

  - **Round 0 — academic discovery**: arXiv, PubMed Central efetch, Crossref + Unpaywall queries, restricted to whitelisted academic domains.
  - **Rounds 1–10 — search-backed discovery**: query expansions combining the canonical occupation title with O*NET sample titles and core-task keywords against the whitelist.
  - **Source fetching**: direct urllib first, premium proxy fallback for sites that bot-block.
  - **Section extraction and chunking**: text is cleaned and split on detected section headers; each chunk is sent to `evidence_cards.extract` to pull `(situation, recommendation, verbatim_quote)` triples.
  - **Item building**: each evidence card becomes one MCQ via `item_generator.build`, which enforces option-length parity, no named-source giveaway phrases, and JSON-only output.

**4. Nine verifier passes.** Every successfully built item runs through `pipeline.item_verifier.verify_item`:

  1. **Source entailment** — verifier confirms the correct option is fully entailed by the source quote.
  2. **Distractor plausibility** — distractors must be on-topic enough that elimination by topicality alone fails (regenerate path: replace the offending distractor and re-enter the verifier).
  3. **Occupation alignment** — the scenario must be a job task the named occupation actually performs.
  4. **Question specificity** — the stem must include a concrete scenario; vague stems are rewritten with named tools, time pressure, or constraints (regenerate path).
  5. **Distractor distinctness** — no two distractors paraphrase the same answer (regenerate path: rewrite a duplicate).
  6. **Format and length sanity** — option-length parity, no malformed JSON, no source-name giveaway in the correct option (regenerate path on the giveaway sub-pass).
  7. **Difficulty pretest** — three closed-book models from three providers answer the stem only. All-correct is rejected as too-easy; all-wrong with low verifier confidence is rejected as too-hard-low-conf; everything else is kept and tagged easy/medium/hard.
  8. **Eliminability judge** — distractors that can be eliminated by general world knowledge alone are flagged and regenerated into more defensible alternatives.
  9. **Correct-option specificity** — vague-qualifier patterns ("appropriate", "adequate", "follow guidelines") in the correct option are rewritten with concrete details from the source quote (regenerate path).

**5. Acceptance gates.** Items that pass verification are then checked against:

  - **Jaccard dedup** at threshold 0.6 against every previously accepted item for the same SOC.
  - **Source-mix cap**: no single source domain may exceed 30% of the SOC's items (40–50% for canonical authorities such as CDC for healthcare or OSHA for industrial; 15–20% for tangential sources).
  - **Blocked-source list** for low-quality aggregators.

**6. Replacement phase.** When all primary workers exit, `parallel_replacement.py` walks SOCs that ended with fewer than 15 items or with a single-source dominance breach. For each, the next reserve in the same SOC major group is run through the full pipeline. The first reserve that produces at least 15 acceptable items replaces the primary; if all reserves are exhausted, the SOC slot is left vacant.

**7. Post-replacement floor.** `topup_to_n_slots.py` enforces a hard per-SOC floor (default 5). SOCs still short are dropped from the published bank.

**8. Export.** `export_v19_final.py` (or the parallel exporter for an earlier run) writes the final CSV in the bank schema, normalises option E ("All of the above") and F ("None of the above"), randomises the correct-letter position so A–F are roughly uniform, and emits the source-distribution report.

`finalize_v19_daemon.sh` automates steps 6–8: it polls the launcher PIDs every 60 seconds and runs the merge + replacement + export sequence once all workers have exited.

### Operational notes

- Each worker writes incrementally to `items.jsonl`, `stats.json`, and a structured log; the orchestrator's quota-aware resume logic handles re-runs cleanly: SOCs with `items_kept ≥ 1` are preserved as-is; `items_kept = 0` with mostly extract failures is retried; `items_kept = 0` with no fetches is also retried (signature of search-API outage).
- Per-SOC operational caps: `MAX_FAILURES_PER_OCCUPATION = 12`, `MAX_FETCH_FAILURES_PER_OCCUPATION = 35`, `MAX_DISCOVERY_QUERIES_PER_OCCUPATION = 30`, target item count = 20.

---

## Community/

The Community pipeline produces MCQs whose correct answer comes from the upvoted top-voted response to a question on a community forum. Distractors are mined from the same thread (sibling answers, off-topic replies) so that the wrong options remain on-topic but materially different from the correct guidance.

### Generators

| Script | Iteration | What it does |
|---|---|---|
| `3_generate_qa.py` | base | Initial Q&A scaffolding: takes a thread, chunks it, calls the LLM with a six-option MCQ prompt |
| `34_generate_reddit_multi_v5.py` | v5 | Reddit-first multi-correct generator. Prioritises practitioner-voiced advice in the question stem |
| `37_generate_multi_v6.py` | v6 | Adds AskMetaFilter alongside Reddit; multi-correct verified items |
| `63_auto_source_prototype.py` | proto | First fully-automated discovery → MCQ pipeline: web-searches for community threads given an occupation |
| `64_auto_source_v2.py` | v2 | Single-answer items where wrong distractors all come from the same thread (no synthetic distractors) |
| `65_auto_source_v3_multivenue.py` | v3 | Multi-venue: Reddit + Stack Exchange in one pass |
| `66_auto_source_v4_occupation.py` | v4 | Canonical occupation-level pipeline; produced the bulk of the published 1,444-item bank |
| `69_auto_source_v5_strict.py` | v5 | Stricter source-quality filter on v4 |
| `70_auto_source_v6_hybrid.py` | v6 | Hybrid scoring across upvote ratio, top/next ratio, and reply count |
| `71_auto_source_v7_alttitles.py` | v7 | Final iteration: O*NET sample-title expansion broadens discovery for occupations that hit canonical-title dead-ends |

Each generator runs as `python <script>.py --from-csv occupations.csv --target N --ckpt-path ckpt.json --bank-path bank.json --log-path worker.log`. Inputs and outputs match the format used by the launchers below.

### Filters and quality passes

| Script | What it does |
|---|---|
| `44_upvote_consensus_filter.py` | Records `top_answer_score` and `next_answer_score`; keeps only items whose top answer's lead over the second answer exceeds the consensus threshold |
| `71_filter_v4_distractors.py` | Post-hoc distractor quality filter on the v4 output |
| `72_advice_agreement_filter.py` | Confirms the top-upvoted response actually maps to the correct option, and that distractors express materially different advice |
| `74_strip_community_voice.py` | Strips first-person community-voice phrasing ("I usually…", "we tried…") from scenarios so they read as neutral occupational situations |
| `98_advice_filter_parallel.py` | Parallel implementation of the advice-agreement filter for large bank sweeps |
| `99_launch_filter_pipeline.py` | Launcher that runs the filters in sequence over a freshly generated bank |

### Backfills

| Script | What it does |
|---|---|
| `49_add_reddit_to_pilots.py` | Rebalances pilot banks to a fixed split of two Stack-Exchange + two Reddit items per (occupation, approach, type) |
| `57_approach2_multi_reddit_backfill.py` | Reddit-only generator that backfills the missing slots in the multi-correct Approach 2 pilot |

### Selection / O*NET

| Script | What it does |
|---|---|
| `1_parse_onet.py` | Initial O*NET task parser |
| `91_parse_onet_full.py` | Full O*NET 29.x parser; emits `output/onet_tasks_parsed_full.csv` with task descriptions, importance scores, and core-task flags |
| `67_select_scale_up_occupations.py` | Selects the scale-up subset based on O*NET task counts and BLS employment |
| `92_select_350_from_full.py` | 350-occupation selection with community-presence-friendly ranking |
| `94_select_all_1016_occupations.py` | Selects all 1,016 ONET occupations after SOC-clustering deduplication; tags `has_tasks` so catch-alls go through a fallback |

### Launchers

| Script | What it does |
|---|---|
| `95_launch_1016_workers.py` | Splits the 1,016-occupation list into chunks and launches one v4 (or v7) worker per chunk in the background; one extra worker handles catch-all SOCs |
| `launch_scale_up.py` | Higher-level orchestrator that runs the community pass first and then layers an authoritative pass over any occupations the community pipeline left short |

### Legacy

| Script | What it does |
|---|---|
| `run_pipeline.sh` | Early scaffolding shell that called the v1-style sequence (`3_generate_qa.py`, `4_evaluate_llms.py`); kept for archive only |

### End-to-end run, in order

**1. O*NET parsing.** Run `1_parse_onet.py` (or the newer `91_parse_onet_full.py`) to produce the task table that drives selection.

**2. Occupation selection.** Use one of `67_select_scale_up_occupations.py` / `92_select_350_from_full.py` / `94_select_all_1016_occupations.py` to write the canonical input CSV (`occupation_title, soc_code, …`).

**3. Parallel generation.** `python 95_launch_1016_workers.py` splits the input across N background workers, each running the canonical generator (`66_auto_source_v4_occupation.py` or `71_auto_source_v7_alttitles.py`). Each worker:

  - Calls a discovery prompt to find Reddit / MetaFilter / Stack Exchange threads relevant to the occupation.
  - For each thread, fetches the top-voted answer and computes upvote-ratio + top/next-ratio metrics.
  - Builds a six-option MCQ where the correct answer paraphrases the top-voted response and the three wrong options are sibling answers from the same thread.
  - Writes incrementally to `bank_<i>.json` and `ckpt_<i>.json`.

**4. Filter pipeline.** `python 99_launch_filter_pipeline.py` runs `74_strip_community_voice.py` → `72_advice_agreement_filter.py` → `98_advice_filter_parallel.py` (parallel) → `44_upvote_consensus_filter.py` in sequence on the merged bank, dropping items that fail any pass.

**5. Backfills.** When pilot or per-approach slots are short, `49_add_reddit_to_pilots.py` and `57_approach2_multi_reddit_backfill.py` re-run only the missing slots with Reddit-only sourcing to keep the source mix balanced.

**6. Combined community + authoritative scale-up.** When the goal is to produce a single bank that mixes both source types, `launch_scale_up.py` is the entry point: it runs the community pass first across all input occupations, then runs the authoritative pass on the same list to top up occupations that the community pass left short. The two passes are deduped on `(source_url, occupation, question)` before producing the merged bank.

---

## Common downstream evaluation

Both pipelines emit the same six-option MCQ format with columns `source_url, occupation, question, option_A, option_B, option_C, option_D, option_E, option_F, correct_answer`. Items from either pipeline can be loaded by the evaluation script (`102_eval_15models.py` in the parent directory) to produce per-model accuracy at three seeds per item.
