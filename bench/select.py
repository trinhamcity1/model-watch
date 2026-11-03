"""Build the Kaggle queue for the HPO benchmark. Usage: python bench/select.py queue.json
Inputs (env): MODELS ('' = all queued, or comma-separated ids), DATASETS ('' = all), MAX_DOCS ('' = config), FORCE ('true').
A model is queued for each dataset that has no result at the current task version (or always, with FORCE).
Size tiers (bench/config.json): up to full_max_params_b = every document; up to quick_max_params_b = a quick check on
quick_docs documents per dataset; above that = never queued, marked server_only with a reason."""
import json, os, sys
from common import CONFIG, load_models, save_models, size_b, tier_for, add_extra_models, migrate_legacy, result_for, is_current

want_models = [x.strip() for x in os.environ.get("MODELS", "").split(",") if x.strip()]
want_ds = [x.strip() for x in os.environ.get("DATASETS", "").split(",") if x.strip()] or list(CONFIG["datasets"])
max_docs = int(os.environ["MAX_DOCS"]) if os.environ.get("MAX_DOCS", "").strip() else None
force = os.environ.get("FORCE", "").lower() == "true"
unknown = [d for d in want_ds if d not in CONFIG["datasets"]]
if unknown:
    sys.exit(f"Unknown dataset(s): {unknown}. Known: {list(CONFIG['datasets'])}")

models = load_models()
migrate_legacy(models)
added = add_extra_models(models)
if added: print("Added hand-picked models:", ", ".join(added))
queue, notes = [], []
for m in models:
    if want_models and m["id"] not in want_models:
        continue
    b, tier = size_b(m), tier_for(m)
    if tier == "server":
        m["server_only"] = True
        m["status_reason"] = f"Large model ({b:g}B): cannot test on the free Kaggle GPUs, server test needed"
        continue
    m.pop("server_only", None)
    if not m.get("ollama_tag"):
        m["status_reason"] = "No download tag in models.json, so the benchmark cannot pull it"
        continue
    todo = [d for d in want_ds if force or not is_current(result_for(m, d), tier)]
    if not todo:
        m.pop("status_reason", None)
        continue
    cap = CONFIG["quick_docs"] if tier == "quick" else None
    n = {d: min([x for x in (max_docs, cap, CONFIG["datasets"][d]["n"]) if x]) for d in todo}
    # Full-tier models go before quick checks; within a tier, models cut off by the time budget last run go first.
    first = 0 if any((result_for(m, d) or {}).get("not_reached") for d in todo) else 1
    queue.append({"id": m["id"], "name": m["name"], "tag": m["ollama_tag"], "role": "candidate", "tier": tier, "datasets": n,
                  "_order": (0 if tier == "full" else 1, first)})
    m["status_reason"] = "In the queue for the benchmark" + (" (quick check, 12 documents per dataset)" if tier == "quick" else "")
    notes.append(f"{m['name']}: {', '.join(todo)}")

queue.sort(key=lambda q: q.pop("_order"))
c = CONFIG["control"]
control = {"id": "_control", "name": "Control (drift check)", "tag": c["tag"], "role": "control", "datasets": {c["dataset"]: c["n"]}}
save_models(models)
json.dump({"control": control, "models": queue}, open(sys.argv[1], "w"), indent=1)
print(f"{len(queue)} models queued" + ("".join("\n  " + x for x in notes) if notes else ""))
print(f"queued={len(queue)}", file=open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a"))
