"""Read the files the Kaggle run produced, apply checks, update docs/models.json and write docs/run_report.json.
Usage: python pipeline/collect.py <out_dir> <queue.json>
Exit code 0 always; the checks decide the `merge_ok` value written to run_report.json."""
import csv, datetime, json, pathlib, re, statistics, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
out_dir, queue_path = pathlib.Path(sys.argv[1]), sys.argv[2]
# Kaggle may return the files inside a subfolder, so look for meta.json anywhere under out_dir.
found = sorted(out_dir.rglob("meta.json"))
if not found:
    files = [str(f.relative_to(out_dir)) for f in out_dir.rglob("*") if f.is_file()][:40]
    raise SystemExit("meta.json not found in the Kaggle output, so the notebook probably stopped early. Files returned: " + ", ".join(files))
out_dir = found[0].parent
queue = json.load(open(queue_path))
meta = json.load(open(out_dir / "meta.json"))
models = json.load(open(ROOT / "docs" / "models.json"))
today = datetime.date.today().isoformat()
slug = lambda tag: re.sub(r"[^A-Za-z0-9]", "_", tag)

def passes(text, check):
    m = re.search(rf"- {check}: (\d+) / (\d+)", text)
    return int(m.group(1)) if m else None

def read_run(tag):
    s = slug(tag)
    sp, rp = out_dir / f"summary_{s}.md", out_dir / f"results_{s}.csv"
    if not sp.exists() or not rp.exists():
        return None, ["no result files"]
    text, rows = sp.read_text(), list(csv.DictReader(open(rp)))
    reasons = []
    cut = sum(1 for r in rows if r.get("done_reason") == "length")
    pf = int(re.search(r"JSON parse failures: (\d+)", text).group(1))
    if len(rows) != 18: reasons.append(f"{len(rows)} of 18 calls finished")
    if cut: reasons.append(f"{cut} answers cut off by the length limit")
    if pf: reasons.append(f"{pf} JSON parse failures")
    m = {"f1": round(float(re.search(r"F1 ([\d.]+)", text).group(1)), 2),
         "labels": int(re.search(r"TP (\d+)", text).group(1)),
         "tps": round(statistics.median(float(r["tokens_per_s"]) for r in rows if r["tokens_per_s"]), 1),
         "secs": round(statistics.median(float(r["total_s"]) for r in rows if r["total_s"]), 1),
         "negation": int(re.search(r"Negation errors[^:]*: (\d+)", text).group(1)),
         "doctor": passes(text, "doctor"), "numbers": passes(text, "invented_numbers")}
    return m, reasons

report = {"date": today, "gpus": meta.get("gpus", []), "results": [], "merge_ok": False}
if not meta.get("gpus"):
    report["problem"] = "no GPU detected, so speeds and sizes are not comparable"
by_id = {e["id"]: e for e in models}
pulled = {x["id"]: x["pull_ok"] for x in meta["models"]}
for q in queue:
    row = {"id": q["id"], "name": q["name"], "role": q.get("role"), "ok": False, "reasons": []}
    if not pulled.get(q["id"], False):
        row["reasons"] = ["model download failed (check the tag)"]
    else:
        m, reasons = read_run(q["ollama_tag"])
        row["reasons"] = reasons
        if m:
            row["metrics"] = m
            prev = by_id.get(q["id"], {}).get("m")
            if q.get("role") == "baseline" and prev and abs(m["f1"] - prev["f1"]) > 0.10:
                row["reasons"].append(f"baseline F1 moved from {prev['f1']} to {m['f1']}")
        row["ok"] = m is not None and not row["reasons"]
    entry = by_id.get(q["id"])
    if entry is None:
        entry = {k: q[k] for k in ("id", "name", "size", "licence", "ollama_tag") if k in q}
        entry.update({"hf": q.get("hf"), "reviewed": q.get("reviewed", False), "source": q.get("source", "scout"), "tested": False, "added": today})
        models.append(entry); by_id[q["id"]] = entry
    if row["ok"]:
        entry["m"], entry["tested"] = row["metrics"], True
        entry.pop("failed", None)
        entry["note"] = f"Run {today} on {', '.join(meta['gpus']) or 'unknown GPU'}"
    else:
        if q.get("role") != "baseline": entry["failed"] = True   # not retried automatically; delete this flag to retry
        entry["note"] = f"Run {today} failed checks: " + "; ".join(row["reasons"])
    report["results"].append(row)

base_rows = [r for r in report["results"] if r["role"] == "baseline"]
report["merge_ok"] = bool(meta.get("gpus")) and bool(base_rows) and all(r["ok"] for r in base_rows) and any(r["ok"] for r in report["results"] if r["role"] != "baseline")
json.dump(models, open(ROOT / "docs" / "models.json", "w"), indent=1)
json.dump(report, open(ROOT / "docs" / "run_report.json", "w"), indent=1)
(ROOT / "runs" / today).mkdir(parents=True, exist_ok=True)
for f in [x for x in out_dir.iterdir() if x.is_file()]:
    (ROOT / "runs" / today / f.name).write_bytes(f.read_bytes())
print(json.dumps({"merge_ok": report["merge_ok"], "results": [(r["name"], r["ok"], r["reasons"]) for r in report["results"]]}, indent=1))
