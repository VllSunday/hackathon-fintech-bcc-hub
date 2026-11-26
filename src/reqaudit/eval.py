"""Evaluation against a gold annotation, from saved runs only: it costs no model calls.

    uv run reqaudit-eval corpus/gold/<doc>.yaml runs/<id> [runs/<id> ...]

A finding matches a gold defect by place, not by wording: its quotes must cover the gold
quotes (both sides of a conflict, one quote otherwise). Matching is one to one. With
several runs of the same document the report also shows stability: which gold defects
every run finds, which ones come and go, and how their severity varies.

A gold file may also list traps: places that look like a defect but are not. A finding
on a trap that matches no gold defect is a false alarm the report counts separately.

    uv run reqaudit-eval --check corpus/gold/*.yaml       # quotes really in the documents
    uv run reqaudit-eval --import answer.txt              # generator output -> corpus files
"""

from __future__ import annotations

import argparse
import dataclasses
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
    kind: str = ""  # cross_section | local | missing
    in_image: bool = False  # quoted from a picture: only vision can read it


@dataclass
class Trap:
    id: str
    quotes: list[str]
    looks_like: str = ""
    why_not: str = ""


@dataclass
class GoldFile:
    document: str  # path under corpus/inputs/
    defects: list[GoldDefect]
    traps: list[Trap] = field(default_factory=list)
    requirements_total: int | None = None


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
    traps_hit: dict[str, str] = field(default_factory=dict)  # trap id -> finding id
    traps_total: int = 0
    by_meaning: dict[str, str] = field(default_factory=dict)  # gold id -> finding id, 2nd pass

    @property
    def recall_by_meaning(self) -> float:
        found = len(self.matched) + len(self.by_meaning)
        return found / len(self.gold) if self.gold else 0.0

    @property
    def precision_by_meaning(self) -> float:
        found = len(self.matched) + len(self.by_meaning)
        return found / self.findings if self.findings else 0.0

    @property
    def recall(self) -> float:
        return len(self.matched) / len(self.gold) if self.gold else 0.0

    @property
    def precision(self) -> float:
        return len(self.matched) / self.findings if self.findings else 0.0

    def recall_of(self, severity: str) -> tuple[int, int]:
        pool = [g for g in self.gold if g.severity == severity]
        return sum(1 for g in pool if g.id in self.matched), len(pool)


def load_gold_file(path: Path) -> GoldFile:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defects = [GoldDefect(id=str(d["id"]), scope=str(d.get("scope", "")),
                          criterion=str(d.get("criterion", "")),
                          severity=str(d.get("severity", "major")),
                          quotes=[str(q) for q in d.get("quotes", [])],
                          note=str(d.get("note", "")), kind=str(d.get("kind", "")),
                          in_image=bool(d.get("in_image", False)))
               for d in data.get("defects") or []]
    traps = [Trap(id=str(t["id"]), quotes=[str(q) for q in t.get("quotes", [])],
                  looks_like=str(t.get("looks_like", "")), why_not=str(t.get("why_not", "")))
             for t in data.get("traps") or []]
    total = data.get("requirements_total")
    return GoldFile(document=str(data.get("document", "")), defects=defects, traps=traps,
                    requirements_total=int(total) if total is not None else None)


def load_gold(path: Path) -> list[GoldDefect]:
    return load_gold_file(path).defects


SEVERITIES = ("critical", "major", "minor")
SCOPES = ("requirement", "pair", "set", "document")


