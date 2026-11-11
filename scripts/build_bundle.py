#!/usr/bin/env python3
"""
Model Watch, script 1: build the offline bundle.

Run this on a machine WITH internet. It downloads everything the offline lab server needs to
benchmark open models with RAG-HPO, into one folder (optionally packed into archive parts),
with a manifest and SHA-256 checksums so the copy can be verified on the server.

What goes in the bundle
  models/      original Hugging Face model weights (safetensors) for vLLM
  hf_cache/    embedding models: SapBERT (RAG-HPO's search) and nomic-embed-text-v1.5 (consistency scores)
  repos/       RAG-HPO (plus RAG-HPO_offline.py, with the online EBI step removed), PhenoBERT (ID-68, GSC+ and
               GeneReviews data, and the specialist tool itself) and PhenoSS (ranks diseases from HPO codes)
               -- plain files, no GitHub needed on the server. The scorer is built into script 2.
  phenobert/   PhenoBERT's trained models and embeddings (Google Drive), NLTK and Stanza data, its own wheels
  tiktoken_cache/  the token-counting word list RAG-HPO would otherwise download on first use
  data/        the prebuilt HPO search index (saves ~45 minutes on the server), hp.obo (to map retired
               HPO IDs when scoring), and data/datasets/:
               tmVar 3.0 (variants), BioRED (relations/evidence). MedMentions/UMLS are out of scope (licence).
  wheels/      Python packages, downloaded for the server's Python version
  containers/  (optional) the vLLM Docker image saved as a tar file
  MANIFEST.json, SHA256SUMS, README.txt

Usage
  pip install -U huggingface_hub
  python build_bundle.py --dry-run                  # asks for the Hugging Face token (from Tri), then lists sizes
  python build_bundle.py --out /data/mw-bundle      # download everything
  python build_bundle.py --out /data/mw-bundle --groups core,large --docker --archive

Nothing here touches patient data: models, public code and public datasets only.

Compliance (Texas prohibited technologies, DIR list updated 4 Feb 2026, which UT System policy UTS 200 follows):
every model and embedding model is checked BEFORE download with bench/compliance.py, the same rules the Kaggle
screen uses. A repo is refused if its owner, its name, or any model it was fine-tuned, quantized or merged from
belongs to a developer on the Texas list, or if its developer is not on the lab's approved list and no one has
reviewed it. If the check cannot be completed (no network, unreadable card), the repo is NOT downloaded.
Refused repos are recorded in MANIFEST.json with the reason and never downloaded.

This script needs compliance.py: it is found in ../bench/ inside the model-watch repo. To run the script on
another machine, copy bench/compliance.py next to it. Without it the script stops.
"""
import argparse, hashlib, json, os, platform, shutil, subprocess, sys, time
from pathlib import Path

# ---------------------------------------------------------------------------------------------
# 1. What to download. Edit these lists. Repos are the ORIGINAL model repos (full precision,
#    for vLLM), not the 4-bit GGUF copies we used on Kaggle.
# ---------------------------------------------------------------------------------------------
MODELS = {
    # Baselines and the current leader on the 24-document test set
    "core": [
        "google/gemma-3-4b-it",                 # gated: accept the Gemma licence on Hugging Face
        "m42-health/Llama3-Med42-8B",           # gated: Llama 3 licence
        "google/gemma-3-12b-it",                # gated: Gemma terms. Replaces Qwen3 4B (dropped: Alibaba is on the
                                                #   Texas list); best new model on Kaggle (10 Nov). Pending lab review.
    ],
    # Biomedical models that failed on Kaggle; retry on the server with the official chat template
    "biomedical": [
        "google/medgemma-1.5-4b-it",            # gated: Health AI Developer Foundations terms (check exact repo name)
        "aaditya/Llama3-OpenBioLLM-8B",
        "BioMistral/BioMistral-7B",
    ],
    # Other small general models from the Model Watch list
    "small": [
        "meta-llama/Llama-3.1-8B-Instruct",     # gated
        "microsoft/Phi-4-mini-instruct",
        "mistralai/Mistral-7B-Instruct-v0.3",
        "ibm-granite/granite-3.3-8b-instruct",  # Apache 2.0. Replaces Qwen3 8B (dropped, same reason). Pending lab review.
    ],
    # Larger models that cannot run on Kaggle. Sized for the server's 2x A100 80 GB:
    # a 70B model at full precision (~140 GB) does not fit with room to run, so it comes as 4-bit AWQ (~40 GB).
    "large": [
        "google/medgemma-27b-text-it",                          # gated, ~54 GB
        "hugging-quants/Meta-Llama-3.1-70B-Instruct-AWQ-INT4",  # ~40 GB (check repo name and licence)
        "openai/gpt-oss-20b",                                   # ~14 GB
    ],
}
DEFAULT_GROUPS = ["core", "biomedical"]

