"""Command line entry point: `reqaudit <document> <report>`."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from reqaudit import __version__
from reqaudit.answers import parse as parse_answers
from reqaudit.config import PROFILES, load_settings
from reqaudit.pipeline import answers_path_for, run_pipeline


def _utf8_console() -> None:
    # Windows consoles default to a legacy code page and fail on Russian text and arrows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reqaudit",
        description="Проверяет документ требований по ISO/IEC/IEEE 29148 и IEEE 830 "
                    "и записывает отчёт на русском языке.",
    )
    parser.add_argument("input", type=Path,
                        help="путь к документу требований: PDF, DOCX, MD, TXT, HTML, RTF")
    parser.add_argument("output", type=Path, nargs="?",
                        help="путь к файлу отчёта; по умолчанию reports/<имя документа>.report.md")
    parser.add_argument("--profile", choices=PROFILES,
                        help="профиль запуска; по умолчанию из REQAUDIT_PROFILE или quick")
    parser.add_argument("--runs-dir", type=Path,
                        help="папка для артефактов прогона; по умолчанию runs/")
    parser.add_argument("--no-cache", action="store_true",
                        help="не брать ответы модели из кэша; новые ответы всё равно сохраняются")
    parser.add_argument("--html", action="store_true",
                        help="записать рядом с отчётом тот же отчёт одним HTML-файлом, "
                             "который открывается без сервера")
    parser.add_argument("--answers", type=Path,
                        help="файл с ответами автора на вопросы агента; агент перепроверит "
                             "связанные находки")
    parser.add_argument("--ask", action="store_true",
                        help="задать вопросы агента в терминале и перепроверить находки "
                             "по ответам")
    parser.add_argument("--version", action="version", version=f"Specula (reqaudit) {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(profile=args.profile, runs_dir=args.runs_dir,
                                 cache_read=not args.no_cache)
    except ValueError as err:
        print(f"Ошибка настройки: {err}", file=sys.stderr)
        return 2

    for note in settings.notes:
        print(note)
    output = args.output or Path("reports") / f"{args.input.stem}.report.md"
    answers: dict[str, str] = {}
    if args.answers is not None:
        if not args.answers.is_file():
            print(f"Файл ответов не найден: {args.answers}", file=sys.stderr)
            return 2
        answers = parse_answers(args.answers.read_text(encoding="utf-8"))
        print(f"Ответов автора в файле: {len(answers)}")
    print(f"Проверяю {args.input} ...")
    result = run_pipeline(args.input, output, settings, answers=answers,
                          ask=input if args.ask else None)
    print(f"Отчёт: {result.report_path}")
    if args.html:
        # Imported here: the page is read only when asked for.
        from reqaudit.ui import rundata

        html = rundata.write_html(result.run_dir, result.report_path.with_suffix(".html"))
        shutil.copyfile(html, result.run_dir / "report.html")
        print(f"Отчёт HTML: {html}")
    answers_file = answers_path_for(output)
    if answers_file.exists():
        print(f"Вопросы для ответов автора: {answers_file}")
    print(f"Артефакты прогона: {result.run_dir}")
    print(f"Расход модели: {result.spend}")
    if result.status != "completed":
        print(f"Статус прогона: {result.status}", file=sys.stderr)
    return result.exit_code
