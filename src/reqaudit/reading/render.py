"""How the document is shown to a model: block ids, kind tags and section markers.

    § Часть A > Функциональные требования
    [b0023] LI: * **Ф-1.** Держатель активной карты ...
    [b0040] TR: | Расходы 48 500 ₸ | Провести покупку | Итог 50 000 ₸ |

Hidden blocks are never shown to a model: they may carry instructions aimed at it.
"""

from __future__ import annotations

from reqaudit.models import Block, BlockKind

TAGS = {
    BlockKind.HEADING: "H",
    BlockKind.PARAGRAPH: "P",
    BlockKind.LIST_ITEM: "LI",
    BlockKind.TABLE: "TH",
    BlockKind.TABLE_ROW: "TR",
    BlockKind.CODE: "CODE",
    BlockKind.CAPTION: "CAP",
    BlockKind.HEADER_FOOTER: "HF",
    BlockKind.IMAGE_TEXT: "IMG",
}

LEGEND = ("Each line starts with a block id in square brackets and a tag: H heading, "
          "P paragraph, LI list item, TH table header row, TR table row, CODE code block, "
          "CAP caption, IMG text read from an image. Lines starting with § give the section "
          "path of the blocks below them.")


def visible(blocks: list[Block]) -> list[Block]:
    return [b for b in blocks if not b.hidden]


def render_block(block: Block) -> str:
    lines = block.text.split("\n")
    head = f"[{block.id}] {TAGS[block.kind]}: {lines[0]}"
    return "\n".join([head, *("    " + line for line in lines[1:])])


def render_blocks(blocks: list[Block]) -> str:
    out: list[str] = []
    current: list[str] | None = None
    for block in visible(blocks):
        path = block.anchor.section_path
        if block.kind == BlockKind.HEADING:
            current = path
        elif path != current:
            out.append("§ " + (" > ".join(path) if path else "(before the first heading)"))
            current = path
        out.append(render_block(block))
    return "\n".join(out)


def render_outline(blocks: list[Block], max_line: int = 160) -> str:
    """Headings plus the first line of each table, for a structure-only view."""
    out = []
    for block in visible(blocks):
        if block.kind in (BlockKind.HEADING, BlockKind.TABLE):
            first = block.text.split("\n")[0]
            out.append(f"[{block.id}] {TAGS[block.kind]}: {first[:max_line]}")
    return "\n".join(out)


def split_windows(blocks: list[Block], max_chars: int) -> list[list[Block]]:
    """Split visible blocks into windows of at most `max_chars` rendered characters.

    Cuts prefer the last heading inside the window so a section is not torn apart.
    A single block longer than the limit gets a window of its own.
    """
    shown = visible(blocks)
    windows: list[list[Block]] = []
    current: list[Block] = []
    size = 0
    for block in shown:
        cost = len(render_block(block)) + 1
        if current and size + cost > max_chars:
            cut = max((i for i, b in enumerate(current) if b.kind == BlockKind.HEADING and i > 0),
                      default=len(current))
            windows.append(current[:cut])
            current = current[cut:]
            size = sum(len(render_block(b)) + 1 for b in current)
        current.append(block)
        size += cost
    if current:
        windows.append(current)
    return windows
