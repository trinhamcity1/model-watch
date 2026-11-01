# Kaggle script run by the pipeline. The queue placeholder below is filled in with the model queue before pushing.
# It installs Ollama, tests every model in the queue with the repo's harness, and leaves files in /kaggle/working/out.
import json, os, pathlib, re, shutil, subprocess, time

QUEUE = json.loads(r'''__QUEUE__''')
REPO = "https://github.com/__REPO__.git"
sh = lambda cmd, **kw: subprocess.run(cmd, shell=True, **kw)

sh("apt-get update -qq && apt-get install -y -qq zstd")
sh("curl -fsSL https://ollama.com/install.sh | sh")
subprocess.Popen(["ollama", "serve"])
time.sleep(12)
sh(f"git clone --depth 1 {REPO} /tmp/repo")

out = pathlib.Path("/kaggle/working/out"); out.mkdir(parents=True, exist_ok=True)
gpus = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", capture_output=True, text=True).stdout.strip().splitlines()
meta = {"gpus": gpus, "models": []}
harness = pathlib.Path("/tmp/repo/harness")

for q in QUEUE:
    tag = q["ollama_tag"]
    try:
        pulled = sh(f"ollama pull {tag}", timeout=1500).returncode == 0
    except subprocess.TimeoutExpired:
        pulled = False
    meta["models"].append({"id": q["id"], "tag": tag, "pull_ok": pulled})
    if pulled:
        env = dict(os.environ, MODELS=tag, REPEATS="1", SKIP_PING="1", TOKEN_SCALE="5")
        try:
            sh("python run_tests_v2.py", cwd=str(harness), env=env, timeout=2400)
        except subprocess.TimeoutExpired:
            print("timed out:", tag)
        for f in list(harness.glob("summary_*.md")) + list(harness.glob("results_*.csv")) + list(harness.glob("answers_*.md")):
            shutil.copy(f, out / f.name)
    sh(f"ollama rm {tag}")  # free disk before the next model

json.dump(meta, open(out / "meta.json", "w"), indent=1)
print("done", json.dumps(meta))
