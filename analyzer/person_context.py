# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Shared target-first message selection for private and group people profiles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from config import env_bool
from parser.qq_parser import Message, is_statistical
from analyzer.local_stats import SESSION_GAP_MS
from analyzer.dialog import _fit_lines, _has_content, _message_line


# Target-only image digests must not reuse results from mixed-speaker digests.
STRATEGY_VERSION = "person-context-v3"
PERSON_CONTEXT_ENABLED = env_bool("QQCHAT_PERSON_CONTEXT", True)
CONTEXT_DIMENSIONS = frozenset({"profile", "member_profiles"})
DEFAULT_NEIGHBORS_BEFORE = 2
DEFAULT_NEIGHBORS_AFTER = 1


class _SelectionLine(str):
    """Keep a line tied to its source message through the legacy line fitter."""

    def __new__(cls, value: str, message: Message, is_target: bool):
        line = super().__new__(cls, value)
        line.message = message
        line.is_target = is_target
        return line

    def __getitem__(self, key):
        return _SelectionLine(super().__getitem__(key), self.message, self.is_target)


@dataclass(frozen=True)
class SelectedMessage:
    message: Message
    line: str
    is_target: bool


@dataclass(frozen=True)
class PersonContextSelection:
    entries: tuple[SelectedMessage, ...]
    target_total: int
    target_sample_count: int
    context_count: int
    sampled: bool

    @property
    def text(self) -> str:
        return "\n".join(entry.line for entry in self.entries)

    @property
    def messages(self) -> list[Message]:
        return [entry.message for entry in self.entries]

    @property
    def target_messages(self) -> list[Message]:
        return [entry.message for entry in self.entries if entry.is_target]


def strategy_fingerprint(base_fingerprint: str, dimension: str) -> str:
    """Bind only the two people-profile cache families to this input strategy."""
    if not PERSON_CONTEXT_ENABLED or dimension not in CONTEXT_DIMENSIONS:
        return base_fingerprint
    payload = f"{base_fingerprint}:{dimension}:{STRATEGY_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _filtered(messages: list[Message]) -> list[Message]:
    return [message for message in messages if _has_content(message) and is_statistical(message)]