# Embedding models.
# - SapBERT: what RAG-HPO uses (initialize_embeddings_model(use_sbert=True) in RAG-HPO.ipynb).
#   RAG-HPO's other option, BAAI/bge-small-en-v1.5, is NOT bundled: BAAI is on the Texas list, and RAG-HPO
#   never loads it while use_sbert=True (make_offline_rag checks that).
# - nomic-embed-text-v1.5 (Nomic AI, Apache 2.0): semantic repeatability/reproducibility scores. Shyr et al.
#   (JAMIA 2026) used MedEmbed-large-v0.1 and checked their results with nomic-embed-text-v1.5; MedEmbed is
#   fine-tuned from BAAI/bge-large-en-v1.5, so we use the paper's own second model instead.
#   nomic-bert-2048 holds the model code that nomic-embed-text-v1.5 loads (trust_remote_code).
EMBEDDING_MODELS = [
    "pritamdeka/SapBERT-mnli-snli-scinli-scitail-mednli-stsb",
    "nomic-ai/nomic-embed-text-v1.5",
    "nomic-ai/nomic-bert-2048",
]

def _load_compliance():
    """bench/compliance.py is the single source of the rules. No copy is kept here, so the two can never differ."""
    import importlib.util
    here = Path(__file__).resolve().parent
    for p in (here.parent / "bench" / "compliance.py", here / "compliance.py"):   # the repo's copy wins when present
        if p.is_file():
            spec = importlib.util.spec_from_file_location("mw_compliance", p)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            return mod, p
    sys.exit("compliance.py not found. Copy bench/compliance.py from the model-watch repo next to this script. "
             "Nothing is downloaded without the compliance rules.")

COMPLIANCE, COMPLIANCE_PATH = _load_compliance()

REPOS = {
    "RAG-HPO":     "https://github.com/PoseyPod/RAG-HPO",
    "PhenoBERT":   "https://github.com/EclipseCN/PhenoBERT",      # ID-68, GSC+, GeneReviews data + the tool (MIT)
    "PhenoSS":     "https://github.com/WGLab/PhenoSS",            # disease ranking from HPO codes (MIT); its
                                                                  # databases/ (hp.db, HPO frequencies) are in the repo
}
# PhenoBERT's trained models and embeddings are on Google Drive (links in its README), not in the repo.
PHENOBERT_DRIVE = {
    "models":     "https://drive.google.com/drive/folders/1NriTyBqh3kxUWv1lrnYjWBpYu0F0hrCh",   # HPOModel_H/
    "models/bert_model_max_triple.pkl": "https://drive.google.com/file/d/1AwRnaB5RruFUEdMkKohZmTlD4ILCkQ_z",
    "embeddings/biobert_v1.1_pubmed": "https://drive.google.com/drive/folders/10lko9BpToUl3PlUWrYbFmNyVHxDX1xby",
    "embeddings/fasttext_pubmed.bin": "https://drive.google.com/file/d/1GFB3I46B50sDUHcSpu84jZKqJnIjc--B",
}
PHENOBERT_PIP = ["nltk", "prettytable", "fasttext-wheel", "stanza", "scipy"]
HPO_INDEX_URL = "https://github.com/trinhamcity1/model-watch/releases/download/hpo-index/hpo_index.tgz"

# Datasets for the next benchmark modules. ID-68 and GSC+ (HPO) come with PhenoBERT above.
# Licence notes are what the sources state; confirm with the lab before publishing any results.
DATASETS = {
    "tmVar3": {   # variant mentions + normalized IDs, 500 PubMed abstracts, PubTator format, no splits
        "kind": "file", "url": "https://ftp.ncbi.nlm.nih.gov/pub/lu/tmVar3/tmVar3Corpus.txt",
        "task": "variant extraction", "licence": "not stated by the source (NCBI); research use, check before redistribution"},
    "BioRED": {   # entities + relations (gene, disease, variant, chemical...), 600 abstracts, PubTator, Train/Dev/Test
        "kind": "zip", "url": "https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/BIORED.zip",
        "task": "relation / evidence extraction", "licence": "not stated by the source (NCBI); research use, check before redistribution"},
}
# MedMentions (UMLS concept normalization) is out of scope for now: the UMLS concept list needs an NLM licence.

