"""Turn harness output files into an entry in docs/models.json.
Usage: python harness/ingest.py <run_dir> <model_id> <tag>
  <run_dir>  folder holding summary_<tag>.md and results_<tag>.csv
  <model_id> id in docs/models.json (for example "qwen")
  <tag>      the model name as the harness wrote it in the file names
"""
import csv, json, re, statistics, sys, pathlib

run_dir, model_id, tag = sys.argv[1], sys.argv[2], sys.argv[3]
d = pathlib.Path(run_dir)
summary = (d / f"summary_{tag}.md").read_text()
rows = list(csv.DictReader(open(d / f"results_{tag}.csv")))

def passes(check):
    m = re.search(rf"- {check}: (\d+) / (\d+)", summary)
    return int(m.group(1)) if m else None

f1 = float(re.search(r"F1 ([\d.]+)", summary).group(1))
tp = int(re.search(r"TP (\d+)", summary).group(1))
neg = int(re.search(r"Negation errors[^:]*: (\d+)", summary).group(1))
tps = statistics.median(float(r["tokens_per_s"]) for r in rows if r["tokens_per_s"])
secs = statistics.median(float(r["total_s"]) for r in rows if r["total_s"])

path = pathlib.Path(__file__).resolve().parent.parent / "docs" / "models.json"
models = json.load(open(path))
entry = next(m for m in models if m["id"] == model_id)
entry["tested"] = True
entry["m"] = {"f1": round(f1, 2), "labels": tp, "tps": round(tps, 1), "secs": round(secs, 1),
              "negation": neg, "doctor": passes("doctor"), "numbers": 7 - (7 - passes("invented_numbers"))}
entry["note"] = f"Run {d.name}"
json.dump(models, open(path, "w"), indent=1)
print("updated", model_id, entry["m"])
