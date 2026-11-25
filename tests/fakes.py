"""Fake models for tests: deterministic stand-ins that answer by the output schema.

`fake_team` plays every role: profiler, segmenter, reviewer, opponent and arbiter. It
reads what it needs from the prompt the pipeline built, so tests exercise the real
prompts, checks and gates without a network.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

_BLOCK_LINE = re.compile(r"^\[(b\d{4})\] (\w+): (.*)$", re.MULTILINE)
_TO_CHECK = re.compile(r"^(R-\d{3}) \(.*?blocks ([b\d, ]+)\): «(.*)»$", re.MULTILINE)
_REF = re.compile(r"^([FD]\d+): .*\n\s+title: (.*)$", re.MULTILINE)
_CHECK = re.compile(r"^Check: (.*)\.$", re.MULTILINE)

Mutate = Callable[[dict, int], dict]


def user_prompt(messages) -> str:
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return part.content
    return ""


def honest_segmentation(prompt: str) -> dict:
    """List items and table rows become units; other non-heading blocks are skipped."""
    units, skipped = [], []
    for block_id, tag, text in _BLOCK_LINE.findall(prompt):
        if tag in ("H", "TH"):
            continue
        if not text.strip(" |"):
            skipped.append({"block_id": block_id, "reason": "empty"})
        elif tag in ("LI", "TR"):
            units.append({"block_ids": [block_id], "doc_label": None, "kind": "functional",
                          "quote": text, "summary": "Требование из документа"})
        else:
            skipped.append({"block_id": block_id, "reason": "context"})
    return {"notes": "Требования заданы списками и таблицами.", "units": units,
            "skipped": skipped}


def _finding(criterion, unit_ids, evidence, title, severity="major") -> dict:
    return {"criterion": criterion, "unit_ids": unit_ids, "evidence": evidence,
            "title": title, "explanation": "Формулировка допускает разные толкования.",
            "severity": severity, "suggestion": "Указать точное значение.",
            "question": f"Как понимать: {title.lower()}?", "question_to": "analyst"}


def honest_review(prompt: str) -> dict:
    """Requirements cell: one verifiability finding on the first requirement."""
    listed = _TO_CHECK.findall(prompt)
    findings = []
    if listed:
        unit, blocks, quote = listed[0]
        findings.append(_finding("verifiable", [unit],
                                 [{"block_id": blocks.split(",")[0].strip(), "quote": quote}],
                                 f"Непроверяемая формулировка в {unit}"))
    return {"reviewed": [{"unit_id": u, "note": "Проверено"} for u, _, _ in listed],
            "findings": findings}


def honest_set_review(prompt: str) -> dict:
    """Consistency cell: a conflict between the first two list items; other cells: nothing."""
    check = _CHECK.search(prompt).group(1)
    findings = []
    if check == "Непротиворечивость набора":
        items = [(b, t) for b, tag, t in _BLOCK_LINE.findall(prompt) if tag == "LI"][:2]
        if len(items) == 2:
            findings.append(_finding("set_consistent", [],
                                     [{"block_id": b, "quote": t} for b, t in items],
                                     "Два требования противоречат друг другу", "critical"))
    return {"analysis": "Сравнены повторяющиеся параметры.", "findings": findings}


_EDITOR_ITEM = re.compile(r"^(D-\d{3}): .*severity=(\w+).*\n\s+title: (.*)$", re.MULTILINE)


def honest_editor(prompt: str) -> dict:
    """Findings with identical titles are one defect; one question per remaining finding."""
    items = _EDITOR_ITEM.findall(prompt)
    by_title: dict[str, list[str]] = {}
    for fid, _, title in items:
        by_title.setdefault(title, []).append(fid)
    groups = [{"finding_ids": ids, "keep": ids[0], "reason": "Одинаковый дефект в одном месте."}
              for ids in by_title.values() if len(ids) > 1]
    questions = [{"text": f"Что имелось в виду: {title.lower()}?", "finding_ids": ids,
                  "question_to": "analyst"} for title, ids in by_title.items()]
    return {"duplicates": groups, "questions": questions}


def honest_page(prompt: str) -> dict:
    """What a transcriber would return for a scanned page of a small specification."""
    return {"blocks": [
        {"kind": "heading", "text": "Требования к уведомлениям", "level": 1},
        {"kind": "list_item", "text": "Ф-1. Клиент задаёт порог расходов в тенге."},
        {"kind": "table_row", "text": "Поле | Значение"},
        {"kind": "table_row", "text": "Порог | от 1 000 до 1 000 000 ₸"},
    ]}


def honest_picture(prompt: str) -> dict:
    return {"kind": "diagram",
            "description": "Схема взаимодействия процессинга, сервиса расходов и сервиса push.",
            "text_in_image": "Процессинг / Сервис расходов / Push / Push отправляется не позднее "
                             "60 секунд после события",
            "statements": ["Push отправляется не позднее 60 секунд после события"]}


def fake_team(mutate_segment: Mutate | None = None,
              review: Callable[[str], dict] = honest_review,
              set_review: Callable[[str], dict] = honest_set_review,
              refute: Callable[[str], bool] = lambda title: False,
              keep_disputed: bool = False,
              editor: Callable[[str], dict] = honest_editor,
              page: Callable[[str], dict] = honest_page,
              picture: Callable[[str], dict] = honest_picture,
              facts: Callable[[str], dict] = lambda prompt: {"facts": []}):
    """A model that plays every role. Returns the model and per-role call counters.

    `refute(title)` decides which findings the opponent refutes; `keep_disputed` is the
    arbiter's answer for them.
    """
    calls = {"profile": 0, "segment": 0, "review": 0, "opponent": 0, "arbiter": 0,
             "editor": 0, "page": 0, "picture": 0, "facts": 0}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        tool = info.output_tools[0]
        fields = set(tool.parameters_json_schema.get("properties", {}))
        prompt = user_prompt(messages)
        if "parts" in fields:
            calls["profile"] += 1
            first = _BLOCK_LINE.search(prompt).group(1)
            args = {"summary": "Тестовый документ требований.",
                    "parts": [{"name": "Документ", "role": "main", "start_block_id": first,
                               "parent": None}],
                    "numbering": "нумерации нет", "has_acceptance_criteria": True,
                    "has_api_spec": False, "has_data_requirements": False,
                    "has_non_functional": False, "has_ui_spec": False}
        elif "skipped" in fields:
            calls["segment"] += 1
            args = honest_segmentation(prompt)
            if mutate_segment:
                args = mutate_segment(args, calls["segment"])
        elif "reviewed" in fields:
            calls["review"] += 1
            args = review(prompt)
        elif "analysis" in fields:
            calls["review"] += 1
            args = set_review(prompt)
        elif "verdicts" in fields:
            calls["opponent"] += 1
            args = {"verdicts": [
                {"ref": ref, "verdict": "refuted" if refute(title) else "upheld",
                 "reasoning": "Проверено по тексту документа.", "counter_evidence": []}
                for ref, title in _REF.findall(prompt)]}
        elif "decisions" in fields:
            calls["arbiter"] += 1
            args = {"decisions": [
                {"ref": ref, "keep": keep_disputed, "reasoning": "Решено по тексту документа."}
                for ref, _ in _REF.findall(prompt)]}
        elif "duplicates" in fields:
            calls["editor"] += 1
            args = editor(prompt)
        elif fields == {"facts"}:
            calls["facts"] += 1
            args = facts(prompt)
        elif fields == {"blocks"}:
            calls["page"] += 1
            args = page(prompt)
        elif "statements" in fields:
            calls["picture"] += 1
            args = picture(prompt)
        else:
            raise AssertionError(f"unexpected schema: {sorted(fields)}")
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=args)])

    return FunctionModel(respond), calls


def fake_analyst(mutate: Mutate | None = None):
    """Backward-compatible name used by segmentation tests."""
    return fake_team(mutate_segment=mutate)
