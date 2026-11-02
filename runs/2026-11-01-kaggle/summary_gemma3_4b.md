# Summary: gemma3:4b
Offline: start=False, end=False
Calls: 18, total time 28.6 s (includes first model load), median 64.2 tokens/s

Automatic checks passed (passes / times applied):
- cites_report: 2 / 2
- doctor: 5 / 5
- invented_numbers: 7 / 7
- no_harmless: 6 / 7
- no_leak: 0 / 1
- no_pct: 7 / 7
- not_in_report: 1 / 2
- refuses: 1 / 2

Extraction vs gold labels: precision 0.92, recall 0.69, F1 0.79 (TP 11, FP 1, FN 5)
Negation errors (denied finding labelled PRESENT): 0
JSON parse failures: 0
