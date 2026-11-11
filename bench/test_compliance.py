"""Tests for bench/compliance.py. No network: Hugging Face replies are simulated.
Run: python bench/test_compliance.py  (the scout and benchmark workflows run it before anything else)."""
import sys, json, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import compliance as c

CARDS = {   # repo -> base models, as Hugging Face would report them
    "google/gemma-3-4b-it": [], "meta-llama/Llama-3.1-8B-Instruct": [], "mistralai/Mistral-7B-Instruct-v0.3": [],
    "unsloth/gemma-3-4b-it-GGUF": ["google/gemma-3-4b-it"],
    "m42-health/Llama3-Med42-8B": ["meta-llama/Meta-Llama-3-8B-Instruct"], "meta-llama/Meta-Llama-3-8B-Instruct": [],
    "someuser/helper-8b": ["Qwen/Qwen3-8B"], "someuser/helper-8b-GGUF": ["someuser/helper-8b"],
    "someuser/mystery-8b": [], "bartowski/mystery-8b-GGUF": ["someuser/mystery-8b"],
    "someuser/merge": ["google/gemma-3-4b-it", "deepseek-ai/DeepSeek-R1"],
    "pritamdeka/SapBERT-mnli-snli-scinli-scitail-mednli-stsb": [],
    "abhinand/MedEmbed-large-v0.1": ["BAAI/bge-large-en-v1.5"],
    **{f"u/l{i}": [f"u/l{i + 1}"] for i in range(9)}, "u/l9": [],
}
MISSING = {"google/gemma-3-12b", "someuser/ghost-base"}   # named as base models but not on Hugging Face
CARDS.update({"google/gemma-3-12b-it": ["google/gemma-3-12b-pt"], "google/gemma-3-12b-pt": ["google/gemma-3-12b"],
              "someuser/ghost-ft": ["someuser/ghost-base"], "someuser/qwen-ghost-ft": ["Qwen/Qwen9-Ghost"]})
MISSING.add("Qwen/Qwen9-Ghost")
def get(url):
    repo = url.split("/api/models/")[1]
    if repo in MISSING:
        import urllib.error; raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
    if repo not in CARDS: raise OSError("network")
    return {"tags": [f"base_model:finetune:{p}" for p in CARDS[repo]]}

fails = []
def expect(label, got, want_ok, want_kind=None):
    ok, why = got
    good = ok == want_ok and (want_kind is None or (why or "").startswith(want_kind))
    if not good: fails.append(f"{label}: got {got}, wanted ok={want_ok} {want_kind or ''}")

# Names that must be refused wherever they appear
for n in ["qwen3:8b", "codeqwen:7b", "MyQwen-8B", "QwenLong-L1-32B", "DeepSeekR1-Distill", "deepseekcoder", "deepseek-r1:8b",
          "qwq:32b", "glm4:9b", "GLM-5.3", "kimi-k2", "bge-m3", "gte-large", "Ling-3.0-tiny", "MiMo-V2.6", "hunyuan-a13b",
          "tongyi-deepresearch", "ui-tars", "cogvlm2", "codegeex4", "ernie-4.5", "hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M"]:
    expect("name " + n, c.check_name(n), False, "blocked")
for n in ["llama3.1:8b", "gemma3:12b", "granite3.3:8b", "spring-8b", "Sterling-7B", "phi4-mini", "gpt-oss:20b", "Engine-8B"]:
    expect("name " + n, c.check_name(n), True)

# Owners
for o in ["Qwen", "Alibaba-NLP", "Wan-AI", "ali-vilab", "iic", "deepseek-ai", "THUDM", "zai-org", "BAAI", "tencent",
          "Tencent-AILab", "ByteDance-Seed", "moonshotai", "MiniMaxAI", "stepfun-ai", "XiaomiMiMo", "huawei-noah",
          "inclusionAI", "baidu", "sensenova", "megvii"]:
    expect("owner " + o, c.check_hf(o + "/x", None, get), False, "blocked")

