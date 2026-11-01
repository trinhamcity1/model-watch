# Model Watch

Compares recent open language models against a baseline on tasks from the Lal lab plan
(patient chatbot, variant report generation, parent-interview annotation, genomic interpretation).
All test data is invented. No UTA data is used anywhere in this repo.

## Parts
| Folder | What it does |
|---|---|
| `docs/` | The website (GitHub Pages). `models.json` holds the results; `candidates.json` is the weekly scout output. |
| `harness/` | `run_tests_v2.py` runs the fixed prompts against Ollama models. `ingest.py` copies a run into `models.json`. |
| `scout/` | `scout.py` lists trending, recently updated text models that pass fixed rules. It downloads nothing. |
| `kaggle/` | Cells for running the harness on Kaggle's free GPU. |
| `.github/workflows/` | Runs the scout every Monday and commits `candidates.json`. |

## Set up
1. Create a public repo named `model-watch` and upload this folder.
2. Settings > Pages > Deploy from a branch > `main` and `/docs`.
3. Actions > Weekly model scout > Run workflow, to check the scout works.

## Run a comparison
1. Open a Kaggle notebook with GPU and Internet on. Paste the cells from `kaggle/run_on_kaggle.py`.
2. Download `run.zip`, unzip it into `runs/<date>/`.
3. `python harness/ingest.py runs/<date> <model_id> <tag>` for each model, then commit.

## Status and known gaps
- `scout.py` and the Kaggle cells are untested against the live services (they could not be reached from the build environment). Expect small fixes on first run.
- Most queued models have no `ollama_tag` yet. Add one (or a GGUF repo from Hugging Face) before the Kaggle loop can run them.
- Test set is small (16 gold labels, 10 report and safety questions). One run can swing by a few points.
- Known check problems to fix before trusting scores: the "harmless" check window is too narrow, and the `not_in_report` check is too strict.
- Speeds from free GPUs do not predict A100 speed.
- The scout lists candidates only. A person must still read each model card.
