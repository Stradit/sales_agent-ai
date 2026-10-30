"""Copilot actions (apps/sales_copilot/actions.py): which questions become a spreadsheet download or a
"create …" proposal, and the CSV export. Pure logic — no database, no LLM, nothing is created."""

import pytest

from apps.sales_copilot import actions, chat, exports, records

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


@pytest.mark.parametrize("q,kind", [
    ("log a call with Robin Vince about pricing", "activity"),
    ("Log an email to Robin Vince", "activity"),
    ("Log a meeting with BNY", "activity"),
    ("I just met Robin", "activity"),
    ("log a follow-up call with Robin", "activity"),     # an activity, not a follow-up task
    ("Create a follow-up task for Robin Vince", "task"),
])
def test_activity_vs_task(q, kind):
    assert actions.detect(q) == "create"
    assert actions.create_kind(q) == kind


@pytest.mark.parametrize("q,target", [
    ("move the BNY pilot to Contract", "contract"),     # "pilot" is the deal name; the target follows "to"
    ("mark the BlackRock deal as won", "won"),
    ("advance the DTCC deal to the next stage", None),  # None = the next stage
    ("move it back to discovery", "discovery"),
    ("set the stage to closed-lost", "lost"),
])
def test_move_deal(q, target):
    assert actions.detect(q) == "move_deal"
    assert actions._target_stage(q) == target


@pytest.mark.parametrize("q", ["mark the task as done", "set the deal value to 300k", "Show open deals at BNY"])
def test_not_a_move(q):
    assert actions.detect(q) != "move_deal"


def test_task_titles():
    assert actions._task_title("create a task to send Robin the deck", {"name": "Robin"}, True) == "Send Robin the deck"
    assert actions._task_title("Create a follow-up task for Robin Vince", {"name": "Robin Vince"}, True) == "Follow up with Robin Vince"
    assert actions._task_title("create a task for each of them", {"name": "Ana", "title": "VP"}, False) == "Reach out to Ana (VP)"


@pytest.mark.parametrize("q,intent", [
    ("show the BNY pilot deal", "deal_info"),
    ("what's the status of the BlackRock deal?", "deal_info"),
    ("status of the introduction to Robin Vince", "intro_info"),
    ("set the BNY pilot deal value to 300k", "update_deal"),
    ("change the close date of the DTCC deal to 15 Dec", "update_deal"),
    ("rename the BNY deal to BNY data quality pilot", "update_deal"),
    ("mark the intro to Robin as accepted", "update_intro"),
    ("update the next step of the intro to Robin to book the meeting", "update_intro"),
    ("move the BNY pilot to Contract", "move_deal"),             # a stage change stays a move
    ("Show open deals at BNY", None),                            # the pipeline list
    ("Prep me for the intro call with Robin", None),
    ("Create an intro email for Robin", None),
])
def test_record_intents(q, intent):
    assert actions.detect(q) == intent


def test_deal_changes_parse_values():
    assert records.deal_changes("set the BNY pilot deal value to 300k") == {"value_amount": 300000.0}
    assert records.deal_changes("set the probability of the BNY deal to 60%") == {"probability": 60}
    assert records.deal_changes("set the forecast category of the BNY deal to commit") == {"forecast_category": "commit"}
    assert records.deal_changes("update the next step of the BNY deal to send the SOW") == {"next_step": "send the SOW"}
    assert records.deal_changes("set the close date of the BNY deal to 2026-11-20") == {"expected_close": "2026-11-20"}
    assert records.deal_changes("set the close date of the BNY deal to December") == {"expected_close": ""}   # too vague: user fills it


@pytest.mark.parametrize("q", ["yes", "Yes please", "yeah do that", "sure", "go ahead", "ok", "do it", "yes, thanks"])
def test_short_confirm(q):
    assert chat.CONFIRM_RE.match(q) and not chat.DECLINE_RE.match(q)


@pytest.mark.parametrize("q", ["no", "nope", "no thanks", "ok thanks", "that's all", "never mind"])
def test_short_decline(q):
    assert chat.DECLINE_RE.match(q)


@pytest.mark.parametrize("q", ["yes I want the BNY list", "what about BNY?", "ok prep me for Robin"])
def test_not_a_short_reply(q):
    assert not chat.CONFIRM_RE.match(q) and not chat.DECLINE_RE.match(q)


def test_offer_prefix_is_dropped():
    assert chat.OFFER_PREFIX_RE.sub("", "Would you like me to draft an email to Robin") == "draft an email to Robin"


def test_topicless_words_are_not_keywords():
    from apps.sales_copilot import retrieve
    assert retrieve._or_tsquery("yes", set()) is None
    assert retrieve._or_tsquery("ok thanks", set()) is None
    assert retrieve._or_tsquery("latest news on tokenization", set()) == "tokenization"
