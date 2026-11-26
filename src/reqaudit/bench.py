"""Bench: documents × configuration variants, run in parallel, compared in one table.

    uv run reqaudit-bench corpus/inputs/near/*.md corpus/inputs/far/*.md \\
        --model vertex:gemini-3.1-pro-preview \\
        --variant base \\
        --variant lean:REQAUDIT_THINKING_ANALYST=low,REQAUDIT_THINKING_EDITOR=low \\
        --variant flash-reviewer:REQAUDIT_MODEL_REVIEWER=vertex:gemini-3.5-flash \\
        --parallel 3 --max-cost 6

Every run is the ordinary `reqaudit` command in its own process with the variant's
environment, so what is measured is exactly what a user runs. After all runs, each one is
scored against its gold file (corpus/gold, matched by the `document` field), diagnosed
from its trace, and summarised: recall, precision, critical recall, severity agreement,
false alarms on traps, cost, time and the diagnosis warnings. `--plan` lists the runs
without starting them. Repeats after the first one bypass the cache, so they measure
stability, not the cache.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean

from reqaudit.diagnose import diagnose
from reqaudit.eval import CORPUS, gold_for, load_gold_file, score_run

_RUN_DIR = re.compile(r"Артефакты прогона:\s*(.+)$", re.MULTILINE)


@dataclass
class Variant:
    name: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class BenchRun:
    variant: str
    document: str
    repeat: int
    run_dir: str | None = None
    exit_code: int | None = None
    status: str = "—"
    seconds: float = 0.0
    cost_usd: float = 0.0
    llm_calls: int = 0
    findings: int | None = None
    recall: float | None = None
    precision: float | None = None
    critical: str = "—"
    severity_agrees: str = "—"
    traps: str = "—"
    warnings: int | None = None
    error: str = ""


def parse_variant(text: str) -> Variant:
    """«name» or «name:KEY=VALUE,KEY=VALUE»."""
    name, _, rest = text.partition(":")
    env = {}
    for pair in filter(None, (p.strip() for p in rest.split(","))):
        key, sep, value = pair.partition("=")
        if not sep or not key.startswith("REQAUDIT_"):
            raise ValueError(f"Вариант «{text}»: ожидается REQAUDIT_…=значение, получено «{pair}»")
        env[key.strip()] = value.strip()
    return Variant(name=name.strip() or "base", env=env)


def plan(documents: list[Path], variants: list[Variant], repeat: int) -> list[BenchRun]:
    return [BenchRun(variant=v.name, document=str(d), repeat=r)
            for v in variants for d in documents for r in range(repeat)]


def _one(run: BenchRun, variant: Variant, base_env: dict[str, str], out: Path,
         timeout_s: float) -> BenchRun:
    runs_dir = out / variant.name
    runs_dir.mkdir(parents=True, exist_ok=True)
    report = runs_dir / f"{Path(run.document).stem}.r{run.repeat}.report.md"
    command = [sys.executable, "-m", "reqaudit", run.document, str(report),
               "--runs-dir", str(runs_dir)] + (["--no-cache"] if run.repeat else [])
    env = {**os.environ, **base_env, **variant.env,
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    started = time.monotonic()
    try:
        done = subprocess.run(command, env=env, capture_output=True, text=True,  # noqa: S603
                              encoding="utf-8", errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        run.error = f"не уложился в {timeout_s / 60:.0f} мин"
        return run
    run.seconds = time.monotonic() - started
    run.exit_code = done.returncode
    found = _RUN_DIR.search(done.stdout or "")
    if found:
        run.run_dir = found.group(1).strip()
    else:
        run.error = ((done.stderr or done.stdout or "").strip().splitlines() or ["—"])[-1][:200]
    return run


def measure(run: BenchRun, corpus: Path) -> BenchRun:
    """Fill the run's numbers from its files: passport, gold score, diagnosis."""
    if not run.run_dir:
        return run
    run_dir = Path(run.run_dir)
    passport_path = run_dir / "passport.json"
    if passport_path.exists():
        passport = json.loads(passport_path.read_text(encoding="utf-8"))
        run.status = passport.get("status", "—")
        run.cost_usd = float(passport.get("llm_cost_usd") or 0.0)
        run.llm_calls = int(passport.get("llm_calls") or 0)
    gold_path = gold_for(Path(run.document), corpus)
    if gold_path and (run_dir / "state.json").exists():
        gold = load_gold_file(gold_path)
        score = score_run(gold.defects, run_dir, gold.traps)
        run.findings = score.findings
        run.recall = score.recall if gold.defects else None
        run.precision = score.precision if score.findings else None
        hit, total = score.recall_of("critical")
        run.critical = f"{hit}/{total}" if total else "—"
        run.severity_agrees = f"{score.severity_agrees}/{len(score.matched)}"
        run.traps = f"{len(score.traps_hit)}/{score.traps_total}" if score.traps_total else "—"
    elif (run_dir / "state.json").exists():
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        run.findings = sum(1 for f in state.get("findings", [])
                           if f.get("status") == "confirmed"
                           and f.get("answer_decision") != "resolved")
    run.warnings = len(diagnose(run_dir).warnings)
    return run


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def summary(runs: list[BenchRun]) -> str:
    out = ["# Сравнение вариантов", "",
           "| Вариант | Документ | Повтор | Статус | Находок | Полнота | Критичные | Точность | "
           "Серьёзность | Ловушки | Замечаний | $ | Мин |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        status = r.status if not r.error else f"ошибка: {r.error}"
        out.append(f"| {r.variant} | {Path(r.document).name} | {r.repeat + 1} | {status} | "
                   f"{'—' if r.findings is None else r.findings} | {_pct(r.recall)} | "
                   f"{r.critical} | {_pct(r.precision)} | {r.severity_agrees} | {r.traps} | "
                   f"{'—' if r.warnings is None else r.warnings} | {r.cost_usd:.2f} | "
                   f"{r.seconds / 60:.1f} |")
    out += ["", "## По вариантам", "",
            "| Вариант | Прогонов | Средняя полнота | Средняя точность | Всего $ | "
            "Среднее время, мин |", "|---|---|---|---|---|---|"]
    for name in dict.fromkeys(r.variant for r in runs):
        group = [r for r in runs if r.variant == name]
        recalls = [r.recall for r in group if r.recall is not None]
        precisions = [r.precision for r in group if r.precision is not None]
        out.append(f"| {name} | {len(group)} | {_pct(mean(recalls)) if recalls else '—'} | "
                   f"{_pct(mean(precisions)) if precisions else '—'} | "
                   f"{sum(r.cost_usd for r in group):.2f} | "
                   f"{mean(r.seconds for r in group) / 60:.1f} |")
    out += ["", "Полнота и точность — по эталону corpus/gold; документы без эталона дают только "
                "число находок. Разбор каждого прогона: `uv run reqaudit-diagnose <папка>`."]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="reqaudit-bench",
        description="Прогон документов в нескольких вариантах настроек параллельно и "
                    "сравнение по эталону, стоимости и времени.")
    parser.add_argument("documents", type=Path, nargs="+", help="входные документы")
    parser.add_argument("--variant", action="append", default=[],
                        help="имя или имя:REQAUDIT_X=значение,REQAUDIT_Y=значение; "
                             "можно несколько раз; без него — один вариант base")
    parser.add_argument("--model", help="цепочка судьи для всех вариантов (REQAUDIT_MODEL_JUDGE)")
    parser.add_argument("--repeat", type=int, default=1, help="прогонов на документ и вариант")
    parser.add_argument("--parallel", type=int, default=3, help="прогонов одновременно")
    parser.add_argument("--max-cost", type=float, help="потолок стоимости одного прогона, $")
    parser.add_argument("--timeout-min", type=float, default=60, help="потолок времени прогона")
    parser.add_argument("--out", type=Path, help="папка стенда; по умолчанию runs/bench/<время>")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--plan", action="store_true", help="только показать, что запустится")
    args = parser.parse_args(argv)

    variants = [parse_variant(v) for v in args.variant] or [Variant("base")]
    names = [v.name for v in variants]
    if len(set(names)) != len(names):
        parser.error("имена вариантов должны различаться")
    missing = [d for d in args.documents if not d.is_file()]
    if missing:
        parser.error("нет файлов: " + ", ".join(map(str, missing)))
    runs = plan(args.documents, variants, max(1, args.repeat))
    base_env = {}
    if args.model:
        base_env["REQAUDIT_MODEL_JUDGE"] = args.model
    if args.max_cost:
        base_env["REQAUDIT_MAX_COST_USD"] = str(args.max_cost)

    print(f"Прогонов: {len(runs)} ({len(args.documents)} документов × {len(variants)} "
          f"вариантов × {max(1, args.repeat)} повторов), одновременно {args.parallel}.")
    for v in variants:
        gold = sum(1 for d in args.documents if gold_for(d, args.corpus))
        print(f"  {v.name}: {v.env or 'настройки по умолчанию'}; с эталоном {gold} из "
              f"{len(args.documents)} документов")
    if args.plan:
        return 0

    out = args.out or Path("runs") / "bench" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    by_name = {v.name: v for v in variants}
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = [pool.submit(_one, r, by_name[r.variant], base_env, out, args.timeout_min * 60)
                   for r in runs]
        for n, future in enumerate(futures, 1):
            r = future.result()
            print(f"[{n}/{len(runs)}] {r.variant} {Path(r.document).name}: "
                  f"{r.error or r.run_dir}", flush=True)
    runs = [measure(r, args.corpus) for r in runs]
    text = summary(runs)
    (out / "summary.md").write_text(text, encoding="utf-8")
    (out / "results.json").write_text(json.dumps([asdict(r) for r in runs], ensure_ascii=False,
                                                 indent=2), encoding="utf-8")
    print(text)
    print(f"Сводка: {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