# Python packages for the server. vLLM brings its own matching PyTorch/CUDA build.
PIP_PACKAGES = [
    "vllm",
    "pronto", "fastembed", "sentence-transformers", "faiss-cpu", "tiktoken",
    "openpyxl", "pandas", "numpy", "requests", "rapidfuzz",
    "einops",                     # nomic-embed-text-v1.5
    "ssmpy", "scipy", "tqdm",     # PhenoSS
]
VLLM_IMAGE = "vllm/vllm-openai:latest"   # pin a version once the server admin confirms the server's CUDA version

# Only the files vLLM needs (skips duplicate .bin/.pth/GGUF copies some repos also ship)
ALLOW = ["*.safetensors", "*.safetensors.index.json", "*.json", "*.model", "*.tiktoken",
         "tokenizer*", "*.txt", "*.jinja", "LICENSE*", "README*"]
SKIP_DIRS = ("original/", "gguf/", "onnx/", "openvino/")


# ---------------------------------------------------------------------------------------------
def log(msg): print(time.strftime("%H:%M:%S"), msg, flush=True)

def gb(n): return f"{n / 1e9:,.1f} GB"

def run(cmd, **kw):
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)

# Older repos ship weights only as PyTorch .bin files; vLLM loads those too. Used only when a repo has no .safetensors.
ALLOW_BIN = ["pytorch_model*.bin", "pytorch_model*.bin.index.json"]

def wanted(path, patterns=None):
    from fnmatch import fnmatch
    return not path.startswith(SKIP_DIRS) and any(fnmatch(path.split("/")[-1], p) or fnmatch(path, p) for p in (patterns or ALLOW))

def repo_plan(api, repo):
    """Return (sha, files, bytes, patterns) for a model repo, or raise with a readable reason."""
    info = api.model_info(repo, files_metadata=True)
    names = [s.rfilename for s in info.siblings]
    has_st = any(n.endswith(".safetensors") and not n.startswith(SKIP_DIRS) for n in names)
    patterns = ALLOW if has_st else ALLOW + ALLOW_BIN
    files = [s for s in info.siblings if wanted(s.rfilename, patterns)]
    # Some repos (Mistral) ship the same weights twice: consolidated*.safetensors for Mistral's own tools and the
    # model*.safetensors that vLLM loads. Keep only the second when both are there.
    if any(f.rfilename.startswith("model") and f.rfilename.endswith(".safetensors") for f in files):
        files = [f for f in files if not f.rfilename.split("/")[-1].startswith("consolidated")]
    size = sum((s.size or 0) for s in files)
    if not any(f.rfilename.endswith((".safetensors", ".bin")) for f in files):
        raise RuntimeError("no model weights (.safetensors or pytorch_model*.bin) found in this repo")
    cd = getattr(info, "card_data", None) or {}
    lic = (cd.get("license_name") or cd.get("license")) if hasattr(cd, "get") else None
    return info.sha, [s.rfilename for s in files], size, patterns, lic

def get_token():
    """Token from HF_TOKEN or a saved login; otherwise ask for it in the terminal (hidden, never saved to disk)."""
    import getpass
    from huggingface_hub import HfApi
    try:
        from huggingface_hub import get_token as saved_token
    except ImportError:
        saved_token = lambda: None
    token = os.environ.get("HF_TOKEN") or saved_token()
    if token:
        log("Using the Hugging Face token already set on this machine.")
        return token
    print("\nSome models (Gemma, MedGemma, Med42, Llama) need a Hugging Face token to download.")
    print("Paste the token Tri gave you. It is hidden as you paste and is not saved anywhere.")
    print("Press Enter without a token to download only the public models.\n")
    for attempt in range(3):
        token = getpass.getpass("Hugging Face token: ").strip()
        if not token:
            log("No token: gated models will be skipped and listed as FAILED.")
            return None
        try:
            who = HfApi(token=token).whoami()
            log(f"Token OK (Hugging Face account: {who.get('name', '?')}).")
            os.environ["HF_TOKEN"] = token     # this run only; gone when the script ends
            return token
        except Exception as e:
            print(f"That token did not work ({type(e).__name__}). Check it was copied completely and try again.")
    sys.exit("No working token after 3 tries. Ask Tri for a new one, then run the script again.")

