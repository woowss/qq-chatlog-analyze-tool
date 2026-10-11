"""Local citation-source checks, independent of model inference quality."""

import re

from analyzer.dialog import _has_content, _message_line, _fit_lines, _fit_message_context
from analyzer import person_context
from parser.qq_parser import is_statistical


_QUOTES = re.compile(r"「([^「」]+)」|『([^『』]+)』|“([^“”]+)”|\"([^\"]+)\"|'([^']+)'")
_DATE = re.compile(r"(?<!\d)(20\d{2}-\d{2}(?:-\d{2})?)(?![\d-])")
_FIELDS = {"evidence", "quote", "self_evidence", "other_evidence", "group_evidence", "signature_moment"}
_QUOTED_FIELDS = {"strengths", "weaknesses", "counter_evidence", "turning_point"}
MAX_ENTRIES = 512
MAX_CANDIDATES = 100


class _InputLine(str):
    """Retain message identity through the existing sampler's line truncation."""

    def __new__(cls, text, message):
        value = super().__new__(cls, text)
        value.message = message
        return value

    def __getitem__(self, key):
        return _InputLine(super().__getitem__(key), self.message)


def _quote(text, explicit):
    matches = [next(v for v in match if v) for match in _QUOTES.findall(text)]
    if len(matches) == 1:
        return matches[0]
    if matches or not explicit:
        return ""
    text = text.strip()
    if not text or text in ("数据不足", "无", "暂无", "未发现", "没有"):
        return ""
    if text.startswith(("依据数字", "数字证据", "被回复", "共发言")):
        return ""
    # Only a bare quote field is unambiguous. Narrative evidence needs delimiters.
    return text


def _collect(result, dimension, chat):
    records = []
    participants = chat.participants()
    aliases = {}
    for person in participants:
        for name in (person.name, person.raw_name, person.uid):
            aliases.setdefault(name, set()).add(person.uid)

    def restrict_senders(scope, senders):
        scope["senders"] = scope["senders"] & senders if "senders" in scope else senders

    def restrict_date(scope, date):
        # A narrower declared day must agree with the enclosing analysis month.
        previous = scope.get("date", "")
        if previous.startswith(date):
            return
        if date.startswith(previous):
            scope["date"] = date
        else:
            scope["conflicting_date"] = True

    def restrict_ids(scope, ids):
        ids = [ids] if isinstance(ids, str) else ids
        valid = isinstance(ids, list) and bool(ids) and all(isinstance(mid, str) and mid for mid in ids)
        if not valid or ("ids" in scope and scope["ids"] is None):
            scope["ids"] = None
        else:
            # A child can add IDs, but cannot discard an invalid outer claim.
            scope["ids"] = list(dict.fromkeys(scope.get("ids", []) + ids))

    def walk(value, path, scope, explicit=False, citation=False):
        if len(records) >= MAX_ENTRIES:
            return
        if isinstance(value, dict):
            scope = dict(scope)
            date = value.get("date") or value.get("month") or value.get("period")
            if isinstance(date, str) and _DATE.fullmatch(date):
                restrict_date(scope, date)
            sender = value.get("sender_uid") or value.get("sender")
            person_entry = (dimension in ("habits", "profile", "member_profiles") and len(path) == 1) or any(
                key in path for key in ("member_emotions", "core_members")
            )
            if sender is None and citation:
                # A name inside a citation object can identify its source.
                # A name on a profile entry identifies the analysis subject,
                # which may differ from the speaker of contextual evidence.
                sender = value.get("name")
            if isinstance(sender, str):
                # An unknown supplied sender must not silently lose its constraint.
                restrict_senders(scope, aliases.get(sender, {sender}))
            if person_entry and isinstance(value.get("name"), str):
                subject_key = path[0] if path else ""
                if dimension == "member_profiles" and subject_key in {p.uid for p in participants}:
                    expected_subjects = {subject_key}
                elif dimension in ("habits", "profile") and subject_key == "self":
                    expected_subjects = {chat.self_uid} if chat.self_uid else set()
                elif dimension in ("habits", "profile") and subject_key == "other":
                    expected_subjects = (
                        {chat.other_uid}
                        if chat.other_uid
                        else {p.uid for p in participants if p.uid and p.uid != chat.self_uid}
                    )
                else:
                    expected_subjects = set()
                if expected_subjects and not (
                    aliases.get(value["name"], {value["name"]}) & expected_subjects
                ):
                    scope["conflicting_subject"] = True
            if citation and ("evidence_ids" in value or "message_id" in value):
                # A nested text/quote object must not erase its parent's IDs.
                restrict_ids(scope, value["evidence_ids"] if "evidence_ids" in value else value["message_id"])
            for key, child in value.items():
                if citation and key in (
                    "evidence_ids",
                    "message_id",
                    "sender_uid",
                    "sender",
                    "name",
                    "date",
                    "month",
                    "period",
                ):
                    continue
                child_scope = dict(scope)
                if key == "self_evidence":
                    restrict_senders(child_scope, {chat.self_uid})
                elif key == "other_evidence":
                    child_scope["exclude_self"] = True
                elif _DATE.fullmatch(key):
                    restrict_date(child_scope, key)
                    child_scope["unit"] = key
                elif dimension in ("habits", "profile") and key in ("self", "other"):
                    child_scope["unit"] = key
                elif dimension == "member_profiles" and key in {p.uid for p in participants}:
                    child_scope["unit"] = key
                is_evidence = key in _FIELDS or key in _QUOTED_FIELDS or (citation and key == "text")
                if is_evidence and "evidence_ids" in value:
                    restrict_ids(child_scope, value["evidence_ids"])
                walk(child, path + [key], child_scope, key in _FIELDS, is_evidence or citation)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + [index], scope, explicit, citation)
        elif isinstance(value, str) and citation:
            quote = _quote(value, explicit)
            if not explicit and not quote:
                return
            scope = dict(scope)
            if quote:
                # A date outside the quote is a declared source constraint.
                dates = _DATE.findall(value.replace(quote, ""))
                if len(set(dates)) == 1:
                    restrict_date(scope, dates[0])
            records.append({"path": path, "text": value, "quote": quote, "scope": scope})

    walk(result, [], {})
    return records


