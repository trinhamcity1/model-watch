#!/usr/bin/env python3
"""
Model Watch, script 3: check enhanced setups against their baseline, for accuracy AND consistency.

An "enhanced setup" is a model plus a prompt set that differs from the baseline: refined prompts (for example from
the error-feedback loop, added later), an AWQ 4-bit version of a model, or RAG-HPO setting changes packaged as a
prompt set. This script does not create enhancements; it measures them, using script 2 (run_benchmark.py, same folder):

  1. Accuracy on the EVALUATION split only (script 2 `run --docs eval`). Enhancements are tuned on the tuning
     split; the split is fixed in splits.json before tuning starts, so tuning documents never reach these scores.
  2. Repeatability and reproducibility (script 2 `consistency`) on the same document sample and settings for the
     baseline and the enhancement. When they share a model, their prompt sets are run together, so the enhanced
     prompts also count as a reproducibility condition.
  3. A comparison: an enhancement that raises F1 but makes the model less consistent is reported as such, not
     adopted silently.

Commands (from the bundle folder, with the same Python as script 2):
  env/bin/python run_enhancements.py example            # writes enhancements.example.json to start from
  env/bin/python run_enhancements.py check  enhancements.json
  env/bin/python run_enhancements.py run    enhancements.json   # long: start inside tmux
  env/bin/python run_enhancements.py compare enhancements.json  # table + CSV; safe to rerun at any time

"Less consistent" = the enhancement's median score is below the lower end of the baseline's 95% interval, for
semantic repeatability, internal repeatability or code-set agreement. "More consistent" = above the upper end.
"""
import argparse, csv, json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT2 = HERE / "run_benchmark.py"
sys.path.insert(0, str(HERE))
import run_benchmark as rb   # noqa: E402  (shares the bundle paths, model list, prompt sets and split)

EXAMPLE = {
    "run_id": "enhancements-1",
    "mode": "as-is",
    "baseline": {"model": "m42-health/Llama3-Med42-8B", "prompt_set": "rag-hpo"},
    "enhancements": [
        {"name": "refined prompts v1", "model": "m42-health/Llama3-Med42-8B", "prompt_set": "refined-v1"},
        {"name": "AWQ 4-bit", "model": "<the AWQ repo name as listed by `run_benchmark.py check`>", "prompt_set": "rag-hpo",
         "baseline": {"model": "m42-health/Llama3-Med42-8B", "prompt_set": "rag-hpo"}},
    ],
    "consistency": {"runs": 20, "sample": 60, "concurrency": 8, "temperature": 0.5, "top_k": 30, "logprobs_k": 30, "seed": 1000},
    "accuracy": {"concurrency": 8},
}
CONSISTENCY_KEYS = ("semantic_repeatability", "internal_repeatability", "code_agreement")


def load(path):
    cfg = json.loads(Path(path).read_text())
    for e in cfg["enhancements"]:
        e.setdefault("baseline", cfg["baseline"])
    return cfg

def setups(cfg):
    """Every distinct (model, prompt set) to measure: baselines and enhancements."""
    out = []
    for e in cfg["enhancements"]:
        for s in (e["baseline"], e):
            key = (s["model"], s["prompt_set"])
            if key not in out: out.append(key)
    return out

def resolve(name):
    """The bundle's full repo name for a model given in the config."""
    models = rb.available_models()
    if name in models: return name
    hits = [r for r in models if name.lower() in r.lower()]
    if len(hits) != 1: rb.die(f"model '{name}': {'no match' if not hits else 'several matches ' + str(hits)} in the bundle")
    return hits[0]

def sh(args):
    rb.log("$ " + " ".join(args))
    r = subprocess.run([sys.executable, str(SCRIPT2)] + args)
    if r.returncode: rb.die(f"script 2 stopped with code {r.returncode}")


def cmd_example(a):
    p = HERE / "enhancements.example.json"
    p.write_text(json.dumps(EXAMPLE, indent=1)); rb.log(f"Wrote {p.name}: edit the models and prompt sets, then `check` it.")

def cmd_check(a):
    cfg = load(a.config); ok = True
    for model, ps in setups(cfg):
        try: rb.log(f"model ok: {resolve(model)}")
        except SystemExit as e: ok = False; rb.log(str(e))
        if not rb.prompt_file(ps).exists(): ok = False; rb.log(f"MISSING prompt set '{ps}': {rb.prompt_file(ps)}")
    sp = rb.get_splits(create=False)
    if not sp: ok = False; rb.log("No splits.json yet: run `run_benchmark.py splits` BEFORE any tuning, then tune on the tuning split only")
    else: rb.log(f"Fixed split from {sp['created']}: accuracy uses the evaluation documents only")
    rb.log("CHECK PASSED" if ok else "CHECK FOUND PROBLEMS"); return 0 if ok else 1

def cmd_run(a):
    cfg = load(a.config)
    if cmd_check(a): rb.die("fix the problems above first")
    rid, mode, c, acc = cfg["run_id"], cfg.get("mode", "as-is"), cfg.get("consistency", {}), cfg.get("accuracy", {})
    pairs = [(resolve(m), ps) for m, ps in setups(cfg)]
    for model, ps in pairs:          # 1. accuracy on the evaluation split
        sh(["run", "--models", model, "--docs", "eval", "--prompt-set", ps, "--mode", mode, "--run-id", rid,
            "--concurrency", str(acc.get("concurrency", 8))])
    by_model = {}
    for model, ps in pairs: by_model.setdefault(model, []).append(ps)
    for model, sets in by_model.items():   # 2. consistency, prompt sets of the same model together
        sh(["consistency", "--models", model, "--prompt-sets", ",".join(sets), "--mode", mode, "--run-id", rid + "_consistency",
            "--runs", str(c.get("runs", 20)), "--sample", str(c.get("sample", 60)), "--concurrency", str(c.get("concurrency", 8)),
            "--temperature", str(c.get("temperature", 0.5)), "--top-k", str(c.get("top_k", 30)),
            "--logprobs-k", str(c.get("logprobs_k", 30)), "--seed", str(c.get("seed", 1000))])
    cmd_compare(a)

