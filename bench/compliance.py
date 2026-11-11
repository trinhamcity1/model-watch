"""Compliance filter: only models from approved developers are screened, downloaded or run.

UT System (UTS 200) follows the list kept by the Texas Department of Information Resources (DIR):
https://dir.texas.gov/information-security/covered-applications-and-prohibited-technologies (updated 4 Feb 2026).
It covers the named companies and "any subsidiary or affiliate".

Two layers, both must pass:
  1. Block list. A model is refused when its Hugging Face owner is a listed developer, when its name, tag or
     repo id contains a listed model family (Qwen, DeepSeek, ...), or when any model it was fine-tuned,
     quantized, distilled or merged from is (Hugging Face base_model metadata, followed to the root).
  2. Allow list. Every root of that lineage (a model with no base model) must come from APPROVED_ORGS, or be
     in REVIEWED with a note by the person who read its card. Ollama library tags (no Hugging Face repo to
     follow) must be in APPROVED_OLLAMA. Anything else is refused as "needs review".
Anything that cannot be read is refused as "unverified" (fail closed) and checked again next run.

Reasons start with "blocked:" (Texas list), "needs review:" (not approved yet) or "unverified:".

This file is the single source. scripts/build_bundle.py loads it (from the repo, or a copy placed beside the
script) and copies it into the bundle; scripts/run_benchmark.py loads the bundle's copy for its offline check.
When DIR updates its list, edit PROHIBITED_ORGS and the FAMILY_ lists here, then run bench/test_compliance.py."""
import json, os, re, urllib.error, urllib.request
from pathlib import Path

VERSION = "2026-11-11"            # change whenever a list below changes; recorded in MANIFEST.json
DIR_LIST_DATE = "2026-02-04"

# Developers on the DIR list (software and hardware lists) with their Hugging Face organisations, lower case.
# An organisation name that does not exist on Hugging Face is harmless here.
PROHIBITED_ORGS = {
    "Alibaba (incl. Qwen)":   ["qwen", "alibaba-nlp", "alibaba-pai", "alibaba-damo-academy", "alibaba-damo", "aidc-ai",
                               "alibaba", "damo-nlp-sg", "damo-nlp-mt", "modelscope", "iic", "wan-ai", "ali-vilab",
                               "funaudiollm", "tongyi-mai", "tongyi-zhiwen", "ofa-sys"],
    "Ant Group (Alibaba affiliate; treated as covered)": ["inclusionai", "antgroup", "ant-research"],
    "Baichuan":               ["baichuan-inc"],
    "Baidu":                  ["baidu", "paddlepaddle"],
    "BAAI":                   ["baai"],
    "ByteDance":              ["bytedance", "bytedance-seed", "bytedance-research", "bytedancespeech"],
    "CloudWalk":              ["cloudwalk"],
    "DeepSeek":               ["deepseek-ai", "deepseek-community"],
    "Huawei":                 ["huawei", "huawei-noah"],
    "iFlytek":                ["iflytek"],
    "Kaspersky":              ["kaspersky", "kasperskylab"],
    "Megvii":                 ["megvii", "megvii-research"],
    "MiniMax":                ["minimaxai"],
    "Moonshot AI":            ["moonshotai"],
    "SenseTime":              ["sensetime", "sensenova", "opensensenova"],
    "StepFun":                ["stepfun-ai"],
    "Tencent":                ["tencent", "tencent-hunyuan", "tencentarc", "tencent-ailab", "tencentbac", "tencentgamemate"],
    "Xiaomi":                 ["xiaomimimo", "xiaomi"],
    "Yitu":                   ["yitu"],
    "Zhipu / Z.ai":           ["zai-org", "thudm", "zhipuai"],
}
_BLOCKED_ORGS = {org: dev for dev, orgs in PROHIBITED_ORGS.items() for org in orgs}

# Distinctive names: refused anywhere inside a name, tag or repo id (so codeqwen, MyQwen, DeepSeekR1 are caught).
FAMILY_SUBSTR = {"qwen": "Alibaba (incl. Qwen)", "qwq": "Alibaba (incl. Qwen)", "tongyi": "Alibaba (incl. Qwen)",
                 "alibaba": "Alibaba (incl. Qwen)", "deepseek": "DeepSeek", "chatglm": "Zhipu / Z.ai",
                 "zhipu": "Zhipu / Z.ai", "codegeex": "Zhipu / Z.ai", "cogvlm": "Zhipu / Z.ai", "moonshot": "Moonshot AI",
                 "minimax": "MiniMax", "baichuan": "Baichuan", "hunyuan": "Tencent", "tencent": "Tencent",
                 "doubao": "ByteDance", "bytedance": "ByteDance", "baidu": "Baidu", "iflytek": "iFlytek",
                 "sensetime": "SenseTime", "sensenova": "SenseTime", "stepfun": "StepFun", "xiaomi": "Xiaomi",
                 "huawei": "Huawei"}
