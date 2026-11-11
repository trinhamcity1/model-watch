# Model Watch

Finds new open language models and benchmarks them on phenotype extraction, then publishes the results on a GitHub Pages site.

## How it works

Three workflows:

1. **Model Watch scout** (`.github/workflows/pipeline.yml`) runs every Monday, or by hand.
   It reads the trending text-generation models on Hugging Face (`scout/scout.py`), looks for a 4-bit GGUF download,
   and adds the models it finds to `docs/models.json` (`scout/update_models.py`). Models over 40B are listed as
   "server only". It then publishes the site and starts the benchmark.
   **Compliance filter** (`bench/compliance.py`, tested by `bench/test_compliance.py`): a model is screened only if
   (a) neither it nor any model it is built on (fine-tunes, distills, quantized copies, followed through Hugging
   Face `base_model` data) comes from a developer on the Texas prohibited technologies list, which UT System
   follows, and (b) the root of that lineage comes from a developer in `APPROVED_ORGS`, or a person has reviewed the
   repo (`REVIEWED`), or it is an approved Ollama library family (`APPROVED_OLLAMA`). Everything else is marked
   `blocked` with a reason starting "blocked" (Texas list), "needs review" (not approved yet) or "unverified"
   (could not be read; checked again next week). The scout lists refused models under `blocked_models` in
   `candidates.json`; `models.json` is rechecked every week; the Kaggle script repeats the name rules before each pull.
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

- `docs/`: the site and its data (`models.json`, `candidates.json`, `run_report.json`, `control.json`, written by the next run)
- `bench/`: the benchmark (selection, compliance filter, Kaggle script, scoring)
- `scripts/`: the offline server benchmark (script 1 builds the bundle, script 2 runs it, script 3 tests enhancements); see `scripts/README.md`
- `scout/`: the weekly scout
- `runs/`: raw Kaggle output for each run
- `harness/`: the retired 18-question test, kept for the record (it applies the compliance filter too). Its scores are archived on the site.

## Known gaps

- Speeds and memory come from two T4 GPUs, not the lab server.
- The HPO release is not pinned; the release used is recorded with each result.
- Scores are document-level, so compare them with document-level results in the literature.
- Thinking models may format their output in ways RAG-HPO does not parse; those runs show a reason on the site.

## Documents, size limits and the saved index

- Every model gets the same 24 documents, listed in `bench/config.json`: 12 hand-picked ID-68 notes and 12 hand-picked GSC+ abstracts, chosen for epilepsy and neurodevelopmental findings (ID-68 picks include the notes with negated findings). The page also shows the mean of the two datasets.
- Models up to 15B run every time. 15B to 40B run only when the benchmark is started with "include 15B+ models" ticked. Over 40B: server only.
- Each model gets 30 minutes per dataset; a model that cannot finish is marked too slow.
- The RAG-HPO search database (about 45 minutes to build) is saved by the collect workflow as the release `hpo-index` and reused until the RAG-HPO version changes or it is older than 90 days.
- Extra hand-picked models live in `bench/extra_models.json`; the benchmark adds any that are missing to `docs/models.json`.
