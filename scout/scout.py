"""Weekly scout: ask Hugging Face for recently updated text models and apply fixed rules.
Writes docs/candidates.json. It does not download any model.
Rules are plain code so the result is repeatable. A person still reads the cards."""
import json, datetime, urllib.request, urllib.parse, pathlib

ALLOWED_LICENCES = {"apache-2.0", "mit", "gemma", "llama3.3", "other"}  # 'other' needs a manual look
BAD_WORDS = ["gguf", "awq", "gptq", "mlx", "nvfp4", "fp8", "bnb", "abliterated", "uncensored", "roleplay",
             "-rp", "coder", "code", "-base", "merge", "lora", "embedding", "guard"]
MIN_B, MAX_B, MAX_AGE_DAYS = 7, 40, 120

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "model-watch-scout"})
    return json.load(urllib.request.urlopen(req, timeout=60))

params = [("pipeline_tag", "text-generation"), ("sort", "trendingScore"), ("limit", "100"),
          ("expand[]", "safetensors"), ("expand[]", "lastModified"), ("expand[]", "tags"),
          ("expand[]", "downloads"), ("expand[]", "likes"), ("expand[]", "gated")]
models = get("https://huggingface.co/api/models?" + urllib.parse.urlencode(params))

now = datetime.datetime.now(datetime.timezone.utc)
keep, dropped = [], 0
for m in models:
    name = m["id"].lower()
    tags = m.get("tags", [])
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
    keep.append({"id": m["id"], "params_b": round(b, 1), "licence": lic, "days_since_update": age,
                 "downloads": m.get("downloads"), "likes": m.get("likes"),
                 "card": f"https://huggingface.co/{m['id']}"})

out = {"generated": now.isoformat(timespec="seconds"), "kept": len(keep), "dropped": dropped,
       "next_step": "A person reads each card: purpose, thinking switch, system prompt, vLLM support, medical evaluation.",
       "candidates": keep}
path = pathlib.Path(__file__).resolve().parent.parent / "docs" / "candidates.json"
json.dump(out, open(path, "w"), indent=1)
print(f"kept {len(keep)}, dropped {dropped}")
