"""Decide which models the Kaggle run tests. Writes queue.json (path given as argv[1]).
Always: the baseline (so every run has a same-hardware reference).
Plus: reviewed models with an ollama_tag that are not tested yet.
Plus: up to MAX_SCOUT (default 40, i.e. everything that qualifies) unreviewed scout candidates that have a suggested tag and fit the free GPUs."""
import json, os, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_SCOUT, MAX_PARAMS_B = int(os.environ.get("MAX_SCOUT", "40")), int(os.environ.get("MAX_PARAMS_B", "40"))
models = json.load(open(ROOT / "docs" / "models.json"))
known_hf = {m.get("hf") for m in models if m.get("hf")}
known_names = {re.sub(r"[^a-z0-9]", "", m["name"].lower()) for m in models}

baseline_id = next((m["id"] for m in models if m.get("baseline")),
                   next((m["id"] for m in models if m.get("tested") and m.get("ollama_tag")), None))
queue = []
for m in models:
    if m["id"] == baseline_id and m.get("ollama_tag"):
        queue.append({**m, "role": "baseline"})
    elif m.get("ollama_tag") and not m.get("tested") and not m.get("failed"):
        queue.append({**m, "role": "reviewed"})

scout_added = 0
cands_doc = json.load(open(ROOT / "docs" / "candidates.json"))
for c in cands_doc["candidates"]:
    short = re.sub(r"[^a-z0-9]", "", c["id"].split("/")[1].lower())
    if c["id"] in known_hf or short in known_names:
        c["status"], c["reason"] = "in_list", None
    elif c["params_b"] > MAX_PARAMS_B:
        c["status"], c["reason"] = "too_large", f"Too large for the free Kaggle GPUs ({c['params_b']}B; the two T4s hold about {MAX_PARAMS_B}B at 4-bit)"
    elif not c.get("suggested_tag"):
        c["status"], c["reason"] = "no_gguf", c.get("tag_note") or "No GGUF version found"
    elif scout_added >= MAX_SCOUT:
        c["status"], c["reason"] = "over_limit", f"Over this run's cap of {MAX_SCOUT} new models; will be tried next run"
    else:
        queue.append({"id": re.sub(r"[^a-z0-9]+", "-", c["id"].lower()).strip("-"), "name": c["id"].split("/")[1],
                      "size": f"{c['params_b']}B", "licence": c["licence"], "ollama_tag": c["suggested_tag"],
                      "hf": c["id"], "tested": False, "reviewed": False, "source": "scout", "role": "scout"})
        scout_added += 1
        c["status"], c["reason"] = "queued", None
cands_doc["limits"] = {"max_params_b": MAX_PARAMS_B, "max_new_per_run": MAX_SCOUT}
json.dump(cands_doc, open(ROOT / "docs" / "candidates.json", "w"), indent=1)

json.dump(queue, open(sys.argv[1], "w"), indent=1)
print("queue:", [(q["name"], q["role"]) for q in queue])
