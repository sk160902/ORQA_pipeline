"""Loads O*NET context per SOC: alternate titles + top-N Core tasks by importance.

Used to anchor discovery search queries beyond the canonical occupation title
(occupations are often discussed online under multiple names).

NOTE: per the call decision, we operate at OCCUPATION level — we don't bind
items to specific task IDs. The top-N tasks are search/discovery flavor only.
"""
from __future__ import annotations
import csv
from . import config

_CACHE: dict | None = None


def _load() -> dict:
    """Returns {soc_code: {"alternate_titles": [...], "key_tasks": [...]}}."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    out: dict = {}

    if config.ONET_ALT_TITLES_TXT.exists():
        with config.ONET_ALT_TITLES_TXT.open(encoding="utf-8") as f:
            next(f, None)
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    continue
                soc, title = parts[0].strip(), parts[1].strip()
                if not soc or not title:
                    continue
                rec = out.setdefault(soc, {"alternate_titles": [], "key_tasks": []})
                if len(rec["alternate_titles"]) < config.ONET_MAX_ALT_TITLES:
                    rec["alternate_titles"].append(title)

    if config.ONET_TASKS_CSV.exists():
        per_soc: dict = {}
        with config.ONET_TASKS_CSV.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                soc = (r.get("onet_code") or "").strip()
                desc = (r.get("task_description") or "").strip()
                if not soc or not desc:
                    continue
                try:
                    score = float(r.get("importance_score") or 0.0)
                except ValueError:
                    score = 0.0
                per_soc.setdefault(soc, []).append((score, desc))
        for soc, tasks in per_soc.items():
            tasks.sort(key=lambda t: t[0], reverse=True)
            rec = out.setdefault(soc, {"alternate_titles": [], "key_tasks": []})
            rec["key_tasks"] = [t[1] for t in tasks[:config.ONET_MAX_TASKS]]

    _CACHE = out
    return out


def get(soc: str) -> dict:
    return _load().get(soc, {"alternate_titles": [], "key_tasks": []})


def context_block(soc: str) -> str:
    """Render a short paragraph for inclusion in discovery prompts."""
    rec = get(soc)
    lines = []
    if rec["alternate_titles"]:
        lines.append("Also known as: " + "; ".join(rec["alternate_titles"]) + ".")
    if rec["key_tasks"]:
        bullets = "\n".join(f"  - {t}" for t in rec["key_tasks"])
        lines.append("Workers in this role typically perform tasks such as:\n" + bullets)
    return "\n".join(lines)


def task_list(soc: str) -> list[str]:
    return get(soc)["key_tasks"]
