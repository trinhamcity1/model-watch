#!/usr/bin/env python3
"""
Model Watch, script 2: run the benchmark on the offline lab server (script 3, run_enhancements.py, builds on it).

Runs from the bundle made by build_bundle.py, with no internet. One model at a time: vLLM serves the
model on a local port, a small proxy applies the run mode (as-is / assisted) and logs every reply,
and each task module runs its pipeline and scores the output against expert annotations.

Commands (run from inside the bundle folder, over SSH):
  python3 run_benchmark.py check                 # bundle, GPUs, disk, offline settings
  python3 run_benchmark.py install               # Python env from wheels/ (skip inside the vLLM container)
  env/bin/python run_benchmark.py smoke          # one model, one document: proves the setup works
  env/bin/python run_benchmark.py run --models core --tasks hpo --mode both        # full datasets by default
  env/bin/python run_benchmark.py splits         # show (or create once) the fixed tuning / evaluation split
  env/bin/python run_benchmark.py consistency --models <shortlist>   # repeatability and reproducibility test
  env/bin/python run_benchmark.py export         # small archive of scores and timings to take off the server

Scoring rules: one fixed HPO release (data/hp.obo, recorded with every result). Retired HPO codes are mapped to
that release in model outputs and reference annotations alike before scoring; the exact match on the original
codes is kept only for comparison with published numbers. As-is and assisted results are always separate rows.

Long runs: start them inside tmux (or with nohup) so they keep going if SSH drops. A rerun skips
model/task/dataset/mode combinations that already have a result, so an interrupted run resumes.

What leaves the server: only `export`'s archive (scores, timings, settings). Raw model replies and
predictions stay in results/ on the server.
"""
import argparse, csv, datetime, hashlib, json, os, re, shutil, signal, socket, subprocess, sys, tarfile, threading, time
import urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
RESULTS = BUNDLE / "results"
WORK = BUNDLE / "work"
ENV = BUNDLE / "env"

