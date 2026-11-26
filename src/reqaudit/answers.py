"""The answers file: the agent's questions with room for the author's answers.

After every run the agent writes `<report>.answers.md` next to the report. The author
fills in the «Ответ:» lines and runs the agent again with `--answers <file>`; the agent
then re-checks the findings each answer concerns.

Each question carries a short fingerprint of its text, so an answer is applied only to
the question it was written for, even if a later run numbers the questions differently.
"""

from __future__ import annotations

import hashlib
import re

from reqaudit.evidence import match_key
from reqaudit.models import Question

_HEADING = re.compile(r"^## (Q-\d+)\s+\[([0-9a-f]{8})\]", re.MULTILINE)
_ANSWER = re.compile(r"^Ответ:[ \t]*(.*)$", re.MULTILINE | re.DOTALL)


def fingerprint(question_text: str) -> str:
    return hashlib.sha256(match_key(question_text).encode("utf-8")).hexdigest()[:8]


def render_template(questions: list[Question], document_name: str,
                    existing: dict[str, str] | None = None) -> str:
    """The answers file; answers already given are kept in place."""
    existing = existing or {}
    lines = [
        "# Ответы на вопросы агента",
        "",
        f"Документ: {document_name}",
        "",
        "Впишите ответ после «Ответ:» под каждым вопросом, на который можете ответить; "
        "остальные оставьте пустыми. Затем запустите агента ещё раз с этим файлом:",
        "",
        f"    reqaudit {document_name} --answers <путь к этому файлу>",
        "",
        "Агент перепроверит находки, к которым относится каждый ответ. Квадратные скобки "
        "после номера вопроса не меняйте: по ним ответ находит свой вопрос.",
        "",
    ]
    for q in questions:
        mark = fingerprint(q.text)
        flags = ", блокирует разработку" if q.blocks_development else ""
        lines += [f"## {q.id} [{mark}]", "",
                  f"Кому: {q.addressee}{flags}. Дефекты: {', '.join(q.finding_ids)}.", "",
                  q.text, "", f"Ответ: {existing.get(mark, '')}".rstrip(), ""]
    return "\n".join(lines)


def parse(text: str) -> dict[str, str]:
    """Fingerprint -> answer, for every question that has a non-empty answer."""
    answers: dict[str, str] = {}
    headings = list(_HEADING.finditer(text))
    for n, heading in enumerate(headings):
        end = headings[n + 1].start() if n + 1 < len(headings) else len(text)
        body = text[heading.end():end]
        found = _ANSWER.search(body)
        answer = " ".join(found.group(1).split()) if found else ""
        if answer:
            answers[heading.group(2)] = answer
    return answers