def check_gold(gold: GoldFile, text: str) -> list[str]:
    """Problems that would make scoring lie: a quote missing from the document or found
    in several places, an unknown criterion or severity, a pair with one quote."""
    from reqaudit.criteria import Criterion  # noqa: PLC0415 - keeps eval light to import

    key = match_key(text)
    codes = {c.value for c in Criterion}
    problems = []
    items = [(d.id, d.quotes) for d in gold.defects if not d.in_image]
    items += [(t.id, t.quotes) for t in gold.traps]
    for item_id, quotes in items:
        if not quotes:
            problems.append(f"{item_id}: нет цитат")
        for q in quotes:
            k = match_key(q)
            if len(k) < 8:
                problems.append(f"{item_id}: цитата слишком короткая: «{q}»")
            elif key.count(k) == 0:
                problems.append(f"{item_id}: цитаты нет в документе: «{q[:80]}»")
            elif key.count(k) > 1:
                problems.append(f"{item_id}: цитата встречается {key.count(k)} раза, нужна "
                                f"однозначная: «{q[:80]}»")
    for d in gold.defects:
        if d.criterion not in codes:
            problems.append(f"{d.id}: неизвестный критерий «{d.criterion}»")
        if d.severity not in SEVERITIES:
            problems.append(f"{d.id}: неизвестная серьёзность «{d.severity}»")
        if d.scope == "pair" and len(d.quotes) < 2:
            problems.append(f"{d.id}: у пары одна цитата")
    ids = [d.id for d in gold.defects] + [t.id for t in gold.traps]
    problems += [f"{i}: id повторяется" for i in sorted({i for i in ids if ids.count(i) > 1})]
    return problems


_MARKS = {"document": ("=== DOCUMENT BEGIN ===", "=== DOCUMENT END ==="),
          "gold": ("=== GOLD BEGIN ===", "=== GOLD END ===")}


def import_generated(raw: str, corpus: Path) -> tuple[Path, Path]:
    """Split a generator answer (corpus/generator_prompt.md) into the document and its gold
    file, at the paths the gold file names. Parts «Часть k из N» are joined in order."""
    def between(name: str) -> list[str]:
        start, end = _MARKS[name]
        chunks, rest = [], raw
        while start in rest:
            rest = rest.split(start, 1)[1]
            chunk, _, rest = rest.partition(end)
            chunks.append(chunk.strip("\n"))
        return chunks

    documents, golds = between("document"), between("gold")
    if not documents or not golds:
        raise ValueError("Не найдены блоки === DOCUMENT BEGIN/END === и === GOLD BEGIN/END ===.")
    gold_text = golds[-1].strip()
    if gold_text.startswith("```"):
        gold_text = gold_text.split("\n", 1)[1].rsplit("```", 1)[0]
    data = yaml.safe_load(gold_text)
    relative = str(data["document"]).strip()
    doc_path = corpus / "inputs" / relative
    gold_path = corpus / "gold" / (Path(relative).stem + ".yaml")
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    gold_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text("\n\n".join(documents).strip() + "\n", encoding="utf-8")
    gold_path.write_text(gold_text.strip() + "\n", encoding="utf-8")
    return doc_path, gold_path


def gold_for(document: Path, corpus: Path) -> Path | None:
    """The gold file whose `document` names this input, if there is one."""
    for path in sorted((corpus / "gold").glob("*.yaml")):
        found = document_path(load_gold_file(path), corpus)
        if found is not None and found.resolve() == document.resolve():
            return path
    return None


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


def score_run(gold: list[GoldDefect], run_dir: Path, traps: list[Trap] | None = None
              ) -> RunScore:
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
                            if f["id"] not in used],
        traps_hit=_traps_hit(traps or [], [f for f in findings if f["id"] not in used], blocks),
        traps_total=len(traps or []))


def _traps_hit(traps: list[Trap], findings: list[dict], blocks: list[dict]) -> dict[str, str]:
    """Traps a finding that matched no gold defect landed on: false alarms."""
    hit: dict[str, str] = {}
    for trap in traps:
        places = {q: _blocks_of(q, blocks) for q in trap.quotes}
        as_gold = GoldDefect(id=trap.id, scope="", criterion="", severity="", quotes=trap.quotes)
        for f in findings:
            if _score(as_gold, f, places) > 0:
                hit[trap.id] = f["id"]
                break
    return hit