def check_all(repos, kind, manifest, token):
    """Run the compliance check; return {repo: lineage} for the repos that may be downloaded.
    Fail closed: a repo is downloaded only if the whole check completed and passed."""
    get = COMPLIANCE.make_get(token)
    allowed = {}
    for repo in repos:
        trail = []
        try:
            ok, why = COMPLIANCE.check_hf(repo, None, get, trail)
        except Exception as e:
            ok, why = False, f"unverified: the compliance check itself failed ({type(e).__name__}: {e})"[:300]
        if ok: allowed[repo] = trail
        else:
            status = "blocked" if why.startswith("blocked") else "refused"
            log(f"  {status.upper()} {repo}: {why}")
            manifest.append({"repo": repo, "kind": kind, "status": status, "reason": why})
    return allowed

def download_models(api, repos, dest, dry, manifest, groups_of=None):
    from huggingface_hub import snapshot_download
    total = 0
    for repo, lineage in check_all(repos, "model", manifest, api.token).items():
        entry = {"repo": repo, "group": (groups_of or {}).get(repo, "?"), "compliance": "passed", "lineage": lineage}
        try:
            sha, files, size, patterns, lic = repo_plan(api, repo)
            entry.update(revision=sha, files=len(files), bytes=size, licence=lic or "not stated on the card",
                         weights="safetensors" if patterns is ALLOW else "pytorch .bin")
            total += size
            log(f"{repo}: {len(files)} files, {gb(size)}, licence: {lic or 'not stated'}{'' if patterns is ALLOW else ' (PyTorch .bin weights)'}")
            if not dry:
                target = dest / repo.replace("/", "__")
                snapshot_download(repo, revision=sha, local_dir=target, allow_patterns=patterns,
                                  ignore_patterns=[d + "*" for d in SKIP_DIRS])
                entry["path"] = str(target.relative_to(dest.parent))
                entry["status"] = "ok"
        except Exception as e:   # gated without access, renamed repo, network error: record it, keep going
            entry["status"] = "failed"; entry["error"] = f"{type(e).__name__}: {e}"[:300]
            log(f"  FAILED {repo}: {entry['error']}")
        manifest.append(entry)
    return total

def download_embeddings(api, dest, dry, manifest):
    from huggingface_hub import snapshot_download
    for repo, lineage in check_all(EMBEDDING_MODELS, "embedding", manifest, api.token).items():
        entry = {"repo": repo, "kind": "embedding", "compliance": "passed", "lineage": lineage}
        try:
            if not dry:
                # Stored as a normal Hugging Face cache, so HF_HOME=<bundle>/hf_cache works offline
                snapshot_download(repo, cache_dir=dest / "hub")
            entry["status"] = "ok" if not dry else "planned"
        except Exception as e:
            entry["status"] = "failed"; entry["error"] = str(e)[:300]; log(f"  FAILED {repo}: {e}")
        manifest.append(entry)

def clone_repos(dest, manifest):
    """Plain files on the server: git is only used here, on the download machine (zip download if git is missing)."""
    dest.mkdir(parents=True, exist_ok=True)
    for name, url in REPOS.items():
        target = dest / name
        if target.exists(): shutil.rmtree(target)
        if shutil.which("git"):
            run(["git", "clone", "--quiet", "--depth", "1", url, str(target)])
            sha = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            shutil.rmtree(target / ".git", ignore_errors=True)
        else:
            import io, urllib.request, zipfile
            zurl = url.replace("https://github.com/", "https://codeload.github.com/") + "/zip/refs/heads/main"
            log(f"git not found, downloading {zurl}")
            with zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(zurl).read())) as z:
                top = z.namelist()[0].split("/")[0]; z.extractall(dest)
            (dest / top).rename(target); sha = "main (zip, commit not recorded)"
        manifest.append({"repo": url, "commit": sha, "path": f"repos/{name}"})

