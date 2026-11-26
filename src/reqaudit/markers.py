"""Marker words that often signal a requirement defect, found by code.

A marker is a candidate, never a verdict: «быстро» in a performance target is a defect,
«клиент» in a document that defines the client is not. Reviewers in s5 must decide on
every candidate in context. The approach follows the literature on requirements smells
(Femmer et al.; weak words plus an LLM filter, 2026) and ГОСТ Р 59194-2020, Annex B.2.

Russian forms are matched by stems, so one pattern covers «удобный», «удобно», «удобного».
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from reqaudit.criteria import Criterion
from reqaudit.models import Unit

# category -> (criterion it usually affects, Russian name, patterns)
_CATEGORIES: dict[str, tuple[Criterion, str, list[str]]] = {
    "subjective": (Criterion.UNAMBIGUOUS, "субъективная оценка", [
        r"удобн\w*", r"понятн\w*", r"интуитивн\w*", r"дружественн\w*", r"эффективн\w*",
        r"оптимальн\w*", r"приемлем\w*", r"разумн\w*", r"достаточн\w*", r"адекватн\w*",
        r"гибк\w*", r"существенн\w*", r"качественн\w*", r"современн\w*", r"красив\w*",
        r"user[- ]friendly", r"easy to use"]),
    "vague_quantity": (Criterion.VERIFIABLE, "неточное количество", [
        r"несколько", r"много\w*", r"мало\w*", r"больш\w* количеств\w*", r"приблизительн\w*",
        r"около", r"почти", r"максимальн\w*", r"минимальн\w*", r"как можно \w+"]),
    "loophole": (Criterion.UNAMBIGUOUS, "лазейка", [
        r"по возможности", r"насколько это возможно", r"при необходимости",
        r"в случае необходимости", r"при наличии (?:технической )?возможности",
        r"как правило", r"допускается", r"по мере надобности", r"если потребуется",
        r"может быть", r"if possible", r"as appropriate"]),
    "open_list": (Criterion.COMPLETE, "открытый перечень", [
        r"и т\.\s?[дп]\.", r"и др\.", r"и прочее", r"но не ограничиваясь", r"etc\.?"]),
    "vague_time": (Criterion.VERIFIABLE, "неизмеримое время", [
        r"быстр\w*", r"мгновенн\w*", r"оперативн\w*", r"своевременн\w*", r"заранее",
        r"незамедлительн\w*", r"в кратчайш\w* срок\w*", r"в разумн\w* срок\w*",
        r"периодическ\w*", r"регулярн\w*", r"в реальном времени"]),
    "absolute": (Criterion.FEASIBLE, "абсолютное утверждение", [
        r"всегда", r"никогда", r"100\s?%", r"24\s?[/x×]\s?7", r"непрерывн\w*",
        r"без ограничений", r"абсолютн\w*", r"гарантированн\w*"]),
    "logic": (Criterion.UNAMBIGUOUS, "неоднозначная логика", [r"и/или", r"and/or"]),
    "unfinished": (Criterion.COMPLETE, "незавершённость", [
        r"TBD", r"TODO", r"уточняется", r"будет (?:определен\w*|уточнен\w*)",
        r"требует уточнения", r"на усмотрение", r"\?\?+", r"XXX"]),
    "generic": (Criterion.UNAMBIGUOUS, "общая формулировка", [
        r"корректн\w*", r"правильн\w*", r"должным образом", r"надлежащ\w*",
        r"соответствующ\w*", r"обеспечива\w*", r"поддержива\w*"]),
}

_PATTERNS = [(category, re.compile(rf"(?<![\wё]){p}(?![\wё])", re.IGNORECASE))
             for category, (_, _, patterns) in _CATEGORIES.items() for p in patterns]


@dataclass(frozen=True)
class MarkerHit:
    id: str
    unit_id: str
    category: str
    word: str
    criterion: Criterion

    @property
    def category_ru(self) -> str:
        return _CATEGORIES[self.category][1]


def find_markers(units: list[Unit]) -> list[MarkerHit]:
    """Marker hits in the units' quotes, one per distinct word per unit, in unit order."""
    hits: list[MarkerHit] = []
    for unit in units:
        seen: set[str] = set()
        for category, pattern in _PATTERNS:
            for match in pattern.finditer(unit.quote):
                word = match.group(0)
                if word.lower() in seen:
                    continue
                seen.add(word.lower())
                hits.append(MarkerHit(id=f"M-{len(hits) + 1:03d}", unit_id=unit.id,
                                      category=category, word=word,
                                      criterion=_CATEGORIES[category][0]))
    return hits
