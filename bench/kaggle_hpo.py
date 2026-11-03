# Kaggle script for the HPO benchmark. The pipeline fills in the two placeholders below before pushing.
# It builds the RAG-HPO search database once, then runs every queued model through RAG-HPO on the
# selected documents, and leaves predictions, gold files and timings in /kaggle/working/out.
import json, os, pathlib, random, re, shutil, subprocess, threading, time

QUEUE = json.loads(r'''__QUEUE__''')
CONFIG = json.loads(r'''__CONFIG__''')
START = time.time()
BUDGET_S = CONFIG["budget_minutes"] * 60
HARD_S = CONFIG.get("hard_limit_minutes", 660) * 60   # stay under Kaggle's session limit (about 12 hours)
left = lambda: HARD_S - (time.time() - START)
sh = lambda cmd, **kw: subprocess.run(cmd, shell=True, **kw)
OUT = pathlib.Path("/kaggle/working/out"); OUT.mkdir(parents=True, exist_ok=True)
meta = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "runs": [], "setup": {}}
def save_meta(): json.dump(meta, open(OUT / "meta.json", "w"), indent=1)

def clone(repo, ref, dest):
    sh(f"git clone --quiet {repo} {dest}")
    sh(f"git -C {dest} checkout --quiet {ref}")
    return sh(f"git -C {dest} rev-parse HEAD", capture_output=True, text=True).stdout.strip()

# ---------- setup ----------
sh("apt-get update -qq && apt-get install -y -qq zstd")
sh("curl -fsSL https://ollama.com/install.sh | sh")
subprocess.Popen(["ollama", "serve"]); time.sleep(12)
RAG = pathlib.Path("/kaggle/working/RAG-HPO"); CORP = pathlib.Path("/kaggle/working/corpora")
meta["setup"]["rag_hpo_commit"] = clone(CONFIG["rag_hpo"]["repo"], CONFIG["rag_hpo"]["ref"], RAG)
meta["setup"]["corpora_commit"] = clone(CONFIG["corpora"]["repo"], CONFIG["corpora"]["ref"], CORP)
sh(f"pip install -q -r {RAG}/requirements.txt")
sh("pip install -q pronto fastembed sentence-transformers faiss-cpu tiktoken nbconvert openpyxl")
sh("jupyter nbconvert --to script HPO_Vectorization.ipynb RAG-HPO.ipynb", cwd=RAG)
t0 = time.time()
vec = sh("python HPO_Vectorization.py", cwd=RAG, capture_output=True, text=True)
meta["setup"]["vectorization_seconds"] = round(time.time() - t0)
meta["setup"]["vectorization_ok"] = (RAG / "hpo_embedded.npz").exists() and (RAG / "hpo_meta.json").exists()
if not meta["setup"]["vectorization_ok"]:
    meta["setup"]["vectorization_error"] = (vec.stdout + vec.stderr)[-3000:]
    save_meta(); raise SystemExit("HPO search database was not built")
hpo_version = "unknown"
for obo in list(RAG.glob("*.obo")) + list(RAG.glob("**/hp.obo")):
    for line in open(obo, errors="ignore"):
        if line.startswith("data-version:"): hpo_version = line.split(":", 1)[1].strip(); break
    if hpo_version != "unknown": break
meta["setup"]["hpo_version"] = hpo_version
gpus = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", capture_output=True, text=True).stdout.strip().splitlines()
meta["setup"]["gpus"] = gpus
src = open(RAG / "RAG-HPO.py").read()
FLAG = re.search(r"FLAG_FILE\s*=\s*['\"]([^'\"]+)['\"]", src).group(1)
tf = re.search(r"TEMP_FILES\s*=\s*\{[^}]*\}", src, re.S)
TEMPS = re.findall(r"['\"]([^'\"]+\.(?:csv|json|pkl|parquet))['\"]", tf.group(0)) if tf else []

# ---------- documents: the same fixed sample every run ----------
import pandas as pd
def pick_docs(ds, n):
    folder = CORP / CONFIG["datasets"][ds]["path"]
    names = sorted(p.stem for p in (folder / "corpus").iterdir() if p.is_file())
    full = CONFIG["datasets"][ds]["n"]
    chosen = sorted(random.Random(CONFIG["sample_seed"]).sample(names, full)) if full < len(names) else names
    return folder, chosen[:n]

