"""Score the Kaggle output and write results into docs/models.json and docs/run_report.json.
Usage: python bench/collect.py <out_dir> <queue.json>. Exit 0 always; merge_ok in run_report.json decides the merge."""
import datetime, json, pathlib, shutil, sys
from common import ROOT, CONFIG, load_models, save_models, migrate_legacy
from score import read_gold, read_pred, score

out_dir, queue = pathlib.Path(sys.argv[1]), json.load(open(sys.argv[2]))
found = sorted(out_dir.rglob("meta.json"))
if not found:
    files = [str(f.relative_to(out_dir)) for f in out_dir.rglob("*") if f.is_file()][:40]
    raise SystemExit("meta.json not found in the Kaggle output. Files returned: " + ", ".join(files))
out_dir = found[0].parent
meta = json.load(open(out_dir / "meta.json"))
setup = meta.get("setup", {})
today = datetime.date.today().isoformat()
models = load_models(); migrate_legacy(models)
by_id = {m["id"]: m for m in models}
task, ver = CONFIG["task"], CONFIG["task_version"]

def gold_for(ds, index):
    gdir = out_dir / "gold" / ds
    idx = json.load(open(gdir / index)) if (gdir / index).exists() else {}
    return {case: read_gold(gdir / (doc + ".ann")) if (gdir / (doc + ".ann")).exists() else (set(), set())
            for case, doc in idx.items()}

report = {"date": today, "gpus": setup.get("gpus", []), "hpo_version": setup.get("hpo_version"),
          "rag_hpo_commit": setup.get("rag_hpo_commit"), "corpora_commit": setup.get("corpora_commit"),
          "task": task, "task_version": ver, "results": [], "control": None, "merge_ok": False, "problems": []}
if not setup.get("gpus"): report["problems"].append("no GPU detected")
if not setup.get("vectorization_ok", True): report["problems"].append("HPO search database was not built")

for run in meta.get("runs", []):
    ds = run["dataset"]
    row = {"id": run["id"], "tag": run["tag"], "role": run["role"], "dataset": ds, "ok": False, "reason": None}
    if run.get("not_reached"):
        row["reason"] = "not reached: the run's time budget ran out, tried first next run"; row["not_reached"] = True
    elif run.get("pull_failed"):
        row["reason"] = "model download failed (check the tag)"
    elif run.get("timeout"):
        row["reason"] = f"timed out after {CONFIG['per_dataset_timeout_minutes']} minutes (too slow on the free GPUs)"
    elif not run.get("pred_file"):
        tail = (run.get("log_tail") or "").strip().splitlines()
        row["reason"] = "RAG-HPO produced no output" + (f" (last log line: {tail[-1][:160]})" if tail else "")
    else:
        gold = gold_for(ds, run.get("gold_index", "index.json"))
        pred, cats = read_pred(out_dir / "pred" / run["pred_file"])
        metrics, per_doc = score(gold, pred)
        n = metrics["n_docs"] or 1
        metrics.update({"seconds": run.get("seconds"), "secs_per_doc": round(run.get("seconds", 0) / n, 1),
                        "docs_per_min": round(60 * n / run["seconds"], 2) if run.get("seconds") else None,
                        "gpu_peak_gib": round(run["gpu_peak_mib"] / 1024, 1) if run.get("gpu_peak_mib") else None,
                        "gpu_idle_gib": round(run["gpu_idle_mib"] / 1024, 1) if run.get("gpu_idle_mib") is not None else None,
                        "categories": cats})
        row.update({"ok": True, "metrics": metrics})
        if metrics["tp"] + metrics["fp"] == 0:
            row.update({"ok": False, "reason": "no HPO codes returned for any document"})
    report["results"].append(row)
    if run["role"] == "control":
        report["control"] = row; continue
    m = by_id.get(run["id"])
    if m is None: continue
    res = {"task_version": ver, "date": today, "ok": row["ok"], "reason": row["reason"],
           "gpus": setup.get("gpus"), "hpo_version": setup.get("hpo_version"), "rag_hpo_commit": setup.get("rag_hpo_commit")}
    if row.get("metrics"): res["metrics"] = row["metrics"]
    if row.get("not_reached"):
        res = {"task_version": None, "not_reached": True, "date": today, "reason": row["reason"]}  # not current, so re-queued
    m.setdefault("results", {}).setdefault(task, {})[ds] = res
    m.pop("status_reason", None)

# Drift check: the control model's F1 must stay close to its last value.
ctrl_path = ROOT / "docs" / "control.json"
prev = json.load(open(ctrl_path)) if ctrl_path.exists() else None
ctrl = report["control"]
if not ctrl or not ctrl["ok"]:
    report["problems"].append("control model did not produce results: " + ((ctrl or {}).get("reason") or "missing"))
else:
    f1 = ctrl["metrics"]["f1"]
    if prev and prev.get("task_version") == ver and abs(f1 - prev["f1"]) > CONFIG["control"]["max_f1_drift"]:
        report["problems"].append(f"control F1 moved from {prev['f1']} to {f1}; check the environment before trusting this run")
    else:
        json.dump({"task_version": ver, "f1": f1, "date": today, "tag": ctrl["tag"]}, open(ctrl_path, "w"), indent=1)

done = [r for r in report["results"] if r["role"] != "control" and not r.get("not_reached")]
report["not_reached"] = sorted({r["id"] for r in report["results"] if r.get("not_reached")})
report["merge_ok"] = not report["problems"] and (any(r["ok"] for r in done) or not done)
save_models(models)
json.dump(report, open(ROOT / "docs" / "run_report.json", "w"), indent=1)
dest = ROOT / "runs" / f"{today}-hpo"; dest.mkdir(parents=True, exist_ok=True)
shutil.copy(out_dir / "meta.json", dest / "meta.json")
if (out_dir / "pred").exists(): shutil.copytree(out_dir / "pred", dest / "pred", dirs_exist_ok=True)
print(json.dumps({"merge_ok": report["merge_ok"], "problems": report["problems"], "not_reached": report["not_reached"],
                  "results": [(r["id"], r["dataset"], r["ok"], (r.get("metrics") or {}).get("f1"), r["reason"]) for r in report["results"]]}, indent=1))
print(f"not_reached={len(report['not_reached'])}\ncompleted={sum(1 for r in done if r['ok'])}",
      file=open(__import__('os').environ.get("GITHUB_OUTPUT", "/dev/null"), "a"))