_JUDGE_INSTRUCTIONS = """You compare two lists about one requirements document: reference
defects found by an expert, and findings a review tool reported. For each reference defect,
name the one finding that reports the same defect, or none. The same defect means the same
problem in the same requirements, even when the finding quotes other places of the
document or words it differently. A finding about a different problem in the same place
is not the same defect. Each finding may be used for at most one reference defect. Be
strict: when unsure, answer none."""


def match_by_meaning(score: RunScore, run_dir: Path, gold: list[GoldDefect],
                     model_id: str) -> dict[str, str]:
    """Second pass, by meaning, for gold defects the place-based pass left unmatched: one
    model call per run. One to one, and only among findings the first pass did not use."""

    from pydantic import BaseModel, create_model  # noqa: PLC0415 - only this pass needs it

    from reqaudit.config import load_settings  # noqa: PLC0415
    from reqaudit.llm.client import LLMClient  # noqa: PLC0415
    from reqaudit.trace import Trace  # noqa: PLC0415

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    used = set(score.matched.values())
    findings = [f for f in state.get("findings", []) if _in_report(f) and f["id"] not in used]
    left = [g for g in gold if g.id not in score.matched]
    if not findings or not left:
        return {}
    ids = tuple(f["id"] for f in findings)

    class Pair(BaseModel):
        gold_id: str
        finding_id: str | None
        reason: str

    Answer = create_model("Answer", pairs=(list[Pair], ...))  # noqa: N806
    prompt = ("<reference>\n" + "\n".join(f"{g.id}: {g.note}" for g in left)
              + "\n</reference>\n<findings>\n"
              + "\n".join(f"{f['id']}: {f['title']}. {f.get('explanation', '')}"
                          for f in findings) + "\n</findings>\n")
    settings = load_settings()
    settings = dataclasses.replace(settings, models={"judge": [model_id]})
    client = LLMClient(settings, Trace(run_dir / "eval_trace.jsonl"))
    answer = client.structured(step="eval_match", role="judge",
                               instructions=_JUDGE_INSTRUCTIONS, prompt=prompt,
                               output_type=Answer)
    matched: dict[str, str] = {}
    taken: set[str] = set()
    for pair in answer.pairs:
        fid = pair.finding_id
        if pair.gold_id in {g.id for g in left} and fid in ids and fid not in taken:
            matched[pair.gold_id] = fid
            taken.add(fid)
    return matched


def report(gold: list[GoldDefect], scores: list[RunScore]) -> str:
    out = [f"# Оценка по эталону: {len(gold)} дефектов, прогонов: {len(scores)}", ""]
    out += ["| Прогон | Находок | Полнота | Критичные | Точность | Критерий совпал | "
            "Серьёзность совпала | Ложные на ловушках | По смыслу: полнота / точность |",
            "|---|---|---|---|---|---|---|---|---|"]
    for s in scores:
        hit, total = s.recall_of("critical")
        traps = f"{len(s.traps_hit)}/{s.traps_total}" if s.traps_total else "—"
        meaning = (f"{s.recall_by_meaning:.0%} / {s.precision_by_meaning:.0%}"
                   if s.by_meaning else "—")
        out.append(f"| {s.run} | {s.findings} | {s.recall:.0%} ({len(s.matched)}/{len(gold)}) "
                   f"| {hit}/{total} | {s.precision:.0%} | {s.criterion_agrees}/{len(s.matched)} "
                   f"| {s.severity_agrees}/{len(s.matched)} | {traps} | {meaning} |")
    out += ["", "## По дефектам эталона", "",
            "| Эталон | Серьёзность | Найден в прогонах | Находки агента | Серьёзность у агента "
            "| Что это |", "|---|---|---|---|---|---|"]
    for g in gold:
        found = sum(1 for s in scores if g.id in s.matched)
        given = Counter(s.severities[g.id] for s in scores if g.id in s.severities)
        shown = ", ".join(f"{k} ×{v}" for k, v in given.most_common()) or "—"
        ids = ", ".join(s.matched.get(g.id) or (f"{s.by_meaning[g.id]}*" if g.id in s.by_meaning
                                                  else "—") for s in scores)
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


CORPUS = Path("corpus")