def prepare(ds, n):
    folder, docs = pick_docs(ds, n)
    gdir = OUT / "gold" / ds; gdir.mkdir(parents=True, exist_ok=True)
    rows = []
    texts = {p.stem: p for p in (folder / "corpus").iterdir() if p.is_file()}
    anns = {p.stem: p for p in (folder / "ann").iterdir() if p.is_file()}
    for i, d in enumerate(docs, 1):
        text = texts[d].read_text(errors="ignore")
        if d in anns: shutil.copy(anns[d], gdir / (d + ".ann"))
        rows.append({"Case": i, "clinical_note": " ".join(text.split())})
    csv = RAG / f"input_{ds}_{n}.csv"
    pd.DataFrame(rows).to_csv(csv, index=False)
    index = f"index_{len(docs)}.json"
    json.dump({str(i): d for i, d in enumerate(docs, 1)}, open(gdir / index, "w"))
    return csv.name, len(docs), index

# ---------- GPU memory sampler ----------
class Peak:
    def __init__(self): self.peak, self.stop = 0, False
    def run(self):
        while not self.stop:
            r = sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits", capture_output=True, text=True)
            try: self.peak = max(self.peak, sum(int(x) for x in r.stdout.split()))
            except ValueError: pass
            time.sleep(1)
def gpu_used():
    r = sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits", capture_output=True, text=True)
    try: return sum(int(x) for x in r.stdout.split())
    except ValueError: return None

def run_one(entry, ds, n):
    rec = {"id": entry["id"], "tag": entry["tag"], "role": entry["role"], "dataset": ds}
    if left() < 10 * 60 or (entry["role"] != "control" and time.time() - START > BUDGET_S):
        rec["not_reached"] = True; return rec
    inp, ndocs, index = prepare(ds, n)
    rec["n_docs"], rec["gold_index"] = ndocs, index
    json.dump({"api_key": "none", "base_url": "http://localhost:11434/v1/chat/completions", "model_name": entry["tag"],
               "max_tokens_per_day": 100000000, "max_queries_per_minute": 1000,
               "temperature": CONFIG["temperature"]}, open(RAG / FLAG, "w"))
    for t in TEMPS:
        if (RAG / t).exists(): (RAG / t).unlink()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", entry["id"]); outname = f"pred_{slug}_{ds}.csv"
    if (RAG / outname).exists(): (RAG / outname).unlink()
    idle = gpu_used(); p = Peak(); th = threading.Thread(target=p.run, daemon=True); th.start()
    t0 = time.time()
    try:
        r = subprocess.run(["python", "RAG-HPO.py"], cwd=RAG, input=f"no\n{inp}\nsave\n{outname}\n", text=True,
                           capture_output=True, timeout=max(60, min(CONFIG["per_dataset_timeout_minutes"] * 60, left() - 5 * 60)))
        rec["log_tail"] = (r.stdout + r.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        rec["timeout"] = True
        if left() < 6 * 60: rec["not_reached"] = True; rec.pop("timeout")   # cut by the run's deadline, not too slow
    rec["seconds"] = round(time.time() - t0, 1)
    p.stop = True; th.join(timeout=5)
    rec["gpu_idle_mib"], rec["gpu_peak_mib"] = idle, p.peak
    pdir = OUT / "pred"; pdir.mkdir(exist_ok=True)
    if (RAG / outname).exists():
        shutil.copy(RAG / outname, pdir / outname); rec["pred_file"] = outname
    return rec

def run_entry(entry):
    if time.time() - START > BUDGET_S and entry["role"] != "control":
        for ds in entry["datasets"]:
            meta["runs"].append({"id": entry["id"], "tag": entry["tag"], "role": entry["role"], "dataset": ds, "not_reached": True})
        save_meta(); return
    try:
        pulled = sh(f"ollama pull {entry['tag']}", timeout=CONFIG["pull_timeout_minutes"] * 60).returncode == 0
    except subprocess.TimeoutExpired:
        pulled = False
    for ds, n in entry["datasets"].items():
        if not pulled:
            meta["runs"].append({"id": entry["id"], "tag": entry["tag"], "role": entry["role"], "dataset": ds, "pull_failed": True})
        else:
            meta["runs"].append(run_one(entry, ds, n))
        save_meta()
    sh(f"ollama rm {entry['tag']}")

run_entry(QUEUE["control"])
for entry in QUEUE["models"]:
    run_entry(entry)
meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
save_meta()
print("done", json.dumps(meta)[:2000])
