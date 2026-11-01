# Model Watch

Compares recent open language models against a baseline on clinical genetics tasks
(patient chatbot, variant report generation, parent-interview annotation, genomic interpretation).
All test data is invented. No real patient or institutional data is used anywhere in this repo.

## How a weekly run works
1. **Monday morning (or press Run workflow):** `.github/workflows/pipeline.yml` starts.
2. **Scout:** `scout/scout.py` lists trending, recently updated text models that pass fixed rules and writes `docs/candidates.json`. Models in `scout/skip.json` are ignored.
3. **Choose:** `pipeline/select_queue.py` builds the test list: the baseline, any hand-added model that has an `ollama_tag` and no results yet, and up to 10 scout models that have a GGUF tag and fit the free GPUs (32B or smaller). Models from earlier weeks stay in `models.json` and on the page, each with the date it was added; hand-picked models stay too. A model that failed is marked `failed` and not retried until you delete that flag.
4. **Kaggle:** the workflow pushes `kaggle/notebook.py` (with the list filled in) to Kaggle, which installs Ollama, downloads each model and runs `harness/run_tests_v2.py`.
5. **Checks:** `pipeline/collect.py` reads the results. A model passes only if all 18 calls finished, no answer was cut off, JSON never failed to parse, and the baseline score stayed within 0.10 of its last run. Failed models keep their old scores and get a note.
6. **Merge:** results go to a branch and a pull request. If everything passed (and auto-merge is on) the PR merges itself; otherwise it stays open for you to review.
7. **Publish and notify:** `deploy.yml` republishes the site, and the workflow opens a GitHub issue that mentions you, which sends you an email or phone notification.

## One-time setup
1. **Pages:** Settings > Pages > Source: **GitHub Actions** (not "Deploy from a branch").
2. **Actions permissions:** Settings > Actions > General > Workflow permissions: **Read and write**, and tick **Allow GitHub Actions to create and approve pull requests**.
3. **Secrets:** Settings > Secrets and variables > Actions > New repository secret: `KAGGLE_USERNAME` (your Kaggle handle) and `KAGGLE_API_TOKEN` (the token that starts with KGAT_, from kaggle.com > Settings > API). Older accounts can use `KAGGLE_KEY` instead of the token.
4. **Kaggle account:** phone-verified, so GPU and internet are allowed in notebooks.
5. Delete the old `.github/workflows/scout.yml` if it is still there. The pipeline replaces it.
6. Run it: Actions > Model Watch pipeline > Run workflow.

## Folders
| Folder | What it does |
|---|---|
| `docs/` | The website. `models.json` holds results, `candidates.json` the scout list, `run_report.json` the last run's checks. |
| `harness/` | `run_tests_v2.py` is the test. `MODELS` must be set. `TOKEN_SCALE` raises answer limits for thinking models. |
| `scout/` | Weekly scout and `skip.json`. |
| `pipeline/` | Queue selection and result checks. |
| `kaggle/` | Notebook template and Kaggle metadata. |
| `runs/` | Raw output of every run. |

## Add a model by hand
Add a block to `docs/models.json` with an `ollama_tag` (an ollama.com tag or `hf.co/<org>/<repo>:Q4_K_M`). The next run tests it.

## Known gaps
- The scout, Kaggle push and merge steps have never run against the live services. Expect fixes on the first run.
- The test is small and easy (16 gold labels). Scores near the top cannot separate good models.
- Two automatic checks (`no_harmless`, `not_in_report`) can flag good answers.
- Kaggle may give one 16 GB GPU instead of two, so 27B to 32B models may fail to load. The checks report this.
- Speeds come from free Kaggle GPUs and do not predict A100 speed.
- No genomic interpretation test exists yet.
- The scout lists leads only. Nobody has read those model cards.