def document_path(gold: GoldFile, corpus: Path) -> Path | None:
    """The input a gold file names: under corpus/inputs/, or relative to the repository."""
    for candidate in (corpus / "inputs" / gold.document, Path(gold.document)):
        if gold.document and candidate.is_file():
            return candidate
    return None


def document_text(path: Path) -> str:
    """All text of the document as the reader sees it, hidden text included, so gold quotes
    of any format can be checked without a model."""
    if path.suffix.lower() == ".pdf":
        from reqaudit.reading.pdf import read_pdf  # noqa: PLC0415 - heavy import, PDF only
        return "\n".join(d.text for d in read_pdf(path).drafts)
    if path.suffix.lower() == ".docx":
        from reqaudit.reading.office import read_docx  # noqa: PLC0415
        return "\n".join(d.text for d in read_docx(path)[0])
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="reqaudit-eval",
        description="Оценка сохранённых прогонов по эталонной разметке; модель не вызывается.")
    parser.add_argument("gold", type=Path, nargs="?", help="эталон: corpus/gold/<документ>.yaml")
    parser.add_argument("runs", type=Path, nargs="*", help="папки прогонов runs/<id>")
    parser.add_argument("--json", type=Path, help="сохранить метрики в JSON")
    parser.add_argument("--check", type=Path, nargs="+", metavar="GOLD",
                        help="проверить эталоны: цитаты дословно есть в документе и однозначны")
    parser.add_argument("--import", dest="imports", type=Path, nargs="+", metavar="ANSWER",
                        help="разрезать ответ генератора на документ и эталон в corpus/")
    parser.add_argument("--corpus", type=Path, default=CORPUS, help="папка корпуса")
    parser.add_argument("--by-meaning", metavar="MODEL",
                        help="второй проход: сопоставить оставшиеся по смыслу моделью MODEL "
                             "(один вызов на прогон); в таблице такие помечены *")
    args = parser.parse_args(argv)

    if args.imports:
        imported = []
        for raw in args.imports:
            doc, gold_path = import_generated(raw.read_text(encoding="utf-8"), args.corpus)
            print(f"{raw}: {doc} и {gold_path}")
            imported.append(gold_path)
        args.check = [*(args.check or []), *imported]
    if args.check:
        failed = 0
        for path in args.check:
            gold_file = load_gold_file(path)
            document = document_path(gold_file, args.corpus)
            if document is None:
                print(f"{path}: документ «{gold_file.document}» не найден")
                failed += 1
                continue
            problems = check_gold(gold_file, document_text(document))
            failed += bool(problems)
            status = "в порядке" if not problems else f"{len(problems)} проблем"
            print(f"{path}: {len(gold_file.defects)} дефектов, {len(gold_file.traps)} ловушек — "
                  f"{status}")
            print("".join(f"  - {p}\n" for p in problems), end="")
        return 1 if failed else 0

    if not args.gold or not args.runs:
        parser.error("нужны эталон и хотя бы одна папка прогона, либо --check/--import")
    gold_file = load_gold_file(args.gold)
    scores = [score_run(gold_file.defects, r, gold_file.traps) for r in args.runs]
    if args.by_meaning:
        for s, r in zip(scores, args.runs, strict=True):
            s.by_meaning = match_by_meaning(s, r, gold_file.defects, args.by_meaning)
    print(report(gold_file.defects, scores))
    if args.json:
        args.json.write_text(json.dumps([score_dict(s) for s in scores], ensure_ascii=False,
                                        indent=2), encoding="utf-8")
    return 0


def score_dict(s: RunScore) -> dict:
    return {"run": s.run, "findings": s.findings, "recall": round(s.recall, 4),
            "precision": round(s.precision, 4), "matched": s.matched,
            "critical_recall": s.recall_of("critical"),
            "criterion_agrees": s.criterion_agrees, "severity_agrees": s.severity_agrees,
            "traps_hit": s.traps_hit, "traps_total": s.traps_total,
            "by_meaning": s.by_meaning}