# Short names: refused only as a whole word (letters between digits and punctuation), to avoid false hits.
FAMILY_WORDS = {"glm": "Zhipu / Z.ai", "kimi": "Moonshot AI", "ernie": "Baidu", "mimo": "Xiaomi", "bge": "BAAI",
                "gte": "Alibaba (incl. Qwen)", "tars": "ByteDance", "pangu": "Huawei",
                "ling": "Ant Group (Alibaba affiliate; treated as covered)",
                "ring": "Ant Group (Alibaba affiliate; treated as covered)"}

# Developers whose own models may be screened and bundled without a person reading the card first.
# The lab decides this list; add a developer only after checking it is not a subsidiary or affiliate of a listed one.
APPROVED_ORGS = {"google", "meta-llama", "facebook", "microsoft", "mistralai", "ibm-granite", "openai", "nvidia",
                 "nomic-ai", "allenai", "liquidai", "cambridgeltl"}

# Single repos a person has read and cleared, for models whose card names no base model or whose developer is
# not in APPROVED_ORGS. The block list still applies to them. Value: what was checked, by whom, when.
REVIEWED = {
    "m42-health/Llama3-Med42-8B": "M42 Health (UAE), fine-tune of Meta Llama 3 8B per its card. Added 2026-11-11; confirm on the card.",
    "aaditya/Llama3-OpenBioLLM-8B": "Saama, fine-tune of Meta Llama 3 8B per its card. Added 2026-11-11; confirm on the card.",
    "BioMistral/BioMistral-7B": "Academic (Avignon/Nantes), further-trained Mistral 7B Instruct per its card. Added 2026-11-11; confirm on the card.",
    "hugging-quants/Meta-Llama-3.1-70B-Instruct-AWQ-INT4": "Hugging Face staff quantization of Meta Llama 3.1 70B. Added 2026-11-11; confirm on the card.",
    "pritamdeka/SapBERT-mnli-snli-scinli-scitail-mednli-stsb": "Independent, built on cambridgeltl SapBERT (PubMedBERT). Added 2026-11-11; confirm on the card.",
}
_REVIEWED = {k.lower() for k in REVIEWED}

# Ollama library families (the part before ':'), each published by an approved developer. An Ollama library tag has
# no base-model record to follow, so any tag not listed here needs a person to check it first.
APPROVED_OLLAMA = {"llama3", "llama3.1", "llama3.2", "llama3.3", "gemma3", "gemma3n", "phi4", "phi4-mini", "mistral",
                   "mistral-nemo", "mistral-small", "granite3.3", "gpt-oss", "olmo2"}

MAX_DEPTH = 6
_WORD = re.compile(r"[a-z]+")
_TEXAS = "on the Texas prohibited technologies list"


def make_get(token=None):
    """A function that reads Hugging Face's JSON API. A token is only needed for private repos."""
    token = token or os.environ.get("HF_TOKEN")
    def get(url):
        headers = {"User-Agent": "model-watch-compliance"}
        if token: headers["Authorization"] = "Bearer " + token
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60))
    return get

def check_name(*names):
    """(ok, reason) from names, tags or repo ids alone. Never a reason to allow a model: only a reason to refuse it."""
    for n in names:
        low = (n or "").lower()
        for s, dev in FAMILY_SUBSTR.items():
            if s in low: return False, f"blocked: '{n}' names {s} ({dev}, {_TEXAS})"
        for w in _WORD.findall(low):
            if w in FAMILY_WORDS: return False, f"blocked: '{n}' names {w} ({FAMILY_WORDS[w]}, {_TEXAS})"
    return True, None

def check_owner(hf_id):
    org = hf_id.split("/")[0].lower()
    if org in _BLOCKED_ORGS: return False, f"blocked: {hf_id} ({_BLOCKED_ORGS[org]}, {_TEXAS})"
    return True, None

def _parents(info):
    """Base models from tags such as base_model:finetune:Qwen/Qwen3-8B and from the card's base_model field."""
    out = {t.split(":")[-1] for t in info.get("tags") or [] if t.startswith("base_model:")}
    bm = (info.get("cardData") or {}).get("base_model") or []
    out.update([bm] if isinstance(bm, str) else bm)
    return sorted(p for p in out if isinstance(p, str) and "/" in p)