def _sample_targets(
    messages: list[Message], sample_size: int, stratified: bool
) -> tuple[list[Message], bool]:
    if sample_size <= 0:
        return [], bool(messages)
    if stratified and len(messages) > sample_size:
        total = len(messages)
        if sample_size == 1:
            indices = [total // 2]
        elif total % sample_size == 0:
            # Keep the established stride samples for exact buckets, e.g. 0, 2, ..., 1598.
            indices = [(index * total) // sample_size for index in range(sample_size)]
        else:
            # For uneven buckets include both ends while still selecting exactly sample_size.
            indices = [(index * (total - 1)) // (sample_size - 1) for index in range(sample_size)]
        return [messages[index] for index in indices], True
    return (messages[-sample_size:] if not stratified else messages), False


def _strict_fit(lines: list[_SelectionLine], budget: int) -> list[_SelectionLine]:
    if not lines or budget <= 1:
        return []
    fitted = list(_fit_lines(lines, budget))
    while fitted and sum(len(line) + 1 for line in fitted) > budget:
        overflow = sum(len(line) + 1 for line in fitted) - budget
        last = fitted[-1]
        keep = len(last) - overflow
        if keep <= 0:
            fitted.pop()
        else:
            fitted[-1] = last[:keep]
    return fitted


def _neighbor_candidates(
    valid_all: list[Message], targets: list[Message], target_ids: set[int], before: int, after: int
) -> tuple[list[Message], list[Message]]:
    positions = {id(message): index for index, message in enumerate(valid_all)}
    before_candidates: list[Message] = []
    after_candidates: list[Message] = []
    for target in targets:
        position = positions.get(id(target))
        if position is None:
            continue
        for distance in range(1, before + 1):
            candidate_position = position - distance
            if candidate_position < 0:
                break
            if (
                valid_all[candidate_position + 1].timestamp - valid_all[candidate_position].timestamp
                > SESSION_GAP_MS
            ):
                break
            candidate = valid_all[candidate_position]
            if id(candidate) not in target_ids:
                before_candidates.append(candidate)
        for distance in range(1, after + 1):
            candidate_position = position + distance
            if candidate_position >= len(valid_all):
                break
            if (
                valid_all[candidate_position].timestamp - valid_all[candidate_position - 1].timestamp
                > SESSION_GAP_MS
            ):
                break
            candidate = valid_all[candidate_position]
            if id(candidate) not in target_ids:
                after_candidates.append(candidate)
    return before_candidates, after_candidates


def select_person_context(
    all_messages: list[Message],
    target_messages: list[Message],
    name_of: Callable[[Message], str],
    sample_size: int,
    max_chars: int,
    *,
    stratified: bool = True,
    enabled: bool | None = None,
    neighbors_before: int = DEFAULT_NEIGHBORS_BEFORE,
    neighbors_after: int = DEFAULT_NEIGHBORS_AFTER,
) -> PersonContextSelection:
    """Select target samples first, then fill spare budget with reply and session context.

    Setting ``enabled=False`` reproduces the former target-only line selection so users can
    roll back the strategy and old profile cache identities remain usable.
    """
    enabled = PERSON_CONTEXT_ENABLED if enabled is None else enabled
    target_all = _filtered(target_messages)
    sampled_targets, stride_sampled = _sample_targets(target_all, sample_size, stratified)
    if max_chars <= 1 or not sampled_targets:
        return PersonContextSelection((), len(target_all), 0, 0, bool(stride_sampled or target_all))

    if not enabled:
        lines = [
            _SelectionLine(_message_line(message, name_of(message)), message, True)
            for message in sampled_targets
        ]
        fitted = list(_fit_lines(lines, max_chars))
        entries = tuple(SelectedMessage(line.message, str(line), True) for line in fitted)
        return PersonContextSelection(
            entries,
            len(target_all),
            len(entries),
            0,
            bool(stride_sampled or len(entries) < len(sampled_targets)),
        )

    target_lines = [
        _SelectionLine(f"[目标人物] {_message_line(message, name_of(message))}", message, True)
        for message in sampled_targets
    ]
    fitted_targets = _strict_fit(target_lines, max_chars)
    if not fitted_targets:
        return PersonContextSelection((), len(target_all), 0, 0, True)
    used_chars = sum(len(line) + 1 for line in fitted_targets)
    target_ids = {id(message) for message in target_all}
    selected_ids = {id(line.message) for line in fitted_targets}
    context_lines: list[_SelectionLine] = []
    sampled = stride_sampled or len(fitted_targets) < len(sampled_targets)

    valid_all = _filtered(all_messages)
    by_id: dict[str, list[Message]] = {}
    for message in valid_all:
        if message.id:
            by_id.setdefault(message.id, []).append(message)
    reply_targets = []
    for target in (line.message for line in fitted_targets):
        matches = by_id.get(target.reply_to_id, []) if target.reply_to_id else []
        if len(matches) == 1 and id(matches[0]) not in target_ids:
            reply_targets.append(matches[0])
    before_candidates, after_candidates = _neighbor_candidates(
        valid_all, [line.message for line in fitted_targets], target_ids, neighbors_before, neighbors_after
    )
    candidates = reply_targets + before_candidates + after_candidates
    seen_context = set(selected_ids)
    for message in candidates:
        marker = id(message)
        if marker in seen_context:
            continue
        seen_context.add(marker)
        line = _SelectionLine(f"[仅供上下文] {_message_line(message, name_of(message))}", message, False)
        cost = len(line) + 1
        if cost > max_chars - used_chars:
            sampled = True
            continue
        context_lines.append(line)
        used_chars += cost

    entries_by_position = {id(line.message): line for line in (*fitted_targets, *context_lines)}
    positions = {id(message): index for index, message in enumerate(all_messages)}
    ordered = sorted(
        entries_by_position.values(), key=lambda line: positions.get(id(line.message), len(positions))
    )
    entries = tuple(SelectedMessage(line.message, str(line), line.is_target) for line in ordered)
    return PersonContextSelection(
        entries,
        len(target_all),
        len(fitted_targets),
        len(context_lines),
        sampled,
    )
