"""Evaluation against a gold annotation, from saved runs only: it costs no model calls.

    uv run reqaudit-eval corpus/gold/<doc>.yaml runs/<id> [runs/<id> ...]

A finding matches a gold defect by place, not by wording: its quotes must cover the gold
quotes (both sides of a conflict, one quote otherwise). Matching is one to one. With
several runs of the same document the report also shows stability: which gold defects
every run finds, which ones come and go, and how their severity varies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from reqaudit.evidence import match_key

_WORD = re.compile(r"[\wё]+", re.IGNORECASE)


@dataclass
class GoldDefect:
    id: str
    scope: str
    criterion: str
    severity: str
    quotes: list[str]
    note: str = ""


@dataclass
class RunScore:
    run: str
    findings: int
    matched: dict[str, str]  # gold id -> finding id
    criterion_agrees: int
    severity_agrees: int
    severities: dict[str, str]  # gold id -> severity the run gave
    unmatched_findings: list[tuple[str, str, str]]  # id, severity, title
    gold: list[GoldDefect] = field(repr=False, default_factory=list)

    @property
    def recall(self) -> float:
        return len(self.matched) / len(self.gold) if self.gold else 0.0

    @property
    def precision(self) -> float:
        return len(self.matched) / self.findings if self.findings else 0.0

    def recall_of(self, severity: str) -> tuple[int, int]:
        pool = [g for g in self.gold if g.severity == severity]
        return sum(1 for g in pool if g.id in self.matched), len(pool)


def load_gold(path: Path) -> list[GoldDefect]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [GoldDefect(id=str(d["id"]), scope=str(d.get("scope", "")),
                       criterion=str(d.get("criterion", "")),
                       severity=str(d.get("severity", "major")),
                       quotes=[str(q) for q in d.get("quotes", [])], note=str(d.get("note", "")))
            for d in data.get("defects", [])]


def _covers(gold_quote: str, finding_quotes: list[str]) -> bool:
    """A finding quote covers a gold quote if one contains the other, or most words overlap."""
    g = match_key(gold_quote)
    g_words = set(_WORD.findall(g))
    for q in finding_quotes:
        k = match_key(q)
        if g and k and (g in k or k in g):
            return True
        words = set(_WORD.findall(k))
        if g_words and len(g_words & words) / len(g_words) >= 0.6:
            return True
    return False


def _tightness(gold_quote: str, finding_quotes: list[str]) -> float:
    """Best word Jaccard between the gold quote and a finding quote: 1.0 is the same words."""
    g = set(_WORD.findall(match_key(gold_quote)))
    best = 0.0
    for q in finding_quotes:
        words = set(_WORD.findall(match_key(q)))
        if g | words:
            best = max(best, len(g & words) / len(g | words))
    return best


def _blocks_of(quote: str, blocks: list[dict]) -> set[str]:
    """Blocks of the run's document that hold the gold quote (exactly, or by most words)."""
    key = match_key(quote)
    exact = {b["id"] for b in blocks if key and key in match_key(b["text"])}
    if exact:
        return exact
    words = set(_WORD.findall(key))
    return {b["id"] for b in blocks
            if words and len(words & set(_WORD.findall(match_key(b["text"])))) / len(words)
            >= 0.8}


def _score(gold: GoldDefect, finding: dict, places: dict[str, set[str]] | None = None) -> float:
    """0 when the finding does not cover the gold defect's place; otherwise higher is closer.

    A gold quote is covered when the finding cites the block it stands in, or quotes the
    same words. Among findings that cover the place, the same criterion and quotes closer
    to the gold ones win, so a broad finding does not take a precise one's match.
    """
    places = places or {}
    quotes = [e["quote"] for e in finding.get("evidence", [])]
    cited = {e.get("block_id") for e in finding.get("evidence", [])}
    covered = sum(1 for g in gold.quotes
                  if (places.get(g) and places[g] & cited) or _covers(g, quotes))
    if covered < min(2, len(gold.quotes)):
        return 0.0
    tight = sum(_tightness(g, quotes) for g in gold.quotes) / len(gold.quotes)
    same_criterion = 1.0 if finding.get("criterion") == gold.criterion else 0.0
    return covered * 10 + same_criterion * 3 + tight * 2


def _in_report(finding: dict) -> bool:
    """Finding.in_report on saved JSON: confirmed and not resolved by the author's answer."""
    return (finding.get("status", "confirmed") == "confirmed"
            and finding.get("answer_decision") != "resolved")


