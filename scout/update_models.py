"""After the scout: add found models to docs/models.json and give every candidate a status and reason.
- in_list: already in models.json (a missing tag is filled from the scout's GGUF lookup)
- added: has a GGUF tag and fits the free Kaggle GPUs; the benchmark will test it
- server_only: larger than the Kaggle limit; listed for a server test
- no_gguf: no downloadable 4-bit version found; checked again next week
Models from Texas-listed developers never reach candidates (scout.py only counts them), and every model
already in models.json is rechecked here: a blocked one gets m["blocked"] and is never queued."""
import datetime, json, pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
from common import CONFIG, load_models, save_models, migrate_legacy
from compliance import check_model

norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
models = load_models(); migrate_legacy(models)
by_hf = {m["hf"]: m for m in models if m.get("hf")}
by_name = {norm(m["name"]): m for m in models}
checked = []
for m in models:   # recheck the whole list every week: the Texas list and model metadata can change
    ok, why = check_model(m)
    if not ok and why.startswith("blocked"): continue   # Texas list: removed from the list and the site
    if ok: m.pop("blocked", None)
    else: m["blocked"] = why
    checked.append(m)
models[:] = checked
cands = json.load(open(ROOT / "docs" / "candidates.json"))
limit, today, added = CONFIG["kaggle_max_params_b"], datetime.date.today().isoformat(), []
for c in cands["candidates"]:
    short = c["id"].split("/")[1]
    m = by_hf.get(c["id"]) or by_name.get(norm(short))
    if m:
        if not m.get("ollama_tag") and c.get("suggested_tag"):
            m["ollama_tag"] = c["suggested_tag"]
        m.setdefault("hf", c["id"])
        c["status"], c["reason"] = "in_list", None
        continue
    big = c["params_b"] > limit
    if not big and not c.get("suggested_tag"):
        c["status"], c["reason"] = "no_gguf", c.get("tag_note") or "No GGUF version found; checked again next week"
        continue
    entry = {"id": re.sub(r"[^a-z0-9]+", "-", c["id"].lower()).strip("-"), "name": short, "size": f"{c['params_b']}B",
             "params_b": c["params_b"], "licence": c["licence"], "ollama_tag": c.get("suggested_tag"), "hf": c["id"],
             "source": "scout", "added": today}
    if big:
        entry["server_only"] = True
        entry["status_reason"] = f"Large model ({c['params_b']}B): cannot test on the free Kaggle GPUs, server test needed"
        c["status"], c["reason"] = "server_only", entry["status_reason"]
    else:
        c["status"], c["reason"] = "added", "Added to the list; the benchmark tests it on the next run"
    ok, why = check_model(entry)
    if not ok and why.startswith("blocked"):
        c["status"], c["reason"] = "blocked", why
        continue
    if not ok:
        entry["blocked"], entry["status_reason"] = why, "Not screened: " + why
        entry.pop("server_only", None)
        c["status"], c["reason"] = "blocked", why
    models.append(entry); by_hf[c["id"]] = entry; added.append(short)
cands["limits"] = {"kaggle_max_params_b": limit}
json.dump(cands, open(ROOT / "docs" / "candidates.json", "w"), indent=1)
save_models(models)
print(f"added {len(added)}: {', '.join(added) or 'none'}; blocked in the list: {sum(1 for m in models if m.get('blocked'))}")