# Offline: nothing may try the internet. Set before any library is imported.
OFFLINE_ENV = {
    "HF_HOME": str(BUNDLE / "hf_cache"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1", "TIKTOKEN_CACHE_DIR": str(BUNDLE / "tiktoken_cache"),
    "VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1",
}
os.environ.update(OFFLINE_ENV)


def log(msg): print(time.strftime("%H:%M:%S"), msg, flush=True)

def smi(*query):
    """nvidia-smi output, or None when it is missing or fails (reported, never a crash)."""
    try:
        r = subprocess.run(["nvidia-smi", *query], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
def die(msg): sys.exit(f"ERROR: {msg}")
def load_json(p): return json.loads(Path(p).read_text())


# =============================================================================================
# Bundle contents
# =============================================================================================
def manifest():
    p = BUNDLE / "MANIFEST.json"
    if not p.exists(): die("MANIFEST.json not found: run this from inside the bundle folder")
    return load_json(p)

# Task settings, built in (the bundle no longer carries the Model Watch repo). "full" = every document in the dataset;
# "test24" = the 24 hand-picked documents used on Kaggle, kept so server results can be compared with Kaggle runs.
TASK_VERSION = "server-1"
HPO_DATASETS = {
    "id68": {"label": "ID-68", "path": "phenobert/data/ID-68",
             "test24": ["09DG00835", "12DG2311", "13DG0911", "13DG1665", "14DG1743", "15DG0115",
                        "15DG0299", "15DG0749", "16DG0971", "17DG0762", "17DG0769", "DD_91704"]},
    "gscplus": {"label": "GSC+", "path": "phenobert/data/GSC+",
                "test24": ["10771486", "11748306", "16401744", "2466440", "3134615", "3688021",
                           "7485365", "8576558", "8929945", "9182785", "9207339", "9546330"]},
    # 10 GeneReviews case summaries, one disease chapter each (NBK id), so the true disease is known:
    # used for the PhenoSS check. Too small to split: every document is an evaluation document.
    "genereviews": {"label": "GeneReviews", "path": "phenobert/data/GeneReviews", "test24": [], "no_split": True},
}
TEMPERATURE = 0.0          # baseline runs; the consistency test samples (see `consistency --temperature`)

# Prompt sets for RAG-HPO. "rag-hpo" = RAG-HPO's own system_prompts.json, unchanged. Variants live in
# prompts/<name>/system_prompts.json (same two keys), written and frozen before any run. Empty slots are skipped.
PROMPTS = BUNDLE / "prompts"
PROMPT_SLOTS = ["variant-1", "variant-2"]
DEFAULT_PROMPT_SET = "rag-hpo"
# Fixed tuning / evaluation split, created once and never changed: tuning documents are only for enhancements
# (prompt variants, error feedback, RAG-HPO settings, thresholds); final scores use the evaluation documents.
SPLITS_FILE = BUNDLE / "splits.json"
TUNE_SHARE, SPLIT_SEED = 0.30, 2026
# Embedding model for the semantic consistency scores. Shyr et al. (JAMIA 2026) used MedEmbed-large-v0.1 and
# checked it against nomic-embed-text-v1.5; MedEmbed is built on BAAI's BGE (Texas prohibited list), so we use
# nomic-embed-text-v1.5. It needs a task prefix on every text and its own model code (trust_remote_code).
EMBED_MODEL = "nomic-ai/nomic-embed-text-v1.5"
EMBED_PREFIX = "clustering: "

# ---- scorer (same rules as Model Watch's bench/score.py on Kaggle) ----
HP_CODE = re.compile(r"HP:\d{7}")
HP_ANY = re.compile(r"HP[:_](\d{7})")   # GSC+ writes HP_0001156 | name, ID-68 writes HP:0001156

def read_gold(path):
    """Expert codes for one document: (present, absent). Lines marked Neg are findings the patient does NOT have."""
    pos, neg = set(), set()
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = HP_ANY.search(line)
        if not m: continue
        parts = line.rstrip("\n").split("\t")
        (neg if any(p.strip().lower() == "neg" for p in parts) else pos).add("HP:" + m.group(1))
    return pos, neg - pos

def read_pred(path):
    """RAG-HPO output: {case: {'present': set, 'other': set}} (present = Category Abnormal) and a count per Category."""
    out, cats = {}, {}
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            case = row.get("patient id") or row.get("patient_id") or row.get("case")
            m = HP_CODE.search(row.get("hpo id") or row.get("hpo_id") or row.get("hpo_term") or "")
            if not case or not m: continue
            cat = row.get("category", "Abnormal") or "Abnormal"
            cats[cat] = cats.get(cat, 0) + 1
            d = out.setdefault(str(int(float(case))), {"present": set(), "other": set()})
            d["present" if cat.lower() == "abnormal" else "other"].add(m.group(0))
    return out, cats

def score_docs(gold_by_case, pred_by_case):
    """Document-level exact-code scoring, micro-averaged; also the mean per-document F1 and negation errors."""
    TP = FP = FN = NEG = 0; f1s = []; per = {}
    for case, (pos, neg) in gold_by_case.items():
        p = pred_by_case.get(case, {"present": set()})["present"]
        tp, fp, fn = len(p & pos), len(p - pos), len(pos - p); negerr = len(p & neg)
        TP += tp; FP += fp; FN += fn; NEG += negerr
        pr = tp / (tp + fp) if tp + fp else 0.0; rc = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0); per[case] = {"tp": tp, "fp": fp, "fn": fn, "neg_errors": negerr}
    P = TP / (TP + FP) if TP + FP else 0.0; R = TP / (TP + FN) if TP + FN else 0.0
    return {"precision": round(P, 3), "recall": round(R, 3), "f1": round(2 * P * R / (P + R), 3) if P + R else 0.0,
            "f1_doc_mean": round(sum(f1s) / len(f1s), 3) if f1s else 0.0,
            "tp": TP, "fp": FP, "fn": FN, "neg_errors": NEG, "n_docs": len(gold_by_case)}, per

_COMPLIANCE = None
def compliance_report():
    """Offline compliance check of the bundle, with the rules script 1 used (compliance.py in the bundle).
    Returns (problems, refused): problems stop every run; refused is {repo: reason} for single models.
    Checks: the bundle carries the rules and they are unchanged; every model and embedding model passed script 1's
    check; its owner, name and own config files do not name a developer on the Texas prohibited technologies list;
    and no model folder was added by hand."""
    global _COMPLIANCE
    if _COMPLIANCE is not None: return _COMPLIANCE
    problems, refused = [], {}
    m, rules = manifest(), BUNDLE / "compliance.py"
    rec = m.get("compliance") or {}
    if not rules.is_file() or not rec:
        problems.append("this bundle has no compliance record (built with an old script 1): rebuild it with the current build_bundle.py")
        _COMPLIANCE = (problems, refused); return _COMPLIANCE
    if hashlib.sha256(rules.read_bytes()).hexdigest() != rec.get("rules_sha256"):
        problems.append("compliance.py in the bundle is not the file script 1 used (checksum differs)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("mw_compliance", rules)
    C = importlib.util.module_from_spec(spec); spec.loader.exec_module(C)
    listed = set()
    for x in m.get("models", []):
        if x.get("status") != "ok": continue
        path = BUNDLE / x["path"]; listed.add(path.resolve())
        if x.get("compliance") != "passed": refused[x["repo"]] = "no passed compliance check recorded in MANIFEST.json"; continue
        ok, why = C.check_offline(x["repo"], path)
        for parent in x.get("lineage", []):
            if ok: ok, why = C.check_offline(parent)
        if not ok: refused[x["repo"]] = why
    if (BUNDLE / "models").is_dir():
        for d in (BUNDLE / "models").iterdir():
            if d.is_dir() and d.resolve() not in listed:
                problems.append(f"models/{d.name} is not in MANIFEST.json (added by hand?): remove it or rebuild the bundle")
    passed_emb = {e["repo"].lower() for e in m.get("embeddings", []) if e.get("compliance") == "passed"}
    for d in (BUNDLE / "hf_cache").glob("**/models--*"):
        repo = d.name[len("models--"):].replace("--", "/", 1)
        ok, why = C.check_offline(repo)
        if not ok: problems.append(f"hf_cache holds {repo}: {why}")
        elif repo.lower() not in passed_emb: problems.append(f"hf_cache holds {repo}, which is not a checked embedding model in MANIFEST.json")
    _COMPLIANCE = (problems, refused); return _COMPLIANCE

def available_models():
    """{repo: {"path": Path, "group": str, "bytes": int}} for every model the bundle downloaded AND the compliance
    check accepts. Every command that runs a model goes through here, so a refused model can never be served."""
    problems, refused = compliance_report()
    if problems: die("compliance check failed, nothing will run:\n  " + "\n  ".join(problems))
    out = {}
    for m in manifest().get("models", []):
        if m.get("status") == "ok" and m["repo"] not in refused:
            path = BUNDLE / m["path"]
            out[m["repo"]] = {"path": path, "group": m.get("group", "?"), "bytes": m.get("bytes", 0)}
    return out

def pick_models(spec):
    models = available_models()
    if not models: die("no downloaded models in MANIFEST.json")
    want = [s.strip() for s in spec.split(",") if s.strip()]
    if want == ["all"]: return list(models)
    chosen = []
    for w in want:
        hits = [r for r, m in models.items() if m["group"] == w] or [r for r in models if w.lower() in r.lower()]
        if not hits: die(f"--models: nothing matches '{w}'. Groups: {sorted({m['group'] for m in models.values()})}; models: {list(models)}")
        chosen += [h for h in hits if h not in chosen]
    return chosen

def is_thinking_model(repo):
    return bool(re.search(r"medgemma-1\.5|gpt-oss|thinking", repo, re.I))


# =============================================================================================
# check / install
# =============================================================================================
def cmd_check(a):
    ok = True
    m = manifest()
    log(f"Bundle built {m.get('built')} with groups {m.get('groups')}")
    sums = BUNDLE / "SHA256SUMS"
    if sums.exists() and not a.skip_checksums:
        log("Verifying checksums (reads every file once; use --skip-checksums to skip)...")
        r = subprocess.run(["sha256sum", "-c", "--quiet", str(sums)], cwd=BUNDLE, capture_output=True, text=True)
        if r.returncode: ok = False; log("CHECKSUM FAILURES:\n" + (r.stdout + r.stderr)[-2000:])
        else: log("Checksums OK")
    g = smi("--query-gpu=name,memory.total,memory.used", "--format=csv,noheader")
    if g is None: ok = False; log("nvidia-smi not found or failed: no GPU visible from here (inside a container, ask the server admin to pass the GPUs through)")
    else: log("GPUs:\n  " + g.strip().replace("\n", "\n  "))
    free = shutil.disk_usage(BUNDLE).free / 1e9
    log(f"Free disk on the bundle's mount: {free:,.0f} GB")
    problems, refused = compliance_report()
    for p_ in problems: log(f"COMPLIANCE: {p_}")
    for r, why in refused.items(): log(f"COMPLIANCE: {r} will not run: {why}")
    if problems or refused: ok = False
    else: log(f"Compliance check passed (rules version {m.get('compliance', {}).get('rules_version')}, Texas DIR list of {m.get('compliance', {}).get('list_updated')})")
    if problems: log("CHECK FOUND PROBLEMS (see above)"); return 1
    models = available_models()
    log(f"Models in the bundle: {len(models)}")
    for r, mm in models.items(): log(f"  [{mm['group']}] {r}  ({mm['bytes'] / 1e9:,.1f} GB){'  thinking' if is_thinking_model(r) else ''}")
    failed = [x["repo"] for x in m.get("models", []) if x.get("status") == "failed"]
    if failed: log(f"Not in the bundle (failed at download): {failed}")
    for need in ["repos/RAG-HPO/RAG-HPO_offline.py", "data/hpo_index.tgz", "data/hp.obo",
                 "repos/PhenoBERT/phenobert/data/ID-68", "tiktoken_cache"]:
        if not (BUNDLE / need).exists(): ok = False; log(f"MISSING: {need}")
    for d in m.get("data", []):
        if d.get("dataset") and d.get("status") == "failed": log(f"Dataset not bundled: {d['dataset']} ({d.get('error', '')[:80]})")
    if not any((BUNDLE / "hf_cache").glob("**/models--" + EMBED_MODEL.replace("/", "--"))):
        log(f"Not in hf_cache: {EMBED_MODEL} (needed only for the consistency test's semantic scores)")
    log(f"Fixed HPO release: {hpo_release()}")
    sp = get_splits(create=False)
    log(f"Tuning/evaluation split: {'created ' + sp['created'] if sp else 'not created yet (run `splits`)'}")
    try:
        import vllm  # noqa: F401
        log(f"vLLM {vllm.__version__} importable")
    except Exception as e:
        log(f"vLLM not importable in this Python ({type(e).__name__}): run `install`, then use env/bin/python, or run inside the vLLM container")
    log("Offline settings: " + ", ".join(f"{k}={v}" for k, v in OFFLINE_ENV.items() if k.startswith(("HF_HUB", "TIKTOKEN"))))
    log("CHECK PASSED" if ok else "CHECK FOUND PROBLEMS (see above)")
    return 0 if ok else 1

def cmd_install(a):
    wheels = BUNDLE / "wheels"
    req = wheels / "requirements-bundle.txt"
    if not req.exists(): die("wheels/ not in the bundle (built with --no-wheels?). Use the vLLM container instead.")
    py = a.python or sys.executable
    if not ENV.exists(): subprocess.run([py, "-m", "venv", str(ENV)], check=True)
    pip = [str(ENV / "bin" / "python"), "-m", "pip"]
    subprocess.run(pip + ["install", "--no-index", "--find-links", str(wheels), "-r", str(req)], check=True)
    log(f"Installed. From now on run: {ENV / 'bin' / 'python'} run_benchmark.py ...")


# =============================================================================================
# vLLM server, one model at a time
# =============================================================================================
def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

class VLLM:
    def __init__(self, repo, info, a):
        self.repo, self.path, self.a = repo, info["path"], a
        self.port = free_port(); self.proc = None
        gpus = a.gpus
        self.tp = a.tp or (2 if info["bytes"] > 60e9 and gpus >= 2 else 1)
        self.log = RESULTS / a.run_id / "logs" / f"vllm_{slug(repo)}.log"
    @property
    def url(self): return f"http://127.0.0.1:{self.port}"
    def __enter__(self):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", str(self.path),
               "--served-model-name", self.repo, "--host", "127.0.0.1", "--port", str(self.port),
               "--tensor-parallel-size", str(self.tp), "--gpu-memory-utilization", str(self.a.gpu_util),
               "--max-model-len", str(self.a.max_model_len), "--disable-log-requests"]
        if self.a.trust_remote_code: cmd.append("--trust-remote-code")
        if getattr(self.a, "logprobs_k", 0): cmd += ["--max-logprobs", str(self.a.logprobs_k)]
        log(f"Starting vLLM for {self.repo} (tensor parallel {self.tp}); log: {self.log}")
        self.proc = subprocess.Popen(cmd, stdout=open(self.log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < self.a.load_timeout_min * 60:
            if self.proc.poll() is not None:
                raise RuntimeError(f"vLLM exited while loading (code {self.proc.returncode}); last lines:\n" + tail(self.log))
            try:
                urllib.request.urlopen(self.url + "/v1/models", timeout=5).read(); self.load_s = time.time() - t0
                log(f"  ready in {self.load_s:.0f} s"); return self
            except Exception: time.sleep(5)
        raise RuntimeError(f"vLLM did not answer within {self.a.load_timeout_min} min; last lines:\n" + tail(self.log))
    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try: self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired: os.killpg(self.proc.pid, signal.SIGKILL)
        time.sleep(5)   # let the GPUs free

def tail(p, n=25):
    try: return "\n".join(Path(p).read_text(errors="ignore").splitlines()[-n:])
    except Exception: return "(no log)"

def slug(s): return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


# =============================================================================================
# Proxy between the task pipeline and vLLM: applies the run mode, logs every reply
# =============================================================================================
# RAG-HPO's two prompts, recognised by their opening words (repos/RAG-HPO/system_prompts.json)
STEP_SIGNS = {"extract": "highly specialized clinical information extraction", "map": "expert clinical phenotype mapper"}
SCHEMAS = {
    "extract": {"type": "object", "required": ["phenotypes"], "properties": {"phenotypes": {"type": "array", "items": {
        "type": "object", "required": ["phrase", "category"], "properties": {
            "phrase": {"type": "string"},
            "category": {"type": "string", "enum": ["Abnormal", "Normal", "Family History", "Other", "Suspected"]}}}}}},
    "map": {"type": "object", "required": ["hpo_id"], "properties": {"hpo_id": {"anyOf": [{"type": "string"}, {"type": "null"}]}}},
}
THINK = re.compile(r"<think>.*?</think>", re.S)

def step_of(body, step_texts=None):
    """Which RAG-HPO step a request belongs to. A prompt variant may not share RAG-HPO's opening words, so the
    prompt set's own two texts are matched first."""
    sysmsg = " ".join(m.get("content", "") for m in body.get("messages", []) if m.get("role") == "system")
    for k, text in (step_texts or {}).items():
        if text and sysmsg.strip() == text.strip(): return k
    return next((k for k, sign in STEP_SIGNS.items() if sign in sysmsg), "other")

def user_text(body):
    return " ".join(m.get("content", "") for m in body.get("messages", []) if m.get("role") == "user")

def certainty_of(choice, k):
    """Token-generation certainty of one reply (Shyr et al.): at each output position, the top-k token
    probabilities are renormalized and their Shannon entropy H taken; returns (mean H over positions, positions).
    Normalized certainty is then C = 1 - H / log2(k)."""
    import math
    content = ((choice or {}).get("logprobs") or {}).get("content") or []
    Hs = []
    for pos in content:
        tops = pos.get("top_logprobs") or []
        if not tops: continue
        ps = [math.exp(t["logprob"]) for t in tops[:k] if t.get("logprob") is not None and t["logprob"] > -1e9]
        z = sum(ps)
        if z <= 0: continue
        Hs.append(-sum((q / z) * math.log2(q / z) for q in ps if q > 0))
    return (sum(Hs) / len(Hs), len(Hs)) if Hs else (None, 0)

def judge(content):
    """strict = the whole reply is JSON; parseable = RAG-HPO's own regex would find a JSON object in it."""
    text = content.strip()
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try: json.loads(fenced); strict = True
    except Exception: strict = False
    m = re.search(r"\{.*\}", text, re.S)
    try: json.loads(m.group(0)) if m else None; parseable = bool(m)
    except Exception: parseable = False
    return strict, parseable

class Proxy:
    """sampling: {"temperature", "top_k", "seed"} forced on every request (consistency test only);
    logprobs_k > 0: asks vLLM for the top-k token probabilities and logs each reply's entropy (not the raw probabilities)."""
    def __init__(self, upstream, mode, thinking, replies_path, max_tokens, step_texts=None, sampling=None, logprobs_k=0):
        self.upstream, self.mode, self.thinking, self.max_tokens = upstream, mode, thinking, max_tokens
        self.step_texts, self.sampling, self.logprobs_k = step_texts, sampling or {}, logprobs_k
        self.replies = open(replies_path, "a"); self.lock = threading.Lock()
        self.stats = {"calls": 0, "strict_json": 0, "parseable": 0, "by_step": {}, "errors": 0, "seconds": 0.0}
        proxy = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                with urllib.request.urlopen(proxy.upstream + self.path, timeout=30) as r: data = r.read()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                code, data = proxy.forward(body)
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def forward(self, body):
        step = step_of(body, self.step_texts)
        for key in ("temperature", "top_k", "seed"):
            if self.sampling.get(key) is not None: body[key] = self.sampling[key]
        if self.logprobs_k: body["logprobs"] = True; body["top_logprobs"] = self.logprobs_k
        if self.thinking == "off": body["chat_template_kwargs"] = {**body.get("chat_template_kwargs", {}), "enable_thinking": False}
        if self.mode == "assisted":
            if step in SCHEMAS:
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": step, "schema": SCHEMAS[step]}}
            body.setdefault("max_tokens", self.max_tokens if self.thinking != "on" else self.max_tokens * 4)
            body.setdefault("repetition_penalty", 1.1)
        t0 = time.time()
        req = urllib.request.Request(self.upstream + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r: code, data = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, data = e.code, e.read()
        secs = time.time() - t0
        content, err, H, ntok = "", None, None, 0
        try:
            out = json.loads(data)
            content = out["choices"][0]["message"].get("content") or ""
            if self.logprobs_k: H, ntok = certainty_of(out["choices"][0], self.logprobs_k)
            if self.mode == "assisted" and "<think>" in content:          # keep only the answer
                out["choices"][0]["message"]["content"] = content = THINK.sub("", content).strip()
                data = json.dumps(out).encode()
        except Exception as e:
            err = f"{type(e).__name__}: {data[:200]!r}"
        strict, parseable = judge(content) if content else (False, False)
        with self.lock:
            s = self.stats; s["calls"] += 1; s["seconds"] += secs
            s["strict_json"] += strict; s["parseable"] += parseable; s["errors"] += bool(err or code != 200)
            b = s["by_step"].setdefault(step, {"calls": 0, "strict_json": 0, "parseable": 0}); b["calls"] += 1
            b["strict_json"] += strict; b["parseable"] += parseable
            rec = {"t": round(time.time(), 1), "step": step, "status": code, "seconds": round(secs, 2),
                   "strict_json": strict, "parseable": parseable, "error": err, "reply": content[:4000]}
            if self.logprobs_k: rec.update(entropy=H, tokens=ntok, k=self.logprobs_k)
            if step == "extract": rec["note_head"] = norm_text(user_text(body))[:300]   # to match the reply to its document
            self.replies.write(json.dumps(rec) + "\n"); self.replies.flush()
        return code, data

    def close(self): self.server.shutdown(); self.replies.close()


class GPUPeak:
    """GPU memory used (MiB, summed over GPUs) every 2 s: peak and average over the run."""
    def __init__(self):
        self.peak, self.total, self.n, self.stop = 0, 0, 0, False
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        while not self.stop:
            out = smi("--query-gpu=memory.used", "--format=csv,noheader,nounits") or ""
            try: used = sum(int(x) for x in out.split()); self.peak = max(self.peak, used); self.total += used; self.n += 1
            except ValueError: pass
            time.sleep(2)
    @property
    def mean(self): return self.total / self.n if self.n else 0


# =============================================================================================
# Task modules. Each: documents(docs) -> list, run(llm_url, model, docs, outdir, a) -> (pred info), score(...) -> metrics
# =============================================================================================
class NotBuilt(Exception): pass

_ALIASES = None
def hpo_aliases():
    """{old or alternative HPO ID: current ID} from data/hp.obo; empty if the bundle has no hp.obo."""
    global _ALIASES
    if _ALIASES is not None: return _ALIASES
    _ALIASES, obo = {}, BUNDLE / "data" / "hp.obo"
    if not obo.exists(): return _ALIASES
    cur = None; obsolete = False; repl = None
    def flush():
        if cur and obsolete and repl: _ALIASES[cur] = repl
    for line in open(obo, errors="ignore"):
        line = line.strip()
        if line == "[Term]": flush(); cur, obsolete, repl = None, False, None
        elif line.startswith("id: HP:"): cur = line[4:]
        elif line.startswith("alt_id: HP:") and cur: _ALIASES[line[8:]] = cur
        elif line == "is_obsolete: true": obsolete = True
        elif line.startswith("replaced_by: HP:"): repl = line[13:]
    flush()
    for k in list(_ALIASES):                      # follow chains (old -> retired -> current)
        seen = {k}
        while _ALIASES[k] in _ALIASES and _ALIASES[k] not in seen: seen.add(_ALIASES[k]); _ALIASES[k] = _ALIASES[_ALIASES[k]]
    return _ALIASES


def hpo_release():
    """The fixed HPO release every score uses: the data-version line of data/hp.obo."""
    obo = BUNDLE / "data" / "hp.obo"
    if not obo.exists(): die("data/hp.obo is missing: the benchmark needs one fixed HPO release to score against")
    for line in open(obo, errors="ignore"):
        if line.startswith("data-version:"): return line.split(":", 1)[1].strip()
        if line.startswith("[Term]"): break
    return "unknown (hp.obo has no data-version line)"

def to_release(codes):
    """Map retired or alternative HPO codes to the fixed release (the same rule for outputs and references)."""
    alias = hpo_aliases(); return {alias.get(c, c) for c in codes}

def norm_text(t): return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

# ---- fixed tuning / evaluation split ----
def get_splits(create=True):
    """{dataset: {"tune": [...], "eval": [...]}}, created once with a fixed seed and then only read."""
    if SPLITS_FILE.exists(): return load_json(SPLITS_FILE)
    if not create: return None
    import random
    out = {"created": datetime.datetime.now().isoformat(timespec="seconds"), "seed": SPLIT_SEED, "tune_share": TUNE_SHARE}
    for ds, d in HPO_DATASETS.items():
        names = sorted(p.name for p in (BUNDLE / "repos" / "PhenoBERT" / d["path"] / "corpus").iterdir() if p.is_file())
        rng = random.Random(f"{SPLIT_SEED}-{ds}"); order = names[:]; rng.shuffle(order)
        n_tune = 0 if d.get("no_split") else round(len(order) * TUNE_SHARE)
        out[ds] = {"tune": sorted(order[:n_tune]), "eval": sorted(order[n_tune:])}
    SPLITS_FILE.write_text(json.dumps(out, indent=1))
    log(f"Created the fixed tuning/evaluation split: {SPLITS_FILE.name} (never edit it once tuning has started)")
    return out

def cmd_splits(a):
    sp = get_splits()
    log(f"Split created {sp['created']}, seed {sp['seed']}, {int(sp['tune_share'] * 100)}% of each dataset for tuning")
    for ds in HPO_DATASETS: log(f"  {ds}: {len(sp[ds]['tune'])} tuning, {len(sp[ds]['eval'])} evaluation documents")

# ---- prompt sets ----
def prompt_file(name):
    if name == DEFAULT_PROMPT_SET: return BUNDLE / "repos" / "RAG-HPO" / "system_prompts.json"
    return PROMPTS / name / "system_prompts.json"

def prompt_sets(names, need=1):
    """Validated prompt sets: {name: (path, {"extract": text, "map": text}, sha256)}. Empty slots are skipped."""
    PROMPTS.mkdir(exist_ok=True)
    for slot in PROMPT_SLOTS:
        (PROMPTS / slot).mkdir(exist_ok=True)
        readme = PROMPTS / slot / "README.txt"
        if not readme.exists():
            readme.write_text("Put a variant of RAG-HPO's system_prompts.json here (same two keys: system_message_I for\n"
                              "phenotype extraction, system_message_II for HPO code choice; same JSON output format).\n"
                              "Write and freeze it before any run: its SHA-256 is recorded with every result.\n")
    out = {}
    for n in names:
        f = prompt_file(n)
        if not f.exists():
            log(f"Prompt set '{n}': no {f.relative_to(BUNDLE)} yet, skipped"); continue
        d = load_json(f)
        if not d.get("system_message_I") or not d.get("system_message_II"):
            die(f"{f}: needs both system_message_I and system_message_II")
        out[n] = (f, {"extract": d["system_message_I"], "map": d["system_message_II"]}, hashlib.sha256(f.read_bytes()).hexdigest()[:16])
    if len(out) < need: die(f"need at least {need} prompt set(s) with files; have {list(out)}")
    return out

# ---- parallel RAG-HPO copies ----
class Workers:
    """N separate working copies of RAG-HPO, so N documents (or runs) go to vLLM at once. Large index files are
    symlinked, not copied. Each copy has its own config, input and temporary files."""
    def __init__(self, n, src):
        import queue
        self.src, self.free = src, queue.Queue()
        for i in range(n):
            d = WORK / "workers" / f"w{i}"
            if not (d / "RAG-HPO_offline.py").exists():
                d.mkdir(parents=True, exist_ok=True)
                for f in src.iterdir():
                    t = d / f.name
                    if t.exists() or t.is_symlink(): continue
                    if f.is_file() and f.stat().st_size > 20e6: t.symlink_to(f)
                    elif f.is_dir(): shutil.copytree(f, t, symlinks=True)
                    else: shutil.copy2(f, t)
            self.free.put(d)

    def run(self, rows, llm_url, model, prompts_path, flag, temps, timeout_s, log_path, temperature):
        """One RAG-HPO process on `rows` (dicts with Case, clinical_note). Returns (pred path or None, seconds, timed_out)."""
        import pandas as pd
        d = self.free.get()
        try:
            shutil.copy(prompts_path, d / "system_prompts.json")
            inp, out = "input.csv", "pred.csv"
            pd.DataFrame(rows).to_csv(d / inp, index=False)
            json.dump({"api_key": "none", "base_url": llm_url + "/v1/chat/completions", "model_name": model,
                       "max_tokens_per_day": 10**9, "max_queries_per_minute": 100000, "temperature": temperature},
                      open(d / flag, "w"))
            for t in temps + [out]:
                if (d / t).exists(): (d / t).unlink()
            t0 = time.time(); timed_out = False
            try:
                r = subprocess.run([sys.executable, "RAG-HPO_offline.py"], cwd=d, input=f"no\n{inp}\nsave\n{out}\n",
                                   text=True, capture_output=True, timeout=timeout_s)
                Path(log_path).write_text((r.stdout + r.stderr)[-20000:])
            except subprocess.TimeoutExpired:
                timed_out = True
            secs = time.time() - t0
            if not (d / out).exists(): return None, secs, timed_out
            keep = Path(log_path).with_name(Path(log_path).stem.replace("pipeline", "pred") + ".csv")
            shutil.copy(d / out, keep); return keep, secs, timed_out
        finally:
            self.free.put(d)

def merge_csv(parts, dest):
    """Concatenate RAG-HPO output files (same header); Case numbers are already unique across the parts."""
    header, rows = None, []
    for p in parts:
        if not p: continue
        with open(p, newline="", encoding="utf-8", errors="ignore") as f:
            r = list(csv.reader(f))
        if not r: continue
        header = header or r[0]; rows += r[1:]
    if header is None: return None
    with open(dest, "w", newline="") as f: w = csv.writer(f); w.writerow(header); w.writerows(rows)
    return dest

class HPOModule:
    """HPO extraction with RAG-HPO (unchanged pipeline, offline copy), scored with the built-in scorer above."""
    name, task_key = "hpo", "hpo_rag"
    def __init__(self):
        self.read_gold, self.read_pred, self.score_fn = read_gold, read_pred, score_docs
        self.rag = WORK / "RAG-HPO"
        if not (self.rag / "hpo_embedded.npz").exists():
            log("Preparing a work copy of RAG-HPO with the HPO index...")
            if self.rag.exists(): shutil.rmtree(self.rag)
            shutil.copytree(BUNDLE / "repos" / "RAG-HPO", self.rag)
            with tarfile.open(BUNDLE / "data" / "hpo_index.tgz") as t: t.extractall(self.rag)
            if not (self.rag / "hpo_embedded.npz").exists(): die("HPO index did not unpack (hpo_embedded.npz missing)")
        src = (self.rag / "RAG-HPO_offline.py").read_text()
        self.flag = re.search(r"FLAG_FILE\s*=\s*['\"]([^'\"]+)['\"]", src).group(1)
        tf = re.search(r"TEMP_FILES\s*=\s*\{[^}]*\}", src, re.S)
        self.temps = re.findall(r"['\"]([^'\"]+\.(?:csv|json|pkl|parquet))['\"]", tf.group(0)) if tf else []

    def datasets(self): return list(HPO_DATASETS)

    def has_docs(self, ds, which):
        """False when a selection is empty for this dataset (GeneReviews has no tuning or Kaggle documents)."""
        try: return bool(self.documents(ds, which)[1])
        except SystemExit: raise
        except Exception: return False

    def documents(self, ds, which):
        """full = every document (ID-68: 68, GSC+: 228, GeneReviews: 10); test24 = the Kaggle set; eval / tune = the fixed split."""
        d = HPO_DATASETS[ds]; folder = BUNDLE / "repos" / "PhenoBERT" / d["path"]
        names = sorted(p.name for p in (folder / "corpus").iterdir() if p.is_file())
        if which == "test24": docs = d["test24"]
        elif which in ("eval", "tune"):
            sp = get_splits()
            if ds not in sp: die(f"splits.json has no '{ds}' (made before it was added): add it by hand as tune [] / eval [all]")
            docs = sp[ds][which]
        else: docs = names
        return folder, docs

    def note(self, folder, doc): return " ".join((folder / "corpus" / doc).read_text(errors="ignore").split())

    def workers(self, n):
        if getattr(self, "_workers", None) is None or self._workers_n != n:
            self._workers, self._workers_n = Workers(n, self.rag), n
        return self._workers

    def run(self, llm_url, model, ds, which, outdir, a):
        """RAG-HPO on one dataset. With --concurrency N the documents are split over N RAG-HPO copies that call
        vLLM at the same time; their outputs are merged before scoring."""
        folder, docs = self.documents(ds, which)
        if a.max_docs: docs = docs[:a.max_docs]
        rows = [{"Case": i, "clinical_note": self.note(folder, doc)} for i, doc in enumerate(docs, 1)]
        n = max(1, min(a.concurrency, len(rows)))
        shards = [rows[i::n] for i in range(n)]
        pool = self.workers(n); results = [None] * n
        prompts_path = prompt_file(a.prompt_set)
        def go(i):
            results[i] = pool.run(shards[i], llm_url, model, prompts_path, self.flag, self.temps, a.timeout_min * 60,
                                  outdir / f"pipeline_{i}.log", TEMPERATURE)
        t0 = time.time()
        ths = [threading.Thread(target=go, args=(i,)) for i in range(n)]
        for t in ths: t.start()
        for t in ths: t.join()
        secs = time.time() - t0
        pred = merge_csv([r[0] for r in results], outdir / "pred.csv")
        for f in outdir.glob("pred_*.csv"): f.unlink()
        json.dump({str(i): d for i, d in enumerate(docs, 1)}, open(outdir / "docs.json", "w"))
        return {"pred": pred, "seconds": secs, "timed_out": any(r[2] for r in results), "n_docs": len(docs),
                "folder": folder, "docs": docs, "partial": any(r[0] is None for r in results) and pred is not None}

    def gold(self, folder, docs):
        return {str(i): self.read_gold(folder / "ann" / d) if (folder / "ann" / d).exists() else (set(), set())
                for i, d in enumerate(docs, 1)}

    def score(self, info):
        """Main score: codes on both sides mapped to the fixed HPO release. exact_original_ids: no mapping (only for
        comparison with published numbers, which use the datasets' original codes)."""
        gold = self.gold(info["folder"], info["docs"])
        pred, cats = self.read_pred(info["pred"])
        exact, _ = self.score_fn(gold, pred)
        g2 = {k: (to_release(p), to_release(n)) for k, (p, n) in gold.items()}
        p2 = {k: {**v, "present": to_release(v["present"])} for k, v in pred.items()}
        m, _ = self.score_fn(g2, p2)
        m["categories"] = cats; m["hpo_release"] = hpo_release()
        m["exact_original_ids"] = {k: exact[k] for k in ("precision", "recall", "f1", "f1_doc_mean", "tp", "fp", "fn", "neg_errors")}
        return m

class PendingModule:
    """Data is in the bundle; the pipeline and scorer are not written yet."""
    def __init__(self, name, task_key, data_path):
        self.name, self.task_key, self.data_path = name, task_key, data_path
    def datasets(self): return [self.name]

    def has_docs(self, ds, which): return True   # run() reports "not built yet"
    def run(self, *args, **kw):
        raise NotBuilt(f"the {self.name} module is not built yet (its data is bundled at {self.data_path})")

def modules(names):
    table = {"hpo": HPOModule,
             "tmvar": lambda: PendingModule("tmvar", "tmvar3_variants", "data/datasets/tmVar3"),
             "biored": lambda: PendingModule("biored", "biored_relations", "data/datasets/BioRED")}
    out = []
    for n in names:
        if n not in table: die(f"--tasks: unknown '{n}'; choose from {list(table)}")
        out.append(table[n]())
    return out


# =============================================================================================
# run / smoke
# =============================================================================================
def combos(repo, a):
    modes = ["as-is", "assisted"] if a.mode == "both" else [a.mode]
    thinks = (["on", "off"] if a.thinking == "both" else [a.thinking]) if is_thinking_model(repo) else ["default"]
    return [(m, t) for m in modes for t in thinks]

def result_dir(repo, mod, ds, which, mode, think, a):
    variant = mode + ("" if think == "default" else f"_thinking-{think}")
    if a.prompt_set != DEFAULT_PROMPT_SET: variant += f"_prompts-{a.prompt_set}"
    return RESULTS / a.run_id / slug(repo) / mod.name / f"{ds}_{which}" / variant

def cmd_run(a, smoke=False):
    a.run_id = a.run_id or datetime.date.today().isoformat()
    if smoke:
        a.models, a.tasks, a.docs, a.max_docs, a.mode, a.run_id = (a.models or "core"), "hpo", "full", 1, "as-is", a.run_id + "_smoke"
        a.concurrency = 1
    repos = pick_models(a.models)
    if smoke: repos = repos[:1]
    mods = modules([t.strip() for t in a.tasks.split(",") if t.strip()])
    sets = prompt_sets([a.prompt_set]); step_texts = sets[a.prompt_set][1]
    info = available_models()
    log(f"Run {a.run_id}: {len(repos)} model(s) x tasks {[m.name for m in mods]} x docs={a.docs} x mode={a.mode}")
    summary_path = RESULTS / a.run_id / "summary.jsonl"; summary_path.parent.mkdir(parents=True, exist_ok=True)
    gpu_names = (smi("--query-gpu=name,memory.total", "--format=csv,noheader") or "").strip().splitlines()
    for repo in repos:
        todo = [(mod, ds, mode, think) for mod in mods for ds in mod.datasets() for mode, think in combos(repo, a)
                if mod.has_docs(ds, a.docs)
                and not (result_dir(repo, mod, ds, a.docs, mode, think, a) / "result.json").exists()]
        if not todo: log(f"{repo}: all done, skipping"); continue
        try:
            server = VLLM(repo, info[repo], a).__enter__()
        except Exception as e:
            log(f"{repo}: could not start ({e})")
            for mod, ds, mode, think in todo:
                write_result(repo, mod, ds, mode, think, a, {"ok": False, "reason": f"vLLM failed to load: {str(e)[:300]}"}, gpu_names, final=False)
            continue
        try:
            for mod, ds, mode, think in todo:
                outdir = result_dir(repo, mod, ds, a.docs, mode, think, a); outdir.mkdir(parents=True, exist_ok=True)
                log(f"  {repo} | {mod.name} | {ds} ({a.docs}) | {mode}{'' if think == 'default' else ', thinking ' + think}")
                proxy = Proxy(server.url, mode, think, outdir / "replies.jsonl", a.max_tokens, step_texts=step_texts)
                peak = GPUPeak()
                res = {"ok": False, "reason": None}; final = True
                try:
                    run = mod.run(proxy.url, repo, ds, a.docs, outdir, a)
                    res.update(seconds=round(run["seconds"], 1), n_docs=run["n_docs"],
                               secs_per_doc=round(run["seconds"] / max(run["n_docs"], 1), 1))
                    if run["timed_out"] and not run["pred"]: res["reason"] = f"timed out after {a.timeout_min} min"
                    elif not run["pred"]: res["reason"] = "the pipeline produced no output (see pipeline.log and replies.jsonl)"
                    else:
                        res["metrics"] = mod.score(run); res["ok"] = res["metrics"]["tp"] + res["metrics"]["fp"] > 0
                        if not res["ok"]: res["reason"] = "no codes returned for any document"
                        elif run["timed_out"] or run.get("partial"): res["reason"] = "partial: a RAG-HPO copy timed out or failed (see pipeline_*.log)"
                except NotBuilt as e:
                    res["reason"] = str(e); final = False
                except Exception as e:
                    res["reason"] = f"{type(e).__name__}: {str(e)[:300]}"
                finally:
                    peak.stop = True; proxy.close()
                st = proxy.stats
                res["format"] = {"calls": st["calls"], "errors": st["errors"],
                                 "strict_json_rate": round(st["strict_json"] / st["calls"], 3) if st["calls"] else None,
                                 "parseable_rate": round(st["parseable"] / st["calls"], 3) if st["calls"] else None,
                                 "by_step": st["by_step"]}
                res["gpu_used_peak_gib"] = round(peak.peak / 1024, 1); res["gpu_used_mean_gib"] = round(peak.mean / 1024, 1)
                res["prompt_set"] = a.prompt_set; res["prompt_sha256"] = sets[a.prompt_set][2]
                res["load_seconds"] = round(getattr(server, "load_s", 0))
                write_result(repo, mod, ds, mode, think, a, res, gpu_names, outdir, final)
                m = res.get("metrics") or {}
                log(f"    -> {'ok' if res['ok'] else res['reason']}  F1 {m.get('f1')}  P {m.get('precision')}  R {m.get('recall')}"
                    f"  neg {m.get('neg_errors')}  {res.get('secs_per_doc')} s/doc  JSON {res['format']['strict_json_rate']}")
                if smoke:
                    print("\nFirst replies from the model:\n" + "\n".join(
                        json.loads(l)["reply"][:600] for l in open(outdir / "replies.jsonl").readlines()[:2]))
        finally:
            server.__exit__(None, None, None)
    log(f"Done. Results in {RESULTS / a.run_id}. Next: `export` to make the archive that may leave the server.")

def vllm_version():
    try:
        from importlib.metadata import version; return version("vllm")
    except Exception: return "unknown"

def run_settings(a, **extra):
    """Everything needed to rerun this result the same way."""
    return {"vllm": vllm_version(), "hpo_release": hpo_release(), "concurrency": a.concurrency,
            "max_model_len": a.max_model_len, "gpu_util": a.gpu_util, "max_tokens_assisted": a.max_tokens, **extra}

def write_result(repo, mod, ds, mode, think, a, res, gpu_names, outdir=None, final=True):
    """final=False (module not built, model failed to load): logged, but retried on the next run."""
    outdir = outdir or result_dir(repo, mod, ds, a.docs, mode, think, a); outdir.mkdir(parents=True, exist_ok=True)
    row = {"model": repo, "task": mod.task_key, "dataset": ds, "docs": a.docs, "mode": mode, "thinking": think,
           "date": datetime.date.today().isoformat(), "run_id": a.run_id, "platform": "lab server, vLLM, full precision",
           "gpus": gpu_names, "task_version": TASK_VERSION, "settings": run_settings(a, temperature=TEMPERATURE), **res}
    (outdir / ("result.json" if final else "not_run.json")).write_text(json.dumps(row, indent=1, default=str))
    if not final: return
    with open(RESULTS / a.run_id / "summary.jsonl", "a") as f: f.write(json.dumps(row, default=str) + "\n")



# =============================================================================================
# consistency: repeatability and reproducibility of the HPO task (Shyr et al., JAMIA 2026)
# =============================================================================================
# Repeatability = the same model, prompt set and settings, run R times with different seeds.
# Reproducibility = the same model across pre-specified prompt sets.
# Semantic scores use the step-1 extraction (the document-level answer: phenotype phrases and categories),
# embedded with nomic-embed-text-v1.5; internal scores use the token entropy of that same reply. We add agreement of the final
# HPO code sets (step 2 included), mapped to the fixed release. Per-run F1 shows how much accuracy moves between runs.
def pick_sample(a):
    """[(dataset, doc)]: --sample-file ('dataset/doc' per line), or a seeded sample from the evaluation split,
    half from each dataset. The per-disease cap cannot be automated (no disease labels): use --sample-file for it."""
    if a.sample_file:
        out = []
        for line in open(a.sample_file):
            line = line.strip()
            if not line or line.startswith("#"): continue
            ds, doc = line.split("/", 1)
            if ds not in HPO_DATASETS: die(f"--sample-file: unknown dataset '{ds}' (use {list(HPO_DATASETS)})")
            out.append((ds, doc))
        return out
    import random
    sp = get_splits(); out = []
    for ds, k in zip(HPO_DATASETS, [a.sample // 2, a.sample - a.sample // 2]):
        pool = sp[ds]["eval"]; rng = random.Random(f"{a.seed}-sample-{ds}")
        out += [(ds, d) for d in sorted(rng.sample(pool, min(k, len(pool))))]
    return out

def extraction_text(reply):
    """Canonical text of a step-1 reply: its phrases and categories, sorted, so order alone is not a difference."""
    t = THINK.sub("", reply or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    try:
        items = json.loads(m.group(0)).get("phenotypes", []) if m else None
        if isinstance(items, list):
            return "; ".join(sorted(f"{str(i.get('phrase', '')).strip().lower()} [{i.get('category', '')}]" for i in items if isinstance(i, dict)))
    except Exception:
        pass
    return norm_text(t)

def jaccard(x, y): return 1.0 if not x and not y else len(x & y) / len(x | y)

def boot_ci(vals, seed=0, n=1000):
    """Median and a 95% bootstrap interval over documents."""
    import random, statistics
    vals = [v for v in vals if v is not None]
    if not vals: return None
    rng = random.Random(seed); meds = sorted(statistics.median(rng.choices(vals, k=len(vals))) for _ in range(n))
    return {"median": round(statistics.median(vals), 4), "ci95": [round(meds[int(0.025 * n)], 4), round(meds[int(0.975 * n) - 1], 4)], "n_docs": len(vals)}

def embedder():
    try:
        from sentence_transformers import SentenceTransformer
        try:
            import torch; dev = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception: dev = "cpu"
        return SentenceTransformer(EMBED_MODEL, device=dev, trust_remote_code=True)
    except Exception as e:
        log(f"  semantic scores skipped: could not load {EMBED_MODEL} offline ({type(e).__name__}: {str(e)[:150]})"); return None

def score_consistency(mdir, sets, notes, a):
    import difflib, math, statistics
    heads = {c: norm_text(t)[:300] for c, (_, _, t) in notes.items()}
    def match(head):
        for c, h in heads.items():
            if head and (h.startswith(head[:200]) or head.startswith(h[:200])): return c
        best = max(heads, key=lambda c: difflib.SequenceMatcher(None, heads[c][:150], (head or "")[:150]).ratio())
        return best if difflib.SequenceMatcher(None, heads[best][:150], (head or "")[:150]).ratio() > 0.8 else None
    gold = {}
    for c, (ds, doc, _) in notes.items():
        f = BUNDLE / "repos" / "PhenoBERT" / HPO_DATASETS[ds]["path"] / "ann" / doc
        pos, neg = read_gold(f) if f.exists() else (set(), set()); gold[c] = (to_release(pos), to_release(neg))
    logk = math.log2(a.logprobs_k) if a.logprobs_k > 1 else None
    data = {}     # data[set][run] = {"codes": {case: set}, "text": {case: str}, "C": {case: certainty}, "f1"...}
    for name in sets:
        for jd in sorted((mdir / name).glob("run_*")):
            if not (jd / "done.json").exists() or not (jd / "pred.csv").exists(): continue
            pred, _ = read_pred(jd / "pred.csv")
            codes = {c: to_release(pred.get(c, {"present": set()})["present"]) for c in notes}
            m, _ = score_docs(gold, {c: {"present": v} for c, v in codes.items()})
            texts, ent = {}, {}
            for line in open(jd / "replies.jsonl"):
                rec = json.loads(line)
                if rec.get("step") != "extract": continue
                c = match(rec.get("note_head", ""))
                if c is None: continue
                texts.setdefault(c, []).append(extraction_text(rec.get("reply", "")))
                if rec.get("entropy") is not None and rec.get("tokens"):
                    s0, n0 = ent.get(c, (0.0, 0)); ent[c] = (s0 + rec["entropy"] * rec["tokens"], n0 + rec["tokens"])
            C = {c: 1 - (sH / n) / logk for c, (sH, n) in ent.items() if n and logk}
            data.setdefault(name, {})[jd.name] = {"codes": codes, "text": {c: " | ".join(v) for c, v in texts.items()}, "C": C,
                                                   "f1": m["f1"], "precision": m["precision"], "recall": m["recall"]}
    model = embedder()
    vec = {}
    if model:
        uniq = sorted({t for runs in data.values() for r in runs.values() for t in r["text"].values()})
        if uniq:
            E = model.encode([EMBED_PREFIX + t for t in uniq], batch_size=32, normalize_embeddings=True, show_progress_bar=False)
            vec = {t: e for t, e in zip(uniq, E)}
    cos = lambda x, y: float(sum(i * j for i, j in zip(vec[x], vec[y]))) if x in vec and y in vec else None
    def pairs_mean(xs):
        v = [z for z in xs if z is not None]; return sum(v) / len(v) if v else None
    rows, per_set = [], {}
    for c in notes:
        row = {"case": c, "dataset": notes[c][0], "doc": notes[c][1]}; set_means, set_texts, set_codes = {}, {}, {}
        for name, runs in data.items():
            R = list(runs.values())
            T = [r["text"].get(c) for r in R if r["text"].get(c) is not None]
            Cs = [r["C"][c] for r in R if c in r["C"]]
            K = [r["codes"][c] for r in R]
            s_rep = pairs_mean([cos(T[i], T[j]) for i in range(len(T)) for j in range(i + 1, len(T))]) if len(T) >= 2 else None
            row[f"{name}:semantic_repeatability"] = None if s_rep is None else (s_rep + 1) / 2
            row[f"{name}:internal_repeatability"] = 1 - 2 * statistics.pstdev(Cs) if len(Cs) >= 2 else None
            row[f"{name}:mean_certainty"] = sum(Cs) / len(Cs) if Cs else None
            row[f"{name}:code_agreement"] = pairs_mean([jaccard(K[i], K[j]) for i in range(len(K)) for j in range(i + 1, len(K))]) if len(K) >= 2 else None
            if Cs: set_means[name] = sum(Cs) / len(Cs)
            set_texts[name], set_codes[name] = T, K
        names = [n for n in data if set_texts.get(n)]
        if len(names) >= 2:
            A = [pairs_mean([cos(x, y) for x in set_texts[p] for y in set_texts[q]]) for i, p in enumerate(names) for q in names[i + 1:]]
            A = [x for x in A if x is not None]
            row["semantic_reproducibility"] = (sum(A) / len(A) + 1) / 2 if A else None
            row["internal_reproducibility"] = 1 - 2 * statistics.pstdev(list(set_means.values())) if len(set_means) >= 2 else None
            row["code_reproducibility"] = pairs_mean([pairs_mean([jaccard(x, y) for x in set_codes[p] for y in set_codes[q]])
                                                       for i, p in enumerate(names) for q in names[i + 1:]])
        rows.append(row)
    for name, runs in data.items():
        f1s = [r["f1"] for r in runs.values()]
        per_set[name] = {"runs_done": len(runs), "prompt_sha256": sets[name][2],
                         **{k: boot_ci([r.get(f"{name}:{k}") for r in rows]) for k in ("semantic_repeatability", "internal_repeatability", "mean_certainty", "code_agreement")},
                         "f1_across_runs": {"mean": round(sum(f1s) / len(f1s), 4), "sd": round(statistics.pstdev(f1s), 4),
                                            "min": min(f1s), "max": max(f1s)} if f1s else None}
    repro = {k: boot_ci([r.get(k) for r in rows]) for k in ("semantic_reproducibility", "internal_reproducibility", "code_reproducibility")} \
        if len(data) >= 2 else None
    with open(mdir / "per_document.csv", "w", newline="") as f:
        keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("case", "dataset", "doc"), k))
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
    return {"by_prompt_set": per_set, "reproducibility": repro, "semantic_scores": bool(vec)}

def cmd_consistency(a):
    a.run_id = a.run_id or (datetime.date.today().isoformat() + "_consistency")
    a.prompt_set, a.docs, a.max_docs = DEFAULT_PROMPT_SET, "consistency", 0
    root = RESULTS / a.run_id / "consistency"; root.mkdir(parents=True, exist_ok=True)
    sets = prompt_sets([x.strip() for x in a.prompt_sets.split(",") if x.strip()])
    if len(sets) < 2: log(f"Prompt sets with files: {list(sets)}. Repeatability only; reproducibility needs 2 or more.")
    sample = pick_sample(a)
    sp = root / "sample.json"
    if sp.exists() and load_json(sp) != [list(x) for x in sample]:
        die(f"{sp} holds a different document sample: use a new --run-id, or the same sample settings to resume")
    sp.write_text(json.dumps([list(x) for x in sample]))
    hpo = HPOModule(); notes, rows = {}, []
    for i, (ds, doc) in enumerate(sample, 1):
        folder, _ = hpo.documents(ds, "full"); notes[str(i)] = (ds, doc, hpo.note(folder, doc))
        rows.append({"Case": i, "clinical_note": notes[str(i)][2]})
    repos, info = pick_models(a.models), available_models()
    settings = run_settings(a, temperature=a.temperature, top_k=a.top_k, logprobs_k=a.logprobs_k, runs=a.runs, seed_base=a.seed,
                            mode=a.mode, thinking=a.thinking, prompt_sets={n: v[2] for n, v in sets.items()},
                            sample_size=len(sample), sample_source=a.sample_file or "evaluation split", embed_model=EMBED_MODEL)
    (root / "settings.json").write_text(json.dumps(settings, indent=1))
    log(f"Consistency test {a.run_id}: {len(repos)} model(s) x {len(sets)} prompt set(s) x {a.runs} runs x {len(sample)} documents, "
        f"{a.concurrency} in parallel, temperature {a.temperature}, top-k {a.top_k}, entropy over top-{a.logprobs_k}")
    from concurrent.futures import ThreadPoolExecutor
    for repo in repos:
        mdir = root / slug(repo)
        jobs = [(n, r) for n in sets for r in range(1, a.runs + 1) if not (mdir / n / f"run_{r:03d}" / "done.json").exists()]
        if jobs:
            try:
                server = VLLM(repo, info[repo], a).__enter__()
            except Exception as e:
                log(f"{repo}: could not start ({e})"); continue
            pool, peak, think = hpo.workers(a.concurrency), GPUPeak(), (a.thinking if is_thinking_model(repo) else "default")
            done = [0]; t0 = time.time()
            def job(n, r):
                jd = mdir / n / f"run_{r:03d}"; jd.mkdir(parents=True, exist_ok=True)
                for f in ("replies.jsonl", "pred.csv"):
                    if (jd / f).exists(): (jd / f).unlink()
                proxy = Proxy(server.url, a.mode, think, jd / "replies.jsonl", a.max_tokens, step_texts=sets[n][1],
                              sampling={"temperature": a.temperature, "top_k": a.top_k, "seed": a.seed + r}, logprobs_k=a.logprobs_k)
                try:
                    pred, secs, timed_out = pool.run(rows, proxy.url, repo, sets[n][0], hpo.flag, hpo.temps,
                                                     a.timeout_min * 60, jd / "pipeline.log", a.temperature)
                finally:
                    proxy.close()
                rec = {"seconds": round(secs, 1), "timed_out": timed_out, "seed": a.seed + r, "calls": proxy.stats["calls"],
                       "errors": proxy.stats["errors"]}
                (jd / ("done.json" if pred else "failed.json")).write_text(json.dumps(rec))
                done[0] += 1
                log(f"  {repo} | {n} | run {r}: {'ok' if pred else 'FAILED'} in {secs:.0f} s ({done[0]}/{len(jobs)})")
            try:
                with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
                    for f in [ex.submit(job, n, r) for n, r in jobs]: f.result()
            finally:
                peak.stop = True; server.__exit__(None, None, None)
            (mdir / "gpu.json").write_text(json.dumps({"peak_gib": round(peak.peak / 1024, 1), "mean_gib": round(peak.mean / 1024, 1),
                                                       "wall_seconds": round(time.time() - t0)}))
        log(f"  {repo}: scoring")
        res = score_consistency(mdir, sets, notes, a)
        row = {"model": repo, "task": "hpo_rag_consistency", "date": datetime.date.today().isoformat(), "run_id": a.run_id,
               "settings": settings, **res}
        if (mdir / "gpu.json").exists(): row["gpu"] = load_json(mdir / "gpu.json")
        (mdir / "consistency.json").write_text(json.dumps(row, indent=1, default=str))
        for n, v in res["by_prompt_set"].items():
            sr, ir, ca = (v.get(k) or {} for k in ("semantic_repeatability", "internal_repeatability", "code_agreement"))
            log(f"    {n}: semantic {sr.get('median')} {sr.get('ci95')}, internal {ir.get('median')}, code sets {ca.get('median')}, "
                f"F1 across runs {v.get('f1_across_runs')}")
        if res["reproducibility"]: log(f"    across prompt sets: {json.dumps(res['reproducibility'])}")
    log(f"Done. Results in {root}. To check the setup's own effect, rerun a few documents with --concurrency 1 under a new --run-id.")

# =============================================================================================
# export: what may leave the server (scores, timings, settings; no text)
# =============================================================================================
def cmd_export(a):
    runs = sorted(p for p in RESULTS.iterdir() if p.is_dir()) if RESULTS.exists() else []
    if a.run_id: runs = [RESULTS / a.run_id]
    if not runs: die("no results yet")
    run = runs[-1]
    rows = [json.loads(p.read_text()) for p in run.rglob("result.json")]
    keep = ["model", "task", "dataset", "docs", "mode", "thinking", "prompt_set", "prompt_sha256", "date", "platform", "gpus",
            "task_version", "settings", "ok", "reason", "seconds", "n_docs", "secs_per_doc", "gpu_used_peak_gib", "gpu_used_mean_gib",
            "load_seconds", "format", "metrics"]
    clean = [{k: r.get(k) for k in keep} for r in rows]
    for c in clean:
        if c.get("format"): c["format"] = {k: v for k, v in c["format"].items() if k != "by_step"} | {"by_step": c["format"].get("by_step")}
    consistency = [{k: v for k, v in json.loads(p.read_text()).items()} for p in run.rglob("consistency.json")]
    out = BUNDLE / f"model_watch_server_{run.name}.json"
    out.write_text(json.dumps({"exported": datetime.datetime.now().isoformat(timespec="seconds"), "run": run.name,
                               "results": clean, "consistency": consistency}, indent=1))
    files = [out.name]
    # As-is and assisted results go in separate tables, so the effect of the output controls is never mixed into a ranking.
    for mode in sorted({c["mode"] for c in clean}):
        csvp = out.with_name(f"{out.stem}_{mode}.csv"); files.append(csvp.name)
        with open(csvp, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["model", "task", "dataset", "docs", "mode", "thinking", "prompt_set", "ok", "f1", "precision", "recall",
                                           "f1_doc_mean", "neg_errors", "f1_exact_original_ids", "hpo_release", "secs_per_doc",
                                           "gpu_peak_gib", "gpu_mean_gib", "strict_json_rate", "reason"])
            for c in (c for c in clean if c["mode"] == mode):
                m = c.get("metrics") or {}; fm = c.get("format") or {}
                w.writerow([c["model"], c["task"], c["dataset"], c["docs"], c["mode"], c["thinking"], c.get("prompt_set"), c["ok"],
                            m.get("f1"), m.get("precision"), m.get("recall"), m.get("f1_doc_mean"), m.get("neg_errors"),
                            (m.get("exact_original_ids") or {}).get("f1"), m.get("hpo_release"), c.get("secs_per_doc"),
                            c.get("gpu_used_peak_gib"), c.get("gpu_used_mean_gib"), fm.get("strict_json_rate"), c.get("reason")])
    if consistency:
        csvp = out.with_name(f"{out.stem}_consistency.csv"); files.append(csvp.name)
        with open(csvp, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["model", "prompt_set", "runs", "semantic_repeatability", "semantic_ci95", "internal_repeatability",
                                           "internal_ci95", "mean_certainty", "code_agreement", "code_ci95", "f1_mean", "f1_sd"])
            for c in consistency:
                for n, v in c["by_prompt_set"].items():
                    g = lambda k: v.get(k) or {}
                    w.writerow([c["model"], n, v["runs_done"], g("semantic_repeatability").get("median"), g("semantic_repeatability").get("ci95"),
                                g("internal_repeatability").get("median"), g("internal_repeatability").get("ci95"),
                                g("mean_certainty").get("median"), g("code_agreement").get("median"), g("code_agreement").get("ci95"),
                                (v.get("f1_across_runs") or {}).get("mean"), (v.get("f1_across_runs") or {}).get("sd")])
                if c.get("reproducibility"):
                    r = c["reproducibility"]
                    w.writerow([c["model"], "ACROSS PROMPT SETS", "", (r.get("semantic_reproducibility") or {}).get("median"),
                                (r.get("semantic_reproducibility") or {}).get("ci95"), (r.get("internal_reproducibility") or {}).get("median"),
                                (r.get("internal_reproducibility") or {}).get("ci95"), "", (r.get("code_reproducibility") or {}).get("median"),
                                (r.get("code_reproducibility") or {}).get("ci95"), "", ""])
    log(f"Wrote {', '.join(files)} ({len(clean)} results, {len(consistency)} consistency summaries): scores, timings and settings only, "
        "no document text or model replies.")


# =============================================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("--skip-checksums", action="store_true")
    sub.add_parser("splits", help="show (or create once) the fixed tuning / evaluation split")
    def server_args(p):
        p.add_argument("--max-tokens", type=int, default=2048, help="answer cap in assisted mode (x4 with thinking on)")
        p.add_argument("--gpus", type=int, default=2)
        p.add_argument("--tp", type=int, default=0, help="tensor parallel size (0 = 2 for models over 60 GB, else 1)")
        p.add_argument("--gpu-util", type=float, default=0.85)
        p.add_argument("--max-model-len", type=int, default=8192)
        p.add_argument("--load-timeout-min", type=float, default=20)
        p.add_argument("--trust-remote-code", action="store_true", help="only for models that need it; runs model code from the bundle")
        p.add_argument("--run-id", default=None, help="results folder name (default: today's date)")
    i = sub.add_parser("install"); i.add_argument("--python", help="Python 3.12 to build the env with (default: this one)")
    for name in ("run", "smoke"):
        p = sub.add_parser(name)
        p.add_argument("--models", default=None if name == "smoke" else "core,biomedical", help="groups (core, biomedical, small, large), names, or all")
        p.add_argument("--tasks", default="hpo", help="hpo, tmvar, biored (only hpo is built so far)")
        p.add_argument("--docs", default="full", choices=["full", "test24", "eval", "tune"],
                       help="full = all of ID-68 (68), GSC+ (228) and GeneReviews (10); test24 = the Kaggle subset; eval / tune = the fixed split")
        p.add_argument("--prompt-set", default=DEFAULT_PROMPT_SET, help="rag-hpo (RAG-HPO's own) or a folder under prompts/")
        p.add_argument("--concurrency", type=int, default=8, help="RAG-HPO copies sending requests to vLLM at once (recorded with results)")
        p.add_argument("--mode", default="both", choices=["as-is", "assisted", "both"])
        p.add_argument("--thinking", default="off", choices=["on", "off", "both"], help="for thinking models only")
        p.add_argument("--max-docs", type=int, default=0, help="cap documents per dataset (0 = all)")
        p.add_argument("--timeout-min", type=float, default=240, help="per model, task and dataset (full GSC+ is 228 abstracts)")
        server_args(p)
    q = sub.add_parser("consistency", help="repeatability and reproducibility of the HPO task (Shyr et al., JAMIA 2026)")
    q.add_argument("--models", required=True, help="the shortlist: model names or groups")
    q.add_argument("--prompt-sets", default=",".join([DEFAULT_PROMPT_SET] + PROMPT_SLOTS), help="rag-hpo plus folders under prompts/; empty slots are skipped")
    q.add_argument("--runs", type=int, default=20, help="runs per prompt set (set it from the pilot: 5, 10, 20, 50)")
    q.add_argument("--sample", type=int, default=60, help="documents, half from each dataset, from the evaluation split")
    q.add_argument("--sample-file", default=None, help="hand-picked documents instead, one 'id68/<doc>' or 'gscplus/<doc>' per line")
    q.add_argument("--concurrency", type=int, default=8, help="runs sent to vLLM at once; rerun a subset with 1 to measure the setup's own effect")
    q.add_argument("--temperature", type=float, default=0.5, help="the paper's setting")
    q.add_argument("--top-k", type=int, default=30, help="sampling top-k (the paper's setting)")
    q.add_argument("--logprobs-k", type=int, default=30, help="top-k token probabilities for the entropy (internal scores)")
    q.add_argument("--seed", type=int, default=1000, help="run r uses seed + r; also seeds the document sample")
    q.add_argument("--mode", default="as-is", choices=["as-is", "assisted"], help="one mode per run-id, so the two are never mixed")
    q.add_argument("--thinking", default="off", choices=["on", "off"], help="for thinking models only")
    q.add_argument("--timeout-min", type=float, default=240, help="per run of the sample")
    server_args(q)
    e = sub.add_parser("export"); e.add_argument("--run-id", default=None)
    a = ap.parse_args()
    if a.cmd == "check": sys.exit(cmd_check(a))
    if a.cmd == "splits": return cmd_splits(a)
    if a.cmd == "consistency": return cmd_consistency(a)
    if a.cmd == "install": return cmd_install(a)
    if a.cmd == "run": return cmd_run(a)
    if a.cmd == "smoke": return cmd_run(a, smoke=True)
    if a.cmd == "export": return cmd_export(a)

if __name__ == "__main__":
    main()
