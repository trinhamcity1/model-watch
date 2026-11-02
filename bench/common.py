"""Shared helpers for the HPO benchmark: reading models.json, sizes, and the legacy archive."""
import json, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.load(open(ROOT / "bench" / "config.json"))
MODELS_PATH = ROOT / "docs" / "models.json"
LEGACY_KEYS = ("m", "f1", "labels", "doctor", "numbers")

def load_models():
    return json.load(open(MODELS_PATH))

def save_models(models):
    json.dump(models, open(MODELS_PATH, "w"), indent=1)

def size_b(m):
    """Total parameters in billions from fields like '30B (3.5B active)' or params_b; None if unknown."""
    if isinstance(m.get("params_b"), (int, float)):
        return float(m["params_b"])
    s = re.match(r"\s*([\d.]+)\s*B", str(m.get("size", "")), re.I)
    return float(s.group(1)) if s else None

def migrate_legacy(models):
    """Move the old 18-question scores into m['legacy'] once, so the page can show them as an archive."""
    changed = False
    for m in models:
        old = [k for k in ("m", "tested", "failed", "note", "reason") if k in m]
        if old and "legacy" not in m:
            m["legacy"] = {"test": "18-question synthetic test (retired)", "scores": m.get("m"),
                           "tested": m.get("tested", False), "failed": m.get("failed", False),
                           "note": m.get("note"), "reason": m.get("reason")}
        if old:
            for k in old: m.pop(k)
            changed = True
    return changed

def result_for(m, ds):
    return ((m.get("results") or {}).get(CONFIG["task"]) or {}).get(ds)

def is_current(res):
    return bool(res) and res.get("task_version") == CONFIG["task_version"]
