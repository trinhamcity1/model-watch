# Model Watch

Finds new open language models and benchmarks them on phenotype extraction, then publishes the results on a GitHub Pages site.

## How it works

Three workflows:

1. **Model Watch scout** (`.github/workflows/pipeline.yml`) runs every Monday, or by hand.
   It reads the trending text-generation models on Hugging Face (`scout/scout.py`), looks for a 4-bit GGUF download,
   and adds the models it finds to `docs/models.json` (`scout/update_models.py`). Models over 40B are listed as
   "server only". It then publishes the site and starts the benchmark.
2. **Model Watch HPO benchmark** (`.github/workflows/benchmark.yml`) runs after the scout, or by hand.
   It queues every model in `docs/models.json` that has no result yet (`bench/select.py`), starts a free Kaggle GPU
   notebook (`bench/kaggle_hpo.py`) and exits. It will not start while a Kaggle run is going or uncollected.
3. **Model Watch collect** (`.github/workflows/collect.yml`) checks every 30 minutes. When the Kaggle run has
   finished, it scores the output (`bench/score.py`, `bench/collect.py`), opens and merges a pull request, publishes the
   site, and starts a follow-up run for any models the run did not reach. A Kaggle run can last up to 11 hours
   (`hard_limit_minutes` in `bench/config.json`).

## The benchmark

Each model is the language model inside [RAG-HPO](https://github.com/PoseyPod/RAG-HPO). RAG-HPO pulls phenotype
phrases from each document and matches them to Human Phenotype Ontology (HPO) terms. The HPO codes it returns are
compared, per document, with expert annotations from two public datasets in the
[PhenoBERT repository](https://github.com/EclipseCN/PhenoBERT) (MIT licence):

- **ID-68**: 68 clinical notes from families with intellectual disability (Anazi et al. 2017). Includes negated findings.
- **GSC+**: PubMed abstracts with HPO annotations (Lobo et al. 2017). A fixed sample of 60 of the 228.

Measures: precision, recall, F1 (summed over documents and averaged per document), negation errors, seconds per
document, and peak GPU memory. A small control model is re-run every time; if its F1 moves by more than 0.10 the
run is not merged.

Settings live in `bench/config.json`. Change `task_version` there whenever the task changes (datasets, sample
sizes, RAG-HPO version); every model is then re-run.

## Running it by hand

Actions -> Model Watch HPO benchmark -> Run workflow. To pick up a finished Kaggle run right away instead of waiting
for the 30-minute check, run Model Watch collect by hand (tick `force` if the run was started by an older version). Options: specific model ids, one dataset, fewer documents for a
quick trial, or force a re-run.

To add a model by hand, add an entry to `docs/models.json` with `id`, `name`, `size`, `licence`, `ollama_tag` (an
Ollama tag or `hf.co/<org>/<repo>:Q4_K_M`) and `hf`. The next benchmark run tests it. If a model failed and you want
to retry it, delete its entry under `results`.

## One-time setup

- Settings -> Pages -> Source: GitHub Actions
- Settings -> Actions -> General -> Workflow permissions: Read and write, and tick "Allow GitHub Actions to create and approve pull requests"
- Secrets: `KAGGLE_USERNAME` and `KAGGLE_API_TOKEN` (Kaggle account with phone verification, for GPU and internet)

## Folders

- `docs/`: the site and its data (`models.json`, `candidates.json`, `run_report.json`, `control.json`)
- `bench/`: the benchmark (selection, Kaggle script, scoring)
- `scout/`: the weekly scout
- `runs/`: raw Kaggle output for each run
- `harness/`, `kaggle/`, `pipeline/`: the retired 18-question test, kept for the record. Its scores are archived on the site.

## Known gaps

- Speeds and memory come from two T4 GPUs, not the lab server.
- The HPO release is not pinned; the release used is recorded with each result.
- Scores are document-level, so compare them with document-level results in the literature.
- Thinking models may format their output in ways RAG-HPO does not parse; those runs show a reason on the site.