def check_hf(hf_id, tags=None, get=None, trail=None, _depth=0):
    """(ok, reason) for a Hugging Face repo id. `tags` saves one API call when the caller already has them.
    `trail`, if given, collects every repo that was checked (the lineage), for the record."""
    get = get or make_get()
    trail = trail if trail is not None else []
    if hf_id.count("/") != 1: return False, f"unverified: '{hf_id}' is not an owner/name Hugging Face id"
    if hf_id in trail: return True, None                      # already checked on this walk
    if _depth > MAX_DEPTH: return False, f"unverified: the base-model chain of {hf_id} is deeper than {MAX_DEPTH}; check it by hand"
    trail.append(hf_id)
    ok, why = check_owner(hf_id)
    if ok: ok, why = check_name(hf_id)
    if not ok: return False, why
    if tags is not None: info = {"tags": tags}
    else:
        try: info = get(f"https://huggingface.co/api/models/{hf_id}")
        except urllib.error.HTTPError as e:
            if e.code != 404 or _depth == 0:
                return False, f"unverified: could not read {hf_id} on Hugging Face (HTTP {e.code}); checked again next run"
            # A base model named on a card but not on Hugging Face (e.g. google/gemma-3-12b). No weights can come from
            # it; its owner and name already passed above, and it is treated as a root: approved developer or review.
            info = {}
        except Exception as e:
            return False, f"unverified: could not read {hf_id} on Hugging Face ({type(e).__name__}); checked again next run"
        if not isinstance(info, dict): return False, f"unverified: unexpected reply for {hf_id}"
    parents = [p for p in _parents(info) if p != hf_id]
    reviewed = hf_id.lower() in _REVIEWED
    for p in parents:
        ok, why = check_hf(p, None, get, trail, _depth + 1)
        if ok or (reviewed and why.startswith("needs review")): continue   # a review clears "not approved yet", never a block
        kind, rest = why.split(": ", 1)
        return False, f"{kind}: {hf_id} -> {rest}"
    if reviewed: return True, None
    if not parents and hf_id.split("/")[0].lower() not in APPROVED_ORGS:
        return False, (f"needs review: {hf_id} names no base model and its developer is not on the approved list; "
                       "read its card, then add the developer to APPROVED_ORGS or the repo to REVIEWED in bench/compliance.py")
    return True, None

def hf_repo_of_tag(tag):
    """'hf.co/owner/name:Q4_K_M' -> 'owner/name'; None for an Ollama library tag."""
    m = re.match(r"(?:https?://)?(?:hf\.co|huggingface\.co)/([^/\s:]+/[^/\s:]+)", tag or "")
    return m.group(1) if m else None

def check_tag(tag, get=None, trail=None):
    """(ok, reason) for what `ollama pull <tag>` would download."""
    ok, why = check_name(tag)
    if not ok: return ok, why
    repo = hf_repo_of_tag(tag)
    if repo: return check_hf(repo, None, get, trail)
    family = (tag or "").split(":")[0].lower()
    if "/" in family or family not in APPROVED_OLLAMA:
        return False, (f"needs review: '{tag}' is not an approved Ollama library model; check who publishes it and what "
                       "it is built on, then add its family to APPROVED_OLLAMA in bench/compliance.py")
    return True, None

def check_model(m, get=None):
    """(ok, reason) for an entry of docs/models.json: its names, its Hugging Face repo, and the tag that is pulled."""
    ok, why = check_name(m.get("name"), m.get("ollama_tag"), m.get("hf"))
    if ok and m.get("hf"): ok, why = check_hf(m["hf"], None, get)
    if ok and m.get("ollama_tag"): ok, why = check_tag(m["ollama_tag"], get)
    if ok and not m.get("hf") and not m.get("ollama_tag"): ok, why = False, "needs review: no Hugging Face id and no tag to check"
    return ok, why

_CONFIG_KEYS = ("model_type", "architectures", "_name_or_path", "name_or_path", "base_model_name_or_path", "auto_map")

def check_offline(repo, model_dir=None):
    """(ok, reason) with no network, for the server: owner, name, and what the model's own config files say it is."""
    ok, why = check_owner(repo)
    if ok: ok, why = check_name(repo)
    if not ok: return ok, why
    if model_dir:
        for fn in ("config.json", "adapter_config.json", "tokenizer_config.json"):
            p = Path(model_dir) / fn
            if not p.is_file(): continue
            try: cfg = json.loads(p.read_text(errors="ignore"))
            except Exception: return False, f"unverified: {fn} of {repo} could not be read"
            for k in _CONFIG_KEYS + ("tokenizer_class",):
                if k in cfg:
                    ok, why = check_name(json.dumps(cfg[k]))
                    if not ok: return False, f"blocked: {repo}'s {fn} says {k} = {json.dumps(cfg[k])[:80]} ({why.split('(', 1)[-1]}"
    return True, None

def guard():
    """The name rules as plain data, for the Kaggle kernel's last check before each pull."""
    return {"version": VERSION, "substr": sorted(FAMILY_SUBSTR), "words": sorted(FAMILY_WORDS),
            "orgs": sorted(_BLOCKED_ORGS), "ollama": sorted(APPROVED_OLLAMA)}
