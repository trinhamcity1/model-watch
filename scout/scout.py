"""Weekly scout: ask Hugging Face for recently updated text models and apply fixed rules.
Writes docs/candidates.json. It downloads no model weights.
A person still reads each card; skip.json holds models already judged not worth testing."""
import json, datetime, urllib.request, urllib.parse, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
from compliance import check_hf, check_tag   # block list (Texas prohibited technologies) + allow list
ALLOWED_LICENCES = {"apache-2.0", "mit", "gemma", "llama3.3", "other"}  # 'other' needs a manual look
BAD_WORDS = ["gguf", "awq", "gptq", "mlx", "nvfp4", "fp8", "bnb", "abliterated", "uncensored", "roleplay",
             "-rp", "coder", "code", "-base", "merge", "lora", "embedding", "guard", "humaniz",
             "sentiment", "classif", "reward", "ocr", "speech", "tts", "asr", "translat", "ner-", "detector", "intent"]
MIN_B, MAX_B, MAX_AGE_DAYS = 1, 1000, 60   # large models are kept and listed as server-only
GGUF_ORGS = ["unsloth", "bartowski", "lmstudio-community", "ggml-org", "mradermacher", "QuantFactory", "MaziyarPanahi"]
SKIP = json.load(open(ROOT / "scout" / "skip.json"))

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "model-watch-scout"})
    return json.load(urllib.request.urlopen(req, timeout=60))

def resolve_gguf(hf_id):
    """Best guess at a GGUF repo Ollama can pull. Returns (tag, note). A wrong guess fails at pull and is caught by the run checks."""
    base = hf_id.split("/")[1].lower()
    try:
        res = get("https://huggingface.co/api/models?" + urllib.parse.urlencode(
            {"search": base, "filter": "gguf", "sort": "downloads", "limit": "20"}))
    except Exception:
        return None, "GGUF lookup failed (network error); will retry next week"
    orgs = [hf_id.split("/")[0]] + GGUF_ORGS   # the model's own publisher first: many labs ship their own GGUF
    for org in orgs:
        for r in res:
            org_r, name = r["id"].split("/", 1)
            if org_r.lower() == org.lower() and name.lower().replace("-gguf", "") == base:
                return f"hf.co/{r['id']}:Q4_K_M", None
    return None, f"No GGUF version found from the model's own publisher or the {len(GGUF_ORGS)} community publishers we search"

params = [("pipeline_tag", "text-generation"), ("sort", "trendingScore"), ("limit", "200"),
          ("expand[]", "safetensors"), ("expand[]", "lastModified"), ("expand[]", "tags"),
          ("expand[]", "downloads"), ("expand[]", "likes"), ("expand[]", "gated")]
models = get("https://huggingface.co/api/models?" + urllib.parse.urlencode(params))

now = datetime.datetime.now(datetime.timezone.utc)
keep, blocked, dropped, skipped, texas = [], [], 0, 0, 0
for m in models:
    name = m["id"].lower()
    tags = m.get("tags", [])
    if m["id"] in SKIP: skipped += 1; continue
    lic = next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), "unknown")
    total = (m.get("safetensors") or {}).get("total")
    b = total / 1e9 if total else None
    age = (now - datetime.datetime.fromisoformat(m["lastModified"].replace("Z", "+00:00"))).days
    why = []
    if any(w in name for w in BAD_WORDS) or "gguf" in tags: why.append("excluded word or format")
    if lic not in ALLOWED_LICENCES: why.append(f"licence {lic}")
    if b is None: why.append("size unknown")
    elif not (MIN_B <= b <= MAX_B): why.append(f"size {b:.0f}B")
    if age > MAX_AGE_DAYS: why.append(f"updated {age} days ago")
    if why: dropped += 1; continue
    # Compliance: no GGUF lookup and no Kaggle run unless the model and everything it is built on pass
    # bench/compliance.py (not from a Texas-listed developer, and from an approved or reviewed one).
    ok, reason = check_hf(m["id"], tags)
    if not ok and reason.startswith("blocked"):
        texas += 1; continue        # Texas list: counted only, not listed on the site
    if not ok:
        blocked.append({"id": m["id"], "params_b": round(b, 1), "licence": lic, "days_since_update": age,
                        "card": f"https://huggingface.co/{m['id']}", "blocked": reason})
        continue
    tag, tag_note = resolve_gguf(m["id"])
    if tag:   # the GGUF copy is what gets pulled, so it is checked too
        t_ok, t_why = check_tag(tag)
        if not t_ok: tag, tag_note = None, "GGUF copy not used: " + t_why
    keep.append({"id": m["id"], "params_b": round(b, 1), "licence": lic, "days_since_update": age,
                 "downloads": m.get("downloads"), "likes": m.get("likes"),
                 "card": f"https://huggingface.co/{m['id']}",
                 "suggested_tag": tag, "tag_note": tag_note})

out = {"generated": now.isoformat(timespec="seconds"), "kept": len(keep), "blocked": texas,
       "needs_review": len(blocked),
       "dropped": dropped, "skipped": skipped,
       "next_step": "Not reviewed. A person reads each card: purpose, thinking switch, system prompt, medical evaluation.",
       "candidates": keep, "blocked_models": blocked}
json.dump(out, open(ROOT / "docs" / "candidates.json", "w"), indent=1)
print(f"kept {len(keep)}, blocked {texas} (Texas list), not approved yet {len(blocked)}, dropped {dropped}, skipped {skipped}")
