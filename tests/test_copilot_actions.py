"""Copilot actions (apps/sales_copilot/actions.py): which questions become a spreadsheet download or a
"create …" proposal, and the CSV export. Pure logic — no database, no LLM, nothing is created."""

import pytest

from apps.sales_copilot import actions, chat, exports

NO_ENTS = {"persona": None, "persona_source": None, "accounts": [], "ambiguous": []}


@pytest.mark.parametrize("q,intent,kind", [
    ("create a task for each of them", "create", "task"),
    ("Provide me list of it in Excel file for download or are able to create the task in introduction for each of them ?",
     "create", "introduction"),
    ("Add them to the introduction wishlist", "create", "introduction"),
    ("add a deal for BNY", "create", "deal"),
    ("schedule a follow-up with Robin", "create", "task"),
    ("create a task to email Robin", "create", "task"),
    ("generate the complete CSV content for direct opening in Excel", "export", None),
    ("Export these to Excel", "export", None),
    ("download all contacts at BlackRock", "export", None),
])
def test_detect(q, intent, kind):
    assert actions.detect(q) == intent
    if kind:
        assert actions.create_kind(q) == kind


@pytest.mark.parametrize("q", [
    "open deals at BNY",                    # a pipeline question, not "open a deal"
    "Create an intro email for Robin",      # a draft
    "write a follow-up email to Robin",
    "which deals are stuck",
    "What is BNY hiring for?",
])
def test_not_an_action(q):
    assert actions.detect(q) is None


def test_route_sends_actions_before_other_intents():
    assert chat.route("Export these to Excel", NO_ENTS) == "export"
    assert chat.route("create a task for each of them", NO_ENTS) == "create"
    assert chat.route("which deals are stuck", NO_ENTS) == "deals"
    assert chat.route("remember that Robin prefers morning calls", NO_ENTS) == "remember"


def test_people_table_only_reuses_people():
    assert actions._people_table({"table": {"kind": "deals", "rows": [{"deal": "x"}]}}) == []
    assert actions._people_table({"table": {"kind": "people", "rows": [{"name": "Ana"}]}}) == [{"name": "Ana"}]


def test_table_csv_is_excel_safe():
    out = exports.table_csv({"columns": ["name", "title"],
                             "rows": [{"name": "=HYPERLINK(1)", "title": "VP, Tech"}, {"name": "Ana", "title": None}]})
    text = out.decode("utf-8")
    assert text.startswith("﻿Name,Title")          # BOM: Excel reads UTF-8
    assert "'=HYPERLINK(1)" in text                     # formula neutralised
    assert '"VP, Tech"' in text and "Ana," in text
