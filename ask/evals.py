"""The benchmark: golden questions generated from the graph, run through Claude Code
headless with and without the context layer, graded without a judge.

    python -m ask evals generate [--per-class 3]     -> ask/evals/questions.yaml
    python -m ask evals run [--dry-run] [--condition baseline|treatment|both] [--only id]
    python -m ask evals report --results ask/evals/results/<file>.json

Nothing here names a workspace, a model or a term: every question is built from what the
context database contains, so the same code benchmarks any tenant. Expected numbers are
computed by running the golden DAX before and after the runs; an answer within tolerance
of either passes.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import db as ctx        # the benchmark builds its questions out of the graph
from . import fabric

HERE = os.path.dirname(os.path.abspath(__file__))
EVALS_DIR = os.path.join(HERE, "evals")
QUESTIONS = os.path.join(EVALS_DIR, "questions.yaml")
RESULTS_DIR = os.path.join(EVALS_DIR, "results")

# Generic business phrases; whichever the context has no hit for becomes the
# out-of-scope question, which the agent must refuse rather than guess.
OUT_OF_SCOPE = ["employee headcount by department", "customer churn rate last quarter",
                "inventory turnover by warehouse", "marketing campaign click-through rate",
                "supplier on-time delivery percentage", "patient readmission rate",
                "student enrolment by faculty", "website session duration",
                "fleet fuel consumption per vehicle", "loan default rate by branch"]

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "the answer in one or two sentences"},
        "value": {"type": ["number", "null"], "description": "the number, when the answer is one"},
        "unit": {"type": ["string", "null"]},
        "period": {"type": ["string", "null"]},
        "citations": {"type": "array", "items": {"type": "object", "properties": {
            "measure": {"type": "string"}, "model": {"type": "string"},
            "model_id": {"type": ["string", "null"]}, "rank": {"type": ["integer", "null"]},
            "conflicting": {"type": ["boolean", "null"]}}}},
        "query": {"type": ["string", "null"], "description": "the DAX or SQL that ran"},
        "out_of_scope": {"type": "boolean", "description": "true when the context cannot answer"},
    },
    "required": ["answer", "out_of_scope"],
}

BASELINE_PROMPT = (
    "You answer questions about a Microsoft Fabric tenant. Semantic model definitions (TMSL) "
    "are on disk under {raw}/<workspace>/definitions/SemanticModel/<model-id>/model.bim, with "
    "item ids in {raw}/<workspace>/items.json. To run a DAX query: python -m ask dax <model-id> "
    "\"<EVALUATE ...>\" --json. Nothing else is available. Answer in the requested JSON; set "
    "out_of_scope true when you cannot find the answer.")


def baseline_prompt() -> str:
    """The control condition: the raw definitions and a DAX runner, no context layer. The
    harvested JSON is not in the repo - context.json says where the working copy is."""
    where = ctx.location() or {}
    raw = os.path.join(where.get("work") or "", "raw")
    return BASELINE_PROMPT.format(raw=raw or "raw")

TREATMENT_HINT = ("Answer in the requested JSON. Set out_of_scope true when the harvested "
                  "context has nothing for the question.")


# ---------------------------------------------------------------- generate

def _q(qid: str, klass: str, question: str, **rest) -> Dict[str, Any]:
    return dict({"id": qid, "class": klass, "question": question}, **rest)


def generate(con, per_class: int = 3, live_values: bool = True) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ws_ids = {n: i for n, i in con.execute(
        "SELECT name, item_id FROM nodes WHERE kind = 'workspace'").fetchall()}

    terms = con.execute("""
        SELECT t.term_id, t.label, t.conflicting, t.n_definitions, d.name, d.owner_item_name,
               d.owner_item_id, d.kind, d.def_id, d.table_name, d.workspace
          FROM terms t JOIN definitions d ON d.def_id = t.top_def_id
         ORDER BY t.conflicting DESC, t.n_definitions DESC, d.score DESC, t.term_id""").fetchall()

    # definition: where is X defined, which to trust
    for i, (tid, label, conf, n, name, owner, owner_id, kind, def_id, tbl, ws) in enumerate(terms[:per_class]):
        out.append(_q("def-%02d" % (i + 1), "definition",
                      "Where is '" + label + "' defined and which definition should I trust?",
                      term_id=tid,
                      expect={"contains_all": [name], "contains_any": [owner],
                              "mentions_conflict": bool(conf)}))

    # conflict: do all definitions agree
    conflicting = [t for t in terms if t[2]][:max(1, per_class - 1)]
    agreeing = [t for t in terms if not t[2] and t[3] >= 2][:1]
    for i, t in enumerate(conflicting + agreeing):
        out.append(_q("conf-%02d" % (i + 1), "conflict",
                      "Do all definitions of '" + t[1] + "' agree with each other?",
                      term_id=t[0],
                      expect={"yes_no": "no" if t[2] else "yes", "mentions_conflict": bool(t[2])}))

    # lineage: what feeds the top measure
    made = 0
    for t in terms:
        if made >= per_class:
            break
        rows = ctx.lineage(con, t[8], "up", 8)["nodes"]
        tables = sorted({r["name"] for r in rows if r["kind"] == "lakehouse_table"})
        if not tables:
            continue
        made += 1
        out.append(_q("lin-%02d" % made, "lineage",
                      "What feeds the measure '" + t[4] + "' in the model '" + t[5]
                      + "'? Name the physical tables.",
                      def_id=t[8], expect={"contains_any": tables}))

    # usage: which reports use it
    made = 0
    for t in terms:
        if made >= per_class:
            break
        reports = [r["name"] for r in ctx.usage(con, "term:" + t[0])["reports"]]
        if not reports:
            continue
        made += 1
        out.append(_q("use-%02d" % made, "usage", "Which reports use '" + t[1] + "'?",
                      term_id=t[0], expect={"contains_any": reports}))

    # data: a rank-1 measure filtered by one value of a low-cardinality string column
    made = 0
    seen_models = set()
    for t in terms:
        if made >= per_class or t[7] != "measure" or t[6] in seen_models:
            continue
        pick = _pick_filter(con, t[6], live_values)
        if not pick:
            continue
        seen_models.add(t[6])
        made += 1
        table, column, value = pick
        dax = ('EVALUATE ROW("v", CALCULATE([' + t[4] + "], " + fabric._dax_name(table, column)
               + ' = "' + str(value).replace('"', '""') + '"))')
        out.append(_q("data-%02d" % made, "data",
                      "What is the " + t[4] + " where " + column + " is '" + str(value) + "'?",
                      answer_kind="number", model_id=t[6], model=t[5],
                      workspace_id=ws_ids.get(t[10]), golden_dax=dax, tolerance_pct=1.0,
                      expect={"contains_any": [t[4]]}))

    # out of scope: a phrase the context has nothing for
    for phrase in OUT_OF_SCOPE:
        hits = ctx.search(con, phrase, limit=3)["hits"]
        if not hits or hits[0]["score"] < 0.7:
            out.append(_q("oos-01", "out_of_scope", "What is the " + phrase + "?",
                          expect={"refuses": True}))
            break
    return out


def _pick_filter(con, model_item_id: str, live_values: bool):
    """(table, column, value) for a string column of the model with 2..50 distinct values,
    from the harvested profile first, else one live DAX probe per candidate column."""
    m = con.execute("SELECT id FROM nodes WHERE kind = 'semantic_model' AND item_id = ?",
                    [model_item_id]).fetchone()
    if not m:
        return None
    rows = con.execute("""
        SELECT t.name, c.name, c.attrs
          FROM nodes c JOIN nodes t ON t.id = c.parent_id
         WHERE c.kind = 'column' AND t.parent_id = ?
           AND lower(coalesce(json_extract_string(c.attrs, '$.data_type'), '')) IN ('string', 'text')
           AND NOT coalesce(TRY_CAST(json_extract_string(c.attrs, '$.is_hidden') AS BOOLEAN), false)
         ORDER BY CASE WHEN json_extract_string(c.attrs, '$.profile') IS NOT NULL THEN 0 ELSE 1 END, t.name, c.name""",
                       [m[0]]).fetchall()
    probes = 0
    for tname, cname, attrs in rows:
        prof = ctx._attrs(attrs).get("profile") or {}
        values = prof.get("values")
        n_distinct = prof.get("n_distinct")
        if not values and live_values and probes < 4:
            probes += 1
            try:
                ws = con.execute("SELECT w.item_id FROM nodes m JOIN nodes w ON w.kind = 'workspace' "
                                 "AND w.name = m.workspace WHERE m.id = ?", [m[0]]).fetchone()[0]
                live = fabric.values(ws, model_item_id, tname, cname, limit=50)
                values, n_distinct = live["values"], live.get("n_distinct")
                if live.get("truncated"):
                    values = None                      # more than the cap: not a filter column
            except Exception:                          # noqa: BLE001 - try the next column
                values = None
        if n_distinct is not None and n_distinct > 50:
            continue
        if values and 2 <= len(values) <= 50:
            values = [v for v in values if v not in (None, "")]
            if values:
                return tname, cname, values[len(values) // 2]
    return None


def write_questions(questions: List[Dict], path: str = QUESTIONS) -> str:
    import yaml
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"version": 1, "generated_at": _now(), "questions": questions}, fh,
                       sort_keys=False, allow_unicode=True, width=100)
    return path


def load_questions(path: str = QUESTIONS) -> List[Dict]:
    import yaml
    if not os.path.exists(path):
        raise ctx.NotFound("no questions at " + path + " - run: python -m ask evals generate")
    with open(path, "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("questions") or []


# ---------------------------------------------------------------- run

def _now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def expected_value(q: Dict) -> Optional[float]:
    """Run the golden DAX and return its single number."""
    res = fabric.dax(q["workspace_id"], q["model_id"], q["golden_dax"], max_rows=1)
    if not res["rows"]:
        return None
    val = next(iter(res["rows"][0].values()))
    return float(val) if val is not None else None


def _claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise SystemExit("claude (Claude Code) is not on PATH")
    return exe


def run_one(q: Dict, condition: str, model: str, budget: float) -> Dict[str, Any]:
    prompt = q["question"] + "\n\n" + (TREATMENT_HINT if condition == "treatment" else "")
    cmd = [_claude(), "-p", prompt, "--output-format", "json",
           "--json-schema", json.dumps(ANSWER_SCHEMA), "--no-session-persistence",
           "--max-budget-usd", str(budget), "--model", model,
           "--disallowedTools", "Edit", "Write", "Bash(python src/run.py *)"]
    if condition == "baseline":
        cmd += ["--bare", "--append-system-prompt", baseline_prompt(),
                "--allowedTools", "Bash(python -m ask dax *)", "Read", "Grep", "Glob"]
    else:
        cmd += ["--allowedTools", "Bash(python -m ask *)", "Read"]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ctx.ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "elapsed_s": 900}
    elapsed = round(time.time() - started, 1)
    out: Dict[str, Any] = {"elapsed_s": elapsed, "exit": proc.returncode}
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        out.update(error="no json from claude", stdout=proc.stdout[-800:], stderr=proc.stderr[-800:])
        return out
    structured = payload.get("structured_output")
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except ValueError:
            structured = None
    out.update(text=payload.get("result"), structured=structured,
               cost_usd=payload.get("total_cost_usd"), turns=payload.get("num_turns"),
               session_id=payload.get("session_id"))
    if proc.returncode and not structured:
        out["error"] = "claude exited " + str(proc.returncode) + ": " + proc.stderr[-400:]
    return out


_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def grade(q: Dict, run: Dict, expected: List[Optional[float]]) -> Dict[str, Any]:
    if run.get("error"):
        return {"pass": False, "reason": run["error"]}
    structured = run.get("structured") or {}
    text = " ".join(str(x) for x in (run.get("text"), structured.get("answer"),
                                      json.dumps(structured.get("citations") or []))).lower()
    exp = q.get("expect") or {}
    reasons = []

    if q["class"] == "out_of_scope":
        ok = bool(structured.get("out_of_scope")) and structured.get("value") is None
        return {"pass": ok, "reason": "refused" if ok else "answered an out-of-scope question"}
    if structured.get("out_of_scope"):
        return {"pass": False, "reason": "refused a question the context can answer"}

    if q.get("answer_kind") == "number":
        got = structured.get("value")
        if got is None:
            m = _NUMBER.search(str(structured.get("answer") or run.get("text") or ""))
            got = float(m.group(0).replace(",", "")) if m else None
        targets = [e for e in expected if e is not None]
        if got is None or not targets:
            return {"pass": False, "reason": "no number" if got is None else "no expected value",
                    "got": got, "expected": expected}
        tol = float(q.get("tolerance_pct", 1.0)) / 100.0
        ok = any(abs(float(got) - e) <= tol * max(abs(e), 1e-9) for e in targets)
        reasons.append("value " + str(got) + " vs " + str(targets))
        if not ok:
            return {"pass": False, "reason": "; ".join(reasons), "got": got, "expected": targets}

    for want in exp.get("contains_all") or []:
        if want.lower() not in text:
            return {"pass": False, "reason": "missing '" + want + "'"}
    if exp.get("contains_any") and not any(w.lower() in text for w in exp["contains_any"]):
        return {"pass": False, "reason": "none of " + ", ".join(exp["contains_any"])}
    if exp.get("mentions_conflict") and not re.search(r"conflict|disagree|differ|competing|not agree", text):
        return {"pass": False, "reason": "conflict not mentioned"}
    if exp.get("yes_no"):
        answer = str(structured.get("answer") or run.get("text") or "").lower()
        head = answer[:160]
        says_no = bool(re.search(r"\b(no|do not|don't|does not|disagree|differ)\b", head))
        says_yes = bool(re.search(r"\b(yes|all .*agree|agree)\b", head)) and not says_no
        want_no = exp["yes_no"] == "no"
        if want_no != says_no or (not want_no and not says_yes):
            return {"pass": False, "reason": "expected " + exp["yes_no"] + ": " + head[:80]}
    return {"pass": True, "reason": "; ".join(reasons) or "matched"}


def run_all(con, args) -> Dict[str, Any]:
    questions = load_questions(args.questions or QUESTIONS)
    if args.only:
        questions = [q for q in questions if q["id"] in set(args.only)]
    conditions = ["baseline", "treatment"] if args.condition == "both" else [args.condition]

    print("expected values (golden DAX, snapshot 1)")
    expected: Dict[str, List[Optional[float]]] = {}
    keep = []
    for q in questions:
        if q.get("answer_kind") == "number":
            try:
                expected[q["id"]] = [expected_value(q)]
                print("  " + q["id"] + ": " + str(expected[q["id"]][0]))
            except Exception as exc:                   # noqa: BLE001 - drop the question
                print("  " + q["id"] + ": golden DAX failed, skipped (" + str(exc)[:120] + ")")
                continue
        keep.append(q)
    questions = keep
    if args.dry_run:
        print(str(len(questions)) + " questions ready; --dry-run, nothing run")
        return {"questions": questions, "expected": expected}

    results = []
    for q in questions:
        for cond in conditions:
            print("[" + cond + "] " + q["id"] + "  " + q["question"][:70], flush=True)
            run = run_one(q, cond, args.model, args.budget)
            verdict = grade(q, run, expected.get(q["id"], []))
            print("   " + ("PASS" if verdict["pass"] else "FAIL") + "  " + verdict["reason"][:100]
                  + "  $" + str(run.get("cost_usd")) + "  " + str(run.get("elapsed_s")) + "s")
            results.append({"id": q["id"], "class": q["class"], "condition": cond,
                            "question": q["question"], "pass": verdict["pass"],
                            "reason": verdict["reason"], "cost_usd": run.get("cost_usd"),
                            "elapsed_s": run.get("elapsed_s"), "turns": run.get("turns"),
                            "answer": (run.get("structured") or {}).get("answer") or run.get("text"),
                            "query": (run.get("structured") or {}).get("query"),
                            "error": run.get("error")})

    print("expected values (snapshot 2)")
    for q in questions:
        if q["id"] in expected:
            try:
                expected[q["id"]].append(expected_value(q))
            except Exception:                          # noqa: BLE001
                pass
    for r in results:
        if r["class"] == "data" and not r["pass"]:
            q = next(x for x in questions if x["id"] == r["id"])
            again = grade(q, {"structured": {"answer": r["answer"], "value": None},
                              "text": r["answer"]}, expected[q["id"]])
            if again["pass"]:
                r["pass"], r["reason"] = True, again["reason"] + " (snapshot 2)"

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    payload = {"ran_at": _now(), "model": args.model, "conditions": conditions,
               "expected": expected, "results": results}
    path = os.path.join(RESULTS_DIR, stamp + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False, default=str)
    table = report_table(payload)
    with open(os.path.join(RESULTS_DIR, stamp + ".md"), "w", encoding="utf-8") as fh:
        fh.write(table)
    print("\n" + table)
    print("wrote " + os.path.relpath(path, ctx.ROOT))
    return payload


def report_table(payload: Dict[str, Any]) -> str:
    results = payload["results"]
    conditions = payload["conditions"]
    classes = []
    for r in results:
        if r["class"] not in classes:
            classes.append(r["class"])
    head = "| class | n | " + " | ".join(conditions) + (" | delta" if len(conditions) == 2 else "") + " |"
    sep = "|---|---|" + "---|" * len(conditions) + ("---|" if len(conditions) == 2 else "")
    lines = ["Accuracy by class (" + str(payload.get("model")) + ", " + str(payload.get("ran_at")) + ")",
             "", head, sep]

    def acc(rows):
        return (sum(1 for r in rows if r["pass"]) / len(rows)) if rows else None

    def fmt(x):
        return "-" if x is None else str(int(round(x * 100))) + "%"

    for klass in classes + ["all"]:
        rows = [r for r in results if klass == "all" or r["class"] == klass]
        per = [acc([r for r in rows if r["condition"] == c]) for c in conditions]
        n = len({r["id"] for r in rows})
        delta = ""
        if len(conditions) == 2 and None not in per:
            delta = " | " + ("+" if per[1] >= per[0] else "") + str(int(round((per[1] - per[0]) * 100))) + " pt"
        lines.append("| " + klass + " | " + str(n) + " | " + " | ".join(fmt(p) for p in per) + delta + " |")
    cost = {c: sum(float(r.get("cost_usd") or 0) for r in results if r["condition"] == c) for c in conditions}
    lines += ["", "Cost: " + ", ".join(c + " $" + format(v, ".2f") for c, v in cost.items()), ""]
    for r in results:
        if not r["pass"]:
            lines.append("- " + r["id"] + " [" + r["condition"] + "]: " + str(r["reason"])[:140])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- entry

def main(args, con) -> None:
    if args.action == "generate":
        questions = generate(con, per_class=args.per_class)
        path = write_questions(questions, args.questions or QUESTIONS)
        by_class: Dict[str, int] = {}
        for q in questions:
            by_class[q["class"]] = by_class.get(q["class"], 0) + 1
        print("wrote " + str(len(questions)) + " questions to " + os.path.relpath(path, ctx.ROOT)
              + ": " + ", ".join(k + "=" + str(v) for k, v in by_class.items()))
        for q in questions:
            print("  " + q["id"].ljust(8) + q["question"])
        print("Correct anything wrong in the file, then: python -m ask evals run --dry-run")
    elif args.action == "run":
        run_all(con, args)
    else:
        path = args.results
        if not path:
            files = sorted(os.listdir(RESULTS_DIR)) if os.path.isdir(RESULTS_DIR) else []
            files = [f for f in files if f.endswith(".json")]
            if not files:
                raise ctx.NotFound("no results under " + RESULTS_DIR)
            path = os.path.join(RESULTS_DIR, files[-1])
        with open(path, "r", encoding="utf-8") as fh:
            print(report_table(json.load(fh)))
