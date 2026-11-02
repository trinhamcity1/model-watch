"""Weekly scout: ask Hugging Face for recently updated text models and apply fixed rules.
Writes docs/candidates.json. It downloads no model weights.
A person still reads each card; skip.json holds models already judged not worth testing."""
import json, datetime, urllib.request, urllib.parse, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWED_LICENCES = {"apache-2.0", "mit", "gemma", "llama3.3", "other"}  # 'other' needs a manual look
BAD_WORDS = ["gguf", "awq", "gptq", "mlx", "nvfp4", "fp8", "bnb", "abliterated", "uncensored", "roleplay",
             "-rp", "coder", "code", "-base", "merge", "lora", "embedding", "guard", "humaniz",
             "sentiment", "classif", "reward", "ocr", "speech", "tts", "asr", "translat", "ner-", "detector", "intent"]
MIN_B, MAX_B, MAX_AGE_DAYS = 7, 40, 60
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
    for org in GGUF_ORGS:
        for r in res:
            org_r, name = r["id"].split("/", 1)
            if org_r.lower() == org.lower() and name.lower().replace("-gguf", "") == base:
                return f"hf.co/{r['id']}:Q4_K_M", None
    return None, f"No GGUF version found from the {len(GGUF_ORGS)} publishers we search"

params = [("pipeline_tag", "text-generation"), ("sort", "trendingScore"), ("limit", "200"),
          ("expand[]", "safetensors"), ("expand[]", "lastModified"), ("expand[]", "tags"),
          ("expand[]", "downloads"), ("expand[]", "likes"), ("expand[]", "gated")]
models = get("https://huggingface.co/api/models?" + urllib.parse.urlencode(params))

now = datetime.datetime.now(datetime.timezone.utc)
keep, dropped, skipped = [], 0, 0
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
    tag, tag_note = resolve_gguf(m["id"])
    keep.append({"id": m["id"], "params_b": round(b, 1), "licence": lic, "days_since_update": age,
                 "downloads": m.get("downloads"), "likes": m.get("likes"),
                 "card": f"https://huggingface.co/{m['id']}",
                 "suggested_tag": tag, "tag_note": tag_note})

out = {"generated": now.isoformat(timespec="seconds"), "kept": len(keep), "dropped": dropped, "skipped": skipped,
       "next_step": "Not reviewed. A person reads each card: purpose, thinking switch, system prompt, medical evaluation.",
       "candidates": keep}
json.dump(out, open(ROOT / "docs" / "candidates.json", "w"), indent=1)
print(f"kept {len(keep)}, dropped {dropped}, skipped {skipped}")