def score_run(gold: list[GoldDefect], run_dir: Path) -> RunScore:
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    findings = [f for f in state.get("findings", []) if _in_report(f)]
    blocks = (state.get("document") or {}).get("blocks", [])
    places = {q: _blocks_of(q, blocks) for g in gold for q in g.quotes}
    pairs = sorted(((s, g.id, f["id"]) for g in gold for f in findings
                    if (s := _score(g, f, places)) > 0), reverse=True)
    matched: dict[str, str] = {}
    used: set[str] = set()
    for _, gid, fid in pairs:
        if gid not in matched and fid not in used:
            matched[gid] = fid
            used.add(fid)
    by_id = {f["id"]: f for f in findings}
    by_gold = {g.id: g for g in gold}
    return RunScore(
        run=run_dir.name, findings=len(findings), matched=matched, gold=gold,
        criterion_agrees=sum(1 for g, f in matched.items()
                             if by_id[f]["criterion"] == by_gold[g].criterion),
        severity_agrees=sum(1 for g, f in matched.items()
                            if by_id[f]["severity"] == by_gold[g].severity),
        severities={g: by_id[f]["severity"] for g, f in matched.items()},
        unmatched_findings=[(f["id"], f["severity"], f["title"]) for f in findings
                            if f["id"] not in used])


def report(gold: list[GoldDefect], scores: list[RunScore]) -> str:
    out = [f"# Оценка по эталону: {len(gold)} дефектов, прогонов: {len(scores)}", ""]
    out += ["| Прогон | Находок | Полнота | Критичные | Точность | Критерий совпал | "
            "Серьёзность совпала |", "|---|---|---|---|---|---|---|"]
    for s in scores:
        hit, total = s.recall_of("critical")
        out.append(f"| {s.run} | {s.findings} | {s.recall:.0%} ({len(s.matched)}/{len(gold)}) "
                   f"| {hit}/{total} | {s.precision:.0%} | {s.criterion_agrees}/{len(s.matched)} "
                   f"| {s.severity_agrees}/{len(s.matched)} |")
    out += ["", "## По дефектам эталона", "",
            "| Эталон | Серьёзность | Найден в прогонах | Находки агента | Серьёзность у агента "
            "| Что это |", "|---|---|---|---|---|---|"]
    for g in gold:
        found = sum(1 for s in scores if g.id in s.matched)
        given = Counter(s.severities[g.id] for s in scores if g.id in s.severities)
        shown = ", ".join(f"{k} ×{v}" for k, v in given.most_common()) or "—"
        ids = ", ".join(s.matched.get(g.id, "—") for s in scores)
        out.append(f"| {g.id} | {g.severity} | {found}/{len(scores)} | {ids} | {shown} | "
                   f"{g.note[:90]} |")
    if len(scores) > 1:
        stable = sum(1 for g in gold if all(g.id in s.matched for s in scores))
        sometimes = sum(1 for g in gold if 0 < sum(g.id in s.matched for s in scores)
                        < len(scores))
        jumps = sum(1 for g in gold
                    if len({s.severities[g.id] for s in scores if g.id in s.severities}) > 1)
        out += ["", f"Стабильность: найдены во всех прогонах — {stable}, только в части — "
                    f"{sometimes}; серьёзность менялась между прогонами у {jumps}."]
    for s in scores:
        if s.unmatched_findings:
            out += ["", f"## Находки вне эталона, {s.run}", "",
                    "Это либо ложные срабатывания, либо дефекты, которых нет в эталоне: "
                    "их стоит просмотреть и при согласии добавить в эталон.", ""]
            out += [f"- {fid} ({sev}): {title}" for fid, sev, title in s.unmatched_findings]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="reqaudit-eval",
        description="Оценка сохранённых прогонов по эталонной разметке; модель не вызывается.")
    parser.add_argument("gold", type=Path, help="эталон: corpus/gold/<документ>.yaml")
    parser.add_argument("runs", type=Path, nargs="+", help="папки прогонов runs/<id>")
    parser.add_argument("--json", type=Path, help="сохранить метрики в JSON")
    args = parser.parse_args(argv)

    gold = load_gold(args.gold)
    scores = [score_run(gold, r) for r in args.runs]
    print(report(gold, scores))
    if args.json:
        args.json.write_text(json.dumps([{
            "run": s.run, "findings": s.findings, "recall": round(s.recall, 4),
            "precision": round(s.precision, 4), "matched": s.matched,
            "critical_recall": s.recall_of("critical"),
            "criterion_agrees": s.criterion_agrees, "severity_agrees": s.severity_agrees,
        } for s in scores], ensure_ascii=False, indent=2), encoding="utf-8")
    return 0