def make_offline_rag(rag_dir, manifest):
    """RAG-HPO ships as a notebook. Its first code cell is the whole pipeline and ends with main().
    The cells after it are the author's leftover utilities: one calls the online EBI OLS service on a file
    from the author's own computer, the other an example on a file that is not in the repo. On Kaggle they
    only crashed after the results were saved; offline they must not run at all. So we keep the pipeline
    cells and drop the rest, and save the result as a plain script (no Jupyter needed on the server)."""
    nb = json.loads((rag_dir / "RAG-HPO.ipynb").read_text())
    code = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]
    cut = next((i for i, c in enumerate(code) if "ols4" in c or "ebi.ac.uk" in c), len(code))
    keep = code[:cut]
    script = "\n\n".join(keep)
    if "main()" not in script: sys.exit("RAG-HPO layout changed: main() is not in the kept cells; check make_offline_rag")
    if "ebi.ac.uk" in script or "ols4" in script: sys.exit("RAG-HPO still contains the online EBI step")
    # RAG-HPO can also use BAAI/bge-small-en-v1.5 (Texas list). It must stay on SapBERT.
    if "initialize_embeddings_model(use_sbert=True)" not in script:
        sys.exit("RAG-HPO no longer calls initialize_embeddings_model(use_sbert=True): it may load BAAI/bge-small-en-v1.5; check before bundling")
    header = ("# RAG-HPO pipeline for offline use, made by Model Watch build_bundle.py from RAG-HPO.ipynb.\n"
              f"# Kept code cells 1-{cut} of {len(code)}; dropped the online EBI OLS lookup and the author's example cells.\n\n")
    (rag_dir / "RAG-HPO_offline.py").write_text(header + script + "\n")
    nbv = json.loads((rag_dir / "HPO_Vectorization.ipynb").read_text())   # fallback if the saved index is ever unusable
    (rag_dir / "HPO_Vectorization.py").write_text("\n\n".join("".join(c["source"]) for c in nbv["cells"] if c["cell_type"] == "code") + "\n")
    manifest.append({"rag_hpo_offline": "repos/RAG-HPO/RAG-HPO_offline.py", "kept_cells": cut, "dropped_cells": len(code) - cut})
    log(f"RAG-HPO_offline.py written: kept {cut} of {len(code)} code cells (online EBI step removed)")

def fetch_datasets(dest, manifest):
    import io, urllib.request, zipfile
    dest.mkdir(parents=True, exist_ok=True)
    for name, d in DATASETS.items():
        target = dest / name
        if target.exists(): shutil.rmtree(target)
        entry = {"dataset": name, "task": d["task"], "source": d["url"], "licence": d["licence"], "path": f"data/datasets/{name}"}
        try:
            if d["kind"] == "git":
                run(["git", "clone", "--quiet", "--depth", "1", d["url"], str(target)])
                entry["commit"] = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
                shutil.rmtree(target / ".git", ignore_errors=True)
            else:
                target.mkdir(parents=True)
                log(f"downloading {d['url']}")
                blob = urllib.request.urlopen(d["url"], timeout=300).read()
                if d["kind"] == "zip":
                    zipfile.ZipFile(io.BytesIO(blob)).extractall(target)
                else:
                    (target / d["url"].rsplit("/", 1)[-1]).write_bytes(blob)
            entry["files"] = sum(1 for p in target.rglob("*") if p.is_file())
            entry["status"] = "ok"
        except Exception as e:
            entry["status"] = "failed"; entry["error"] = f"{type(e).__name__}: {e}"[:300]; log(f"  FAILED {name}: {entry['error']}")
        manifest.append(entry)