def _input_lines(chat, dimension, unit, person_context_enabled=None):
    """Reconstruct text selection only, without vision/network or prompt changes.

    Legacy results do not record their historical input configuration. This
    check uses the current allowed input strategy; it cannot certify old input.
    """
    from analyzer import deepseek_client as dc, group_client as gc, recap_client as rc

    messages = chat.messages
    if dimension.startswith("group_"):
        valid = gc._month_messages(chat, [m for m in messages if m.time_str.startswith(unit)])
        names = {p.uid: p.name for p in chat.participants()}

        def resolve(uid):
            return names.get(uid, "未知发送者")

        entries = [
            (i, m.sender_uid or "__unknown__", m, _message_line(m, resolve(m.sender_uid)))
            for i, m in enumerate(valid)
        ]
        selected, lines, _ = gc._fit_group_entries(
            entries, gc.GROUP_MAX_DIALOG_CHARS, lambda kept: gc._render_group_entries(kept, resolve)
        )
        return {id(entry[2]): line for entry, line in zip(selected, lines, strict=True)}
    valid = [m for m in messages if _has_content(m) and is_statistical(m)]
    if dimension in ("emotion", "topics", "relationship"):
        valid = [m for m in valid if m.time_str.startswith(unit)]
        selected, lines, _ = _fit_message_context(
            valid, chat.self_uid, chat.self_name, chat.other_name, dc.MAX_DIALOG_CHARS
        )
        return {id(m): line for m, line in zip(selected, lines, strict=True)}
    if dimension in ("profile", "member_profiles"):
        if dimension == "member_profiles":
            target = [m for m in valid if m.sender_uid == unit]
            sample_size, budget = gc.MEMBER_PROFILE_SAMPLES, gc.GROUP_MAX_DIALOG_CHARS
            names = {p.uid: p.name for p in chat.participants()}

            def name_of(message):
                return names.get(message.sender_uid, "未知发送者")
        else:
            target = [m for m in valid if (m.sender_uid == chat.self_uid) == (unit == "self")]
            target_uids = getattr(chat, "analysis_target_uids", None)
            if target_uids is not None:
                if unit == "self":
                    target = target if chat.self_uid in target_uids else []
                else:
                    target = [m for m in target if m.sender_uid in target_uids]
            sample_size, budget = 800, dc.MAX_DIALOG_CHARS

            def name_of(message):
                return chat.self_name if message.sender_uid == chat.self_uid else chat.other_name

        selection = person_context.select_person_context(
            messages,
            target,
            name_of,
            sample_size,
            budget,
            stratified=True,
            enabled=person_context_enabled,
        )
        return {id(entry.message): entry.line for entry in selection.entries}
    if dimension == "habits":
        valid = [m for m in valid if (m.sender_uid == chat.self_uid) == (unit == "self")]
        target_uids = getattr(chat, "analysis_target_uids", None)
        if target_uids is not None:
            if unit == "self":
                valid = valid if chat.self_uid in target_uids else []
            else:
                valid = [m for m in valid if m.sender_uid in target_uids]
        size, budget = 500, dc.MAX_DIALOG_CHARS
        display_name = chat.self_name if unit == "self" else chat.other_name
        lines = [_InputLine(_message_line(m, display_name), m) for m in valid[-size:]]
        kept = _fit_lines(lines, budget)
        return {id(line.message): str(line) for line in kept}
    if dimension == "recap":
        stride = max(1, (len(valid) + rc.RECAP_SAMPLE_LINES - 1) // rc.RECAP_SAMPLE_LINES)
        budget, chosen = rc.RECAP_SAMPLE_CHARS, {}
        for message in valid[::stride]:
            name = chat.self_name if message.sender_uid == chat.self_uid else chat.other_name
            line = _message_line(message, name)
            budget -= len(line) + 1
            if budget < 0:
                break
            chosen[id(message)] = line
        return chosen
    return {}


def verify_sources(chat, dimension, result, index, formatter, person_context_enabled=None):
    records = _collect(result, dimension, chat)
    matches = index.match_quotes(record["quote"] for record in records)
    input_scopes = {}
    out = []
    for record in records:
        scope, quote = record.pop("scope"), record["quote"]
        positions = matches.get(quote, [])
        positions = [
            pos
            for pos in positions
            if (
                not scope.get("conflicting_date")
                and not scope.get("conflicting_subject")
                and (not scope.get("date") or chat.messages[pos].time_str.startswith(scope["date"]))
                and ("senders" not in scope or chat.messages[pos].sender_uid in scope["senders"])
                and (not scope.get("exclude_self") or chat.messages[pos].sender_uid != chat.self_uid)
            )
        ]
        status = "insufficient" if not quote else "not_found"
        ids = scope.get("ids")
        if quote and "ids" in scope:
            ids = [ids] if isinstance(ids, str) else ids
            valid_ids = isinstance(ids, list) and bool(ids) and all(isinstance(x, str) and x for x in ids)
            identified = [index.ids.get(mid, []) for mid in ids] if valid_ids else []
            unit = scope.get("unit", "")
            if unit not in input_scopes:
                input_scopes[unit] = _input_lines(chat, dimension, unit, person_context_enabled)
            allowed = input_scopes[unit]
            # Every declared ID must be unique, in the current chat/allowed text
            # selection, and consistent with quote, sender and date.
            if identified and all(len(found) == 1 for found in identified):
                chosen = [found[0] for found in identified]
                if all(
                    pos in positions and quote in allowed.get(id(chat.messages[pos]), "") for pos in chosen
                ):
                    positions = sorted(set(chosen))
                else:
                    positions = []
            else:
                positions = []
        if quote and positions:
            status = "unique" if len(positions) == 1 else "multiple"
            # Context lookup is by ID. A missing/duplicate ID cannot safely link
            # even an otherwise unique text match to its original position.
            if any(
                not chat.messages[pos].id or len(index.ids[chat.messages[pos].id]) != 1 for pos in positions
            ):
                status, positions = "insufficient", []
        source_positions = getattr(chat, "_analysis_source_positions", None)
        candidates = []
        for pos in positions[:MAX_CANDIDATES]:
            source_position = source_positions[pos] if source_positions is not None else pos
            candidate = formatter(chat.messages[pos], chat.self_uid)
            candidate["position"] = source_position
            candidates.append(candidate)
        record.update(
            status=status,
            candidate_count=len(positions),
            candidates=candidates,
        )
        out.append(record)
    return {"entries": out, "limit": MAX_ENTRIES, "source_only": True}
