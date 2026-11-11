# Server scripts (offline lab benchmark)

These run the full benchmark on an offline GPU server. The Kaggle screen in `bench/` and `scout/` is separate.

| Script | Where it runs | What it does |
| --- | --- | --- |
| `build_bundle.py` (script 1) | A machine with internet | Downloads the models, embedding models, code and public datasets into one folder ("the bundle") with checksums. Checks every model, and every model it was built on, against the Texas prohibited technologies list before downloading. |
| `run_benchmark.py` (script 2) | The server, offline | Checks and installs the bundle, fixes the tuning/evaluation split, runs every model on every dataset, runs the repeatability and reproducibility test, and exports scores only. |
| `run_enhancements.py` (script 3) | The server, offline | Measures enhanced setups (new prompt sets, 4-bit models) against their baseline, for accuracy on evaluation documents and for consistency. |

Order: run script 1 with `--dry-run` first, then for real; copy the bundle to the server; copy scripts 2 and 3 next to it; then
`python3 run_benchmark.py check`, `install`, `splits`, `smoke`, `run`, `export`. Each script's own help (`--help`) lists every option.

Compliance: `bench/compliance.py` is the only copy of the rules, shared by the Kaggle screen and these scripts.
Script 1 loads it (from `bench/`, or from a copy placed next to the script on another machine) and stops without it.
It refuses a model before download if its owner, name or any base model belongs to a developer on the Texas
prohibited technologies list, if its developer is not in `APPROVED_ORGS` and the repo is not in `REVIEWED`, or if the
check cannot be completed. It copies `compliance.py` into the bundle and records each model's checked lineage in
MANIFEST.json. Script 2 repeats the check offline (`check`, and before every run): it refuses a bundle with no
compliance record, any model whose owner, name or config files name a listed developer, and any model folder that
is not in MANIFEST.json. Never add model folders to a bundle by hand. When DIR updates its list, edit
`bench/compliance.py`, raise `VERSION`, run `python bench/test_compliance.py`, and rebuild the bundle.

Nothing here contains a token or patient data. The Hugging Face token comes from `huggingface-cli login` or `HF_TOKEN`
on the machine running script 1; never commit it. Raw model replies stay on the server; only scores, timings and settings
are exported.