def fetch_phenobert(root, py, manifest):
    """Everything PhenoBERT needs offline, best effort (a failure here is recorded and does not stop the bundle):
    trained models + embeddings from Google Drive (into repos/PhenoBERT/phenobert/), NLTK and Stanza data,
    and its Python packages in a separate wheels/phenobert/ folder (it runs in its own environment)."""
    pb = root / "repos" / "PhenoBERT" / "phenobert"
    entry = {"phenobert_assets": {}}
    try:
        import gdown
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "-q", "gdown"]); import gdown
    for rel, url in PHENOBERT_DRIVE.items():
        target = pb / rel
        try:
            if "/folders/" in url:
                target.mkdir(parents=True, exist_ok=True)
                gdown.download_folder(url, output=str(target), quiet=True, use_cookies=False)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                gdown.download(url, output=str(target), quiet=True, fuzzy=True)
            entry["phenobert_assets"][rel] = "ok"
        except Exception as e:
            entry["phenobert_assets"][rel] = f"failed: {type(e).__name__}: {e}"[:200]
            log(f"  FAILED PhenoBERT {rel}: {e} -- download it by hand from {url}")
    nl, st = root / "phenobert_env" / "nltk_data", root / "phenobert_env" / "stanza_resources"
    try:
        for pkg in ("nltk", "stanza"):
            try: __import__(pkg)
            except ImportError: run([sys.executable, "-m", "pip", "install", "-q", pkg])
        import nltk, stanza
        for r in ("stopwords", "wordnet", "averaged_perceptron_tagger", "punkt"):
            nltk.download(r, download_dir=str(nl), quiet=True)
        stanza.download("en", package="mimic", processors={"ner": "i2b2"}, model_dir=str(st), verbose=False)
        entry["nltk_stanza"] = "ok"
    except Exception as e:
        entry["nltk_stanza"] = f"failed: {type(e).__name__}: {e}"[:200]; log(f"  FAILED NLTK/Stanza data: {e}")
    wd = root / "wheels" / "phenobert"; wd.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "pip", "download", *PHENOBERT_PIP, "-d", str(wd),
                        "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
                        "--python-version", py, "--only-binary=:all:"])
    entry["wheels"] = "ok" if r.returncode == 0 else "failed: some packages have no wheel for this Python; build them on the server"
    manifest.append(entry)

def fetch_tiktoken(dest, manifest):
    """RAG-HPO counts tokens with tiktoken, which downloads its word list on first use. Cache it in the bundle."""
    dest.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(dest)
    try:
        import tiktoken
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "-q", "tiktoken"]); import tiktoken
    tiktoken.get_encoding("cl100k_base")
    manifest.append({"tiktoken": "cl100k_base", "path": "tiktoken_cache"})

HPO_OBO_URLS = ["https://purl.obolibrary.org/obo/hp.obo",
                "https://github.com/obophenotype/human-phenotype-ontology/releases/latest/download/hp.obo"]

def fetch_index(dest, manifest):
    dest.mkdir(parents=True, exist_ok=True)
    run(["curl", "-fL", "--retry", "3", "-o", str(dest / "hpo_index.tgz"), HPO_INDEX_URL])
    manifest.append({"url": HPO_INDEX_URL, "path": "data/hpo_index.tgz"})
    # The HPO ontology itself: lets the scorer map retired HPO IDs (alt_id / replaced_by) in old gold files
    for url in HPO_OBO_URLS:
        if subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(dest / "hp.obo"), url]).returncode == 0:
            ver = next((l.split(":", 1)[1].strip() for l in open(dest / "hp.obo", errors="ignore") if l.startswith("data-version:")), "unknown")
            manifest.append({"url": url, "path": "data/hp.obo", "hpo_version": ver}); log(f"hp.obo downloaded ({ver})"); return
    log("WARNING: could not download hp.obo; scores will not map retired HPO IDs")

def download_wheels(dest, py, manifest):
    dest.mkdir(parents=True, exist_ok=True)
    reqs = list(PIP_PACKAGES)
    rag_req = dest.parent / "repos" / "RAG-HPO" / "requirements.txt"
    if rag_req.exists():
        reqs += [l.strip() for l in rag_req.read_text().splitlines() if l.strip() and not l.startswith("#")]
    (dest / "requirements-bundle.txt").write_text("\n".join(sorted(set(reqs))) + "\n")
    run([sys.executable, "-m", "pip", "download", "-r", str(dest / "requirements-bundle.txt"), "-d", str(dest),
         "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
         "--python-version", py, "--only-binary=:all:"])
    manifest.append({"pip": "wheels/requirements-bundle.txt", "python": py,
                     "wheels": len(list(dest.glob("*.whl")))})

def save_docker(dest, manifest):
    dest.mkdir(parents=True, exist_ok=True)
    run(["docker", "pull", VLLM_IMAGE])
    tar = dest / "vllm-openai.tar"
    run(["docker", "save", "-o", str(tar), VLLM_IMAGE])
    manifest.append({"image": VLLM_IMAGE, "path": "containers/vllm-openai.tar"})