def accuracy(rid, model, ps, mode):
    """F1, precision and recall per dataset on the evaluation split, and their mean over datasets."""
    out = {}
    for ds, d in rb.HPO_DATASETS.items():
        if d.get("no_split"): continue   # GeneReviews (10 docs) is a PhenoSS check, not part of the F1 comparison
        # script 2 names result folders <mode>[_thinking-<on|off>][_prompts-<set>]
        def wanted(name):
            if not name.startswith(mode): return False
            return "_prompts-" not in name if ps == rb.DEFAULT_PROMPT_SET else name.endswith(f"_prompts-{ps}")
        hits = [h for h in (rb.RESULTS / rid / rb.slug(model) / "hpo" / f"{ds}_eval").glob("*/result.json") if wanted(h.parent.name)]
        if hits:
            m = json.loads(hits[0].read_text()).get("metrics") or {}
            out[ds] = {k: m.get(k) for k in ("f1", "precision", "recall")}
    vals = [v["f1"] for v in out.values() if v.get("f1") is not None]
    out["mean_f1"] = round(sum(vals) / len(vals), 4) if vals else None
    return out

def consistency(rid, model, ps):
    p = rb.RESULTS / (rid + "_consistency") / "consistency" / rb.slug(model) / "consistency.json"
    if not p.exists(): return None
    return (json.loads(p.read_text()).get("by_prompt_set") or {}).get(ps)

def compare_scores(base, enh):
    """Per consistency score: lower / similar / higher than the baseline's 95% interval."""
    out = {}
    for k in CONSISTENCY_KEYS:
        b, e = (base or {}).get(k) or {}, (enh or {}).get(k) or {}
        if not b.get("ci95") or e.get("median") is None: out[k] = "no data"; continue
        lo, hi = b["ci95"]
        out[k] = "lower" if e["median"] < lo else "higher" if e["median"] > hi else "similar"
    return out

def cmd_compare(a):
    cfg = load(a.config); rid, mode = cfg["run_id"], cfg.get("mode", "as-is")
    rows = []
    for e in cfg["enhancements"]:
        bm, em = resolve(e["baseline"]["model"]), resolve(e["model"])
        bacc, eacc = accuracy(rid, bm, e["baseline"]["prompt_set"], mode), accuracy(rid, em, e["prompt_set"], mode)
        bcon, econ = consistency(rid, bm, e["baseline"]["prompt_set"]), consistency(rid, em, e["prompt_set"])
        verdicts = compare_scores(bcon, econ)
        d_f1 = None if bacc["mean_f1"] is None or eacc["mean_f1"] is None else round(eacc["mean_f1"] - bacc["mean_f1"], 4)
        if "no data" in verdicts.values() or d_f1 is None: decision = "incomplete: run or rerun `run`"
        elif "lower" in verdicts.values(): decision = "less consistent: review before adopting" + (" (F1 higher)" if d_f1 > 0 else "")
        elif d_f1 > 0: decision = "candidate to keep: higher F1, consistency not worse"
        else: decision = "no gain"
        med = lambda c, k: ((c or {}).get(k) or {}).get("median")
        rows.append({"enhancement": e["name"], "baseline": f"{bm} / {e['baseline']['prompt_set']}", "enhanced": f"{em} / {e['prompt_set']}",
                     "f1_eval_baseline": bacc["mean_f1"], "f1_eval_enhanced": eacc["mean_f1"], "f1_change": d_f1,
                     **{f"{k}_baseline": med(bcon, k) for k in CONSISTENCY_KEYS},
                     **{f"{k}_enhanced": med(econ, k) for k in CONSISTENCY_KEYS},
                     **{f"{k}_vs_baseline": v for k, v in verdicts.items()},
                     "decision": decision, "accuracy_detail": {"baseline": bacc, "enhanced": eacc}})
    out = rb.RESULTS / rid / "enhancement_comparison.json"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"run_id": rid, "mode": mode, "split": "evaluation documents only", "rows": rows}, indent=1))
    flat = [{k: v for k, v in r.items() if k != "accuracy_detail"} for r in rows]
    if flat:
        with open(out.with_suffix(".csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(flat[0])); w.writeheader(); w.writerows(flat)
    for r in rows:
        rb.log(f"{r['enhancement']}: F1 {r['f1_eval_baseline']} -> {r['f1_eval_enhanced']} ({r['f1_change']:+})" if r["f1_change"] is not None
               else f"{r['enhancement']}: F1 not available")
        rb.log("    " + ", ".join(f"{k.replace('_', ' ')} {r[k + '_baseline']} -> {r[k + '_enhanced']} ({r[k + '_vs_baseline']})" for k in CONSISTENCY_KEYS))
        rb.log(f"    -> {r['decision']}")
    rb.log(f"Wrote {out.relative_to(rb.BUNDLE)} and its .csv (scores only; safe to take off the server).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("example")
    for n in ("check", "run", "compare"):
        sub.add_parser(n).add_argument("config")
    a = ap.parse_args()
    if a.cmd == "example": return cmd_example(a)
    if a.cmd == "check": sys.exit(cmd_check(a))
    if a.cmd == "run": return cmd_run(a)
    if a.cmd == "compare": return cmd_compare(a)

if __name__ == "__main__":
    main()
