"""Document-level scoring of RAG-HPO output against PhenoBERT-format gold files.
Gold line: start<TAB>end<TAB>phrase<TAB>HP:xxxxxxx[<TAB>Neg]. Lines marked Neg are findings the patient does NOT have.
A predicted code counts as present when its Category is 'Abnormal'."""
import csv, re

HP = re.compile(r"HP:\d{7}")
HP_ANY = re.compile(r"HP[:_](\d{7})")   # GSC+ writes HP_0001156 | name, ID-68 writes HP:0001156

def read_gold(path):
    pos, neg = set(), set()
    for line in open(path, encoding="utf-8", errors="ignore"):
        parts = line.rstrip("\n").split("\t")
        m = HP_ANY.search(line)
        if not m: continue
        code = "HP:" + m.group(1)
        (neg if any(p.strip().lower() == "neg" for p in parts) else pos).add(code)
    return pos, neg - pos

def read_pred(path):
    """Returns {case_number: {'present': set, 'other': set}} and a count of each Category value."""
    out, cats = {}, {}
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            case = row.get("patient id") or row.get("patient_id") or row.get("case")
            code = row.get("hpo id") or row.get("hpo_id") or row.get("hpo_term") or ""
            m = HP.search(code)
            if not case or not m: continue
            cat = row.get("category", "Abnormal") or "Abnormal"
            cats[cat] = cats.get(cat, 0) + 1
            d = out.setdefault(str(int(float(case))), {"present": set(), "other": set()})
            d["present" if cat.lower() == "abnormal" else "other"].add(m.group(0))
    return out, cats

def score(gold_by_case, pred_by_case):
    """gold_by_case: {case: (pos, neg)}. Returns overall and per-document metrics."""
    TP = FP = FN = NEG = 0; f1s = []; per = {}
    for case, (pos, neg) in gold_by_case.items():
        p = pred_by_case.get(case, {"present": set()})["present"]
        tp, fp, fn = len(p & pos), len(p - pos), len(pos - p)
        negerr = len(p & neg)
        TP += tp; FP += fp; FN += fn; NEG += negerr
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
        f1s.append(f1); per[case] = {"tp": tp, "fp": fp, "fn": fn, "neg_errors": negerr}
    P = TP / (TP + FP) if TP + FP else 0.0
    R = TP / (TP + FN) if TP + FN else 0.0
    return {"precision": round(P, 3), "recall": round(R, 3), "f1": round(2 * P * R / (P + R), 3) if P + R else 0.0,
            "f1_doc_mean": round(sum(f1s) / len(f1s), 3) if f1s else 0.0,
            "tp": TP, "fp": FP, "fn": FN, "neg_errors": NEG, "n_docs": len(gold_by_case)}, per
