# -*- coding: utf-8 -*-
"""计划台账（`docs/plans/`）的结构性校验。

`AGENTS.md` 的 **Plan ledger and session close-out** 要求：

1. 本轮跳过/延后且经用户确认的项写入 `docs/plans/NEXT.md`，每条含
   「为什么当时跳过 / 证据 / 下一步」三个字段；
2. 计划项完成时从 `NEXT.md` 删除，并在 `docs/plans/DONE.md` 追加一行；
3. 同一项不得同时存在于两个文件（id 唯一且不与档案重叠）；
4. 两份新文档都按 `docs/INDEX.md` 的入场规则登记。

本文件把这几条变成断言——否则台账会随文档编辑静默漂移，而"孤儿文档 /
id 重复 / 两个文件同时收录同一条"正是它想防止的失败模式。
纯文本解析，无 Qt、无网络、无第三方依赖。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PLANS = DOCS / "plans"
NEXT = PLANS / "NEXT.md"
DONE = PLANS / "DONE.md"
INDEX = DOCS / "INDEX.md"
AGENTS = ROOT / "AGENTS.md"

#: 计划项 id：`P-<YYYY-MM-DD>-<两位序号>`（见 NEXT.md 头部的条目格式）。
ENTRY_ID_RE = re.compile(r"P-\d{4}-\d{2}-\d{2}-\d{2}")

#: 条目标题行：`### P-2026-09-29-01 标题`。
ENTRY_HEADING_RE = re.compile(r"^###\s+(P-\d{4}-\d{2}-\d{2}-\d{2})\b.*$", re.M)

#: 每个条目必须具备的三个字段行（行首 "- 字段名"）。
REQUIRED_FIELDS = ("为什么当时跳过", "证据", "下一步")

#: INDEX.md 里的相对链接目标（相对 `docs/`）。
LEDGER_FILES = {
    "NEXT.md": "plans/NEXT.md",
    "DONE.md": "plans/DONE.md",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _entries(text: str) -> list[tuple[str, str]]:
    """按 `### P-...` 标题切分 NEXT.md，返回 [(id, 条目正文), ...]。"""
    matches = list(ENTRY_HEADING_RE.finditer(text))
    entries: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        entries.append((match.group(1), text[start:end]))
    return entries


@pytest.mark.parametrize("name", sorted(LEDGER_FILES))
def test_ledger_file_exists(name: str):
    """两份台账文件必须存在——它们由 `AGENTS.md` 规则指向。"""
    path = PLANS / name
    assert path.is_file(), f"计划台账文件缺失：{path}"


@pytest.mark.parametrize("name", sorted(LEDGER_FILES))
def test_ledger_file_is_registered_in_index(name: str):
    """台账是 docs/ 下的新文档，按入场规则必须登记且链接相对路径可解析。"""
    target = LEDGER_FILES[name]
    assert target in _read(INDEX), (
        f"docs/INDEX.md 未按相对路径 `{target}` 登记 {name}（新文档入场规则第 1 条）"
    )
    assert (DOCS / target).is_file(), f"docs/INDEX.md 登记的 `{target}` 指向不存在的文件"


def test_agents_declares_plan_ledger_rule():
    """`AGENTS.md` 必须仍有该规则并同时指向两份台账——删规范即红。"""
    text = _read(AGENTS)
    assert "Plan ledger and session close-out" in text, (
        "AGENTS.md 缺少 `Plan ledger and session close-out` 小节"
    )
    for key in ("docs/plans/NEXT.md", "docs/plans/DONE.md"):
        assert key in text, f"AGENTS.md 的台账规则未提到 `{key}`"


def test_next_has_at_least_one_entry():
    """空池虽合法，但格式断言不能在零条目时静默通过。"""
    entries = _entries(_read(NEXT))
    assert entries, f"{NEXT.name} 没有任何 `### P-...` 条目（格式见文件头部）"


def test_next_entry_ids_are_unique():
    """id 必须唯一——重复 id 会让"同一条"无法被可靠引用。"""
    ids = ENTRY_ID_RE.findall(_read(NEXT))
    duplicates = sorted({entry_id for entry_id in ids if ids.count(entry_id) > 1})
    assert duplicates == [], f"{NEXT.name} 存在重复计划号：{duplicates}"


def test_no_next_id_appears_in_done():
    """同一项不得同时存在于两个文件：NEXT 的 id 不能出现在 DONE。"""
    next_ids = set(ENTRY_ID_RE.findall(_read(NEXT)))
    done_ids = set(ENTRY_ID_RE.findall(_read(DONE)))
    overlap = sorted(next_ids & done_ids)
    assert overlap == [], (
        f"计划号同时出现在 {NEXT.name} 与 {DONE.name}：{overlap}"
        "（完成时应从 NEXT 删除并只保留在 DONE）"
    )


def test_every_next_entry_declares_required_fields():
    """每个条目都要有「为什么当时跳过 / 证据 / 下一步」三个字段行。"""
    for entry_id, body in _entries(_read(NEXT)):
        for field in REQUIRED_FIELDS:
            pattern = rf"^-\s*{re.escape(field)}\s*[:：]"
            assert re.search(pattern, body, re.M), (
                f"{NEXT.name} 的条目 {entry_id} 缺少字段行「- {field}：」"
            )