# Lineage and the allow list
expect("approved developer", c.check_hf("google/gemma-3-4b-it", None, get), True)
expect("quantized copy of an approved model", c.check_hf("unsloth/gemma-3-4b-it-GGUF", None, get), True)
expect("reviewed fine-tune", c.check_hf("m42-health/Llama3-Med42-8B", None, get), True)
expect("reviewed, no base model", c.check_hf("pritamdeka/SapBERT-mnli-snli-scinli-scitail-mednli-stsb", None, get), True)
expect("fine-tune of Qwen under a neutral name", c.check_hf("someuser/helper-8b", None, get), False, "blocked")
expect("GGUF of that fine-tune", c.check_hf("someuser/helper-8b-GGUF", None, get), False, "blocked")
expect("merge with one listed parent", c.check_hf("someuser/merge", None, get), False, "blocked")
expect("MedEmbed (built on BGE)", c.check_hf("abhinand/MedEmbed-large-v0.1", None, get), False, "blocked")
expect("unknown developer, no base model", c.check_hf("someuser/mystery-8b", None, get), False, "needs review")
expect("GGUF of an unknown model", c.check_hf("bartowski/mystery-8b-GGUF", None, get), False, "needs review")
expect("unreadable repo", c.check_hf("someuser/not-there", None, get), False, "unverified")
expect("missing base model, approved owner (Gemma 3 12B)", c.check_hf("google/gemma-3-12b-it", None, get), True)
expect("missing base model, unknown owner", c.check_hf("someuser/ghost-ft", None, get), False, "needs review")
expect("missing base model, listed owner", c.check_hf("someuser/qwen-ghost-ft", None, get), False, "blocked")
expect("the model itself missing", c.check_hf("google/gemma-3-12b", None, get), False, "unverified")
expect("chain deeper than the limit", c.check_hf("u/l0", None, get), False, "unverified")
expect("listed base model in tags passed by the scout", c.check_hf("x/y", ["base_model:quantized:Qwen/Qwen3-8B"], get), False, "blocked")

# What ollama pull would fetch
expect("approved Ollama family", c.check_tag("llama3.1:8b", get), True)
for t in ["r1-1776:70b", "deepscaler:14b", "openthinker:7b", "sailor2:8b", "reader-lm", "marco-o1", "cogito:14b", "yi:34b"]:
    expect("Ollama tag " + t, c.check_tag(t, get), False, "needs review")
expect("hf.co tag, approved lineage", c.check_tag("hf.co/unsloth/gemma-3-4b-it-GGUF:Q4_K_M", get), True)
expect("hf.co tag, Qwen lineage", c.check_tag("hf.co/someuser/helper-8b-GGUF:Q4_K_M", get), False, "blocked")

# models.json entries
expect("tag-only entry whose GGUF repo is a Qwen fine-tune",
       c.check_model({"name": "Helper-8B", "ollama_tag": "hf.co/someuser/helper-8b-GGUF:Q4_K_M"}, get), False, "blocked")
expect("clean entry", c.check_model({"name": "Gemma 3 4B", "hf": "google/gemma-3-4b-it", "ollama_tag": "gemma3:4b"}, get), True)
expect("entry with nothing to check", c.check_model({"name": "Thing"}, get), False, "needs review")

# Offline check on the server
d = pathlib.Path(tempfile.mkdtemp())
(d / "config.json").write_text(json.dumps({"model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"]}))
expect("offline: config says Qwen", c.check_offline("someuser/helper-8b", d), False, "blocked")
(d / "config.json").write_text(json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]}))
expect("offline: config says Llama", c.check_offline("meta-llama/Llama-3.1-8B-Instruct", d), True)
expect("offline: listed owner", c.check_offline("Qwen/Qwen3-8B", d), False, "blocked")

# The lists themselves
for org in c.APPROVED_ORGS:
    if org in c._BLOCKED_ORGS: fails.append(f"{org} is both approved and prohibited")
for repo in c.REVIEWED:
    if not c.check_owner(repo)[0] or not c.check_name(repo)[0]: fails.append(f"REVIEWED holds a listed model: {repo}")
for fam in c.APPROVED_OLLAMA:
    if not c.check_name(fam)[0]: fails.append(f"APPROVED_OLLAMA holds a listed family: {fam}")

if fails:
    print("\n".join(fails)); sys.exit(f"{len(fails)} compliance test(s) FAILED")
print(f"OK: all compliance tests passed (lists version {c.VERSION})")