def checksums(root):
    log("Writing SHA256SUMS (this reads every file once)...")
    lines = []
    for p in sorted(root.rglob("*")):
        # scripts 2 and 3 are left out on purpose: they can be replaced on the server with newer versions
        if p.is_file() and p.name not in ("SHA256SUMS", "run_benchmark.py", "run_enhancements.py") and ".git/" not in str(p):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 24), b""): h.update(chunk)
            lines.append(f"{h.hexdigest()}  {p.relative_to(root)}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n")

def archive(root, part_gb):
    log(f"Packing into {part_gb} GB parts...")
    base = root.parent / (root.name + ".tar")
    run(["bash", "-c", f"tar -C '{root.parent}' -cf - '{root.name}' | split -b {part_gb}G -d - '{base}.part'"])
    log(f"Done: {base}.part00, part01, ... Rejoin on the server with: cat {base.name}.part* | tar -xf -")

README = """Model Watch offline bundle
==========================
Built: {built} on {host}
Groups: {groups}

Verify after copying to the server:
    cd <bundle> && sha256sum -c SHA256SUMS --quiet && echo OK

Contents: see MANIFEST.json (model revisions, repo commits, dataset sources and licence notes, failures).

Compliance
  Every model and embedding model was checked before download with compliance.py (in this folder): not from a
  developer on the Texas prohibited technologies list (DIR, 4 Feb 2026), not built on such a model, and from a
  developer the lab approved or a repo a person reviewed. Each model's checked lineage is in MANIFEST.json;
  anything refused is listed there as "blocked" or "refused" with the reason and was not downloaded.
  Script 2 repeats the check offline before it runs anything. Do not add model folders to this bundle by hand.

Datasets
  HPO extraction          repos/PhenoBERT/phenobert/data/ID-68, GSC+ (GeneReviews: 10 cases with a known disease)
  Variant extraction      data/datasets/tmVar3/tmVar3Corpus.txt          (500 abstracts, PubTator)
  Relations / evidence    data/datasets/BioRED/                          (600 abstracts, Train/Dev/Test PubTator)
Next step: copy script 2 (run_benchmark.py) and script 3 (run_enhancements.py) into this folder separately, then run script 2. It installs from wheels/ or containers/, serves each model
with vLLM, runs RAG-HPO on the test documents and writes results/.

Offline notes
  - Set these before running anything, so nothing tries to reach the internet:
        export HF_HOME=<bundle>/hf_cache HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
        export TIKTOKEN_CACHE_DIR=<bundle>/tiktoken_cache
  - Run repos/RAG-HPO/RAG-HPO_offline.py, not the notebook: it is the same pipeline with the online
    EBI OLS lookup (and the author's leftover example cells) removed. No Jupyter needed.
  - PhenoSS runs offline only with --mode hpodb_only (the other modes call the online OARD service).
  - PhenoBERT runs in its own environment (wheels/phenobert/), with
        export NLTK_DATA=<bundle>/phenobert_env/nltk_data STANZA_RESOURCES_DIR=<bundle>/phenobert_env/stanza_resources
        export TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1   # its model files are whole pickled objects (PyTorch >= 2.6)
    Its .pkl files come from Google Drive and are loaded with pickle: run it only inside the container,
    and check the checksums in MANIFEST.json/SHA256SUMS first.
"""

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="mw-bundle", help="bundle folder (needs room for all models)")
    ap.add_argument("--groups", default=",".join(DEFAULT_GROUPS), help=f"model groups: {', '.join(MODELS)}")
    ap.add_argument("--python", default="3.12", help="Python version ON THE SERVER, for the wheels")
    ap.add_argument("--dry-run", action="store_true", help="only list what would be downloaded")
    ap.add_argument("--docker", action="store_true", help="also save the vLLM Docker image")
    ap.add_argument("--no-wheels", action="store_true", help="skip pip wheels (e.g. when using the Docker image)")
    ap.add_argument("--archive", action="store_true", help="pack the bundle into tar parts for transfer")
    ap.add_argument("--part-gb", type=int, default=50)
    ap.add_argument("--max-gb", type=float, default=1500, help="storage budget on the server (lab guidance: stay under ~1.5-2 TB)")
    ap.add_argument("--no-phenobert", action="store_true", help="skip PhenoBERT's models, NLTK/Stanza data and wheels")
    a = ap.parse_args()
    log(f"Compliance rules: {COMPLIANCE_PATH} (version {COMPLIANCE.VERSION}; {sum(len(v) for v in COMPLIANCE.PROHIBITED_ORGS.values())} "
        f"blocked organisations, {len(COMPLIANCE.APPROVED_ORGS)} approved developers, {len(COMPLIANCE.REVIEWED)} reviewed repos)")

    from huggingface_hub import HfApi
    token = get_token()
    api = HfApi(token=token)
    groups = [g.strip() for g in a.groups.split(",") if g.strip()]
    unknown = [g for g in groups if g not in MODELS]
    if unknown: sys.exit(f"Unknown group(s) {unknown}; choose from {list(MODELS)}")
    repos = [r for g in groups for r in MODELS[g]]

    root = Path(a.out).resolve()
    manifest = {"built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "groups": groups,
                "compliance": {"list": "Texas DIR prohibited technologies", "list_updated": COMPLIANCE.DIR_LIST_DATE,
                               "rules_version": COMPLIANCE.VERSION, "rules_file": "compliance.py",
                               "rules_sha256": hashlib.sha256(COMPLIANCE_PATH.read_bytes()).hexdigest()},
                "models": [], "embeddings": [], "repos": [], "data": [], "wheels": [], "containers": []}

    log(f"{'DRY RUN: ' if a.dry_run else ''}{len(repos)} models from groups {groups}")
    groups_of = {r: g for g in groups for r in MODELS[g]}
    total = download_models(api, repos, root / "models", a.dry_run, manifest["models"], groups_of)
    log(f"Model weights total: {gb(total)} (plus ~1 GB embeddings, ~75 MB HPO index, ~5-10 GB wheels or Docker image)")
    free = shutil.disk_usage(root.parent if root.parent.exists() else Path.cwd()).free
    log(f"Free disk here: {gb(free)}")
    need = total + 10e9   # weights + ~10 GB for embeddings, index, wheels
    if need > a.max_gb * 1e9:
        log(f"WARNING: about {gb(need)} needed, over the {a.max_gb:g} GB server budget. Use fewer groups or ask for more space.")
    failed = [m["repo"] for m in manifest["models"] if m.get("status") == "failed"]
    blocked = [m["repo"] for m in manifest["models"] if m.get("status") in ("blocked", "refused")]
    if a.dry_run:
        check_all(EMBEDDING_MODELS, "embedding", manifest["embeddings"], token)
        blocked += [m["repo"] for m in manifest["embeddings"] if m.get("status") in ("blocked", "refused")]
        if blocked: log(f"Refused by the compliance check (will not be downloaded): {blocked}")
        if failed: log(f"Would fail (check access/licence or repo name): {failed}")
        return
    if total * 1.1 > free: log("WARNING: probably not enough disk space for the bundle.")

    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(COMPLIANCE_PATH, root / "compliance.py")   # script 2 re-checks the bundle with these same rules, offline
    download_embeddings(api, root / "hf_cache", False, manifest["embeddings"])
    clone_repos(root / "repos", manifest["repos"])
    make_offline_rag(root / "repos" / "RAG-HPO", manifest["repos"])
    if not a.no_phenobert: fetch_phenobert(root, a.python, manifest["repos"])
    fetch_datasets(root / "data" / "datasets", manifest["data"])
    fetch_tiktoken(root / "tiktoken_cache", manifest["data"])
    fetch_index(root / "data", manifest["data"])
    if not a.no_wheels: download_wheels(root / "wheels", a.python, manifest["wheels"])
    if a.docker: save_docker(root / "containers", manifest["containers"])

    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    (root / "README.txt").write_text(README.format(built=manifest["built"], host=platform.node(), groups=", ".join(groups)))
    checksums(root)
    blocked += [m["repo"] for m in manifest["embeddings"] if m.get("status") in ("blocked", "refused")]
    if blocked: log(f"Refused by the compliance check (not downloaded): {blocked}")
    if failed: log(f"Finished WITH FAILURES (see MANIFEST.json): {failed}")
    else: log(f"Finished: {root}")
    if token: log("Done with the token: tell Tri, so he can delete it in his Hugging Face settings.")
    if a.archive: archive(root, a.part_gb)

if __name__ == "__main__":
    main()
