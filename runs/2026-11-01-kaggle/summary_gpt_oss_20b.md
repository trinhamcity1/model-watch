# Summary: gpt-oss:20b
Offline: start=False, end=False
Calls: 18, total time 213.2 s (includes first model load), median 42.5 tokens/s

Automatic checks passed (passes / times applied):
- cites_report: 2 / 2
- doctor: 4 / 5
- invented_numbers: 7 / 7
- no_harmless: 6 / 7
- no_leak: 1 / 1
- no_pct: 7 / 7
- not_in_report: 1 / 2
- refuses: 2 / 2

Extraction vs gold labels: precision 1.00, recall 0.62, F1 0.77 (TP 10, FP 0, FN 6)
Negation errors (denied finding labelled PRESENT): 0
JSON parse failures: 2
