# Paste each block into its own cell of a Kaggle notebook.
# Settings: Accelerator = GPU T4 x2, Internet = On. Upload nothing private; the prompts are invented.

# --- Cell 1: get the repo and Ollama ---
# !git clone https://github.com/<YOUR-USER>/model-watch.git
# !curl -fsSL https://ollama.com/install.sh | sh
# import subprocess, time
# subprocess.Popen(["ollama", "serve"]); time.sleep(8)

# --- Cell 2: smoke test with the small baseline (3 minutes) ---
# !cd model-watch/harness && MODELS=qwen3:4b-instruct-2507-q4_K_M REPEATS=1 SKIP_PING=1 python run_tests_v2.py

# --- Cell 3: run the queued models that have an ollama_tag in docs/models.json ---
# import json, subprocess, os
# models = [m for m in json.load(open("model-watch/docs/models.json")) if m.get("ollama_tag") and not m["tested"]]
# for m in models:
#     subprocess.run(["ollama", "pull", m["ollama_tag"]])
#     env = dict(os.environ, MODELS=m["ollama_tag"], REPEATS="1", SKIP_PING="1")
#     subprocess.run(["python", "run_tests_v2.py"], cwd="model-watch/harness", env=env)

# --- Cell 4: zip the output files and download them from the Output tab ---
# !cd model-watch/harness && zip -r /kaggle/working/run.zip summary_*.md results_*.csv answers_*.md
