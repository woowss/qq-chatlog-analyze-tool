"""Issue #11: configuration-aware cache compatibility, with real temporary cache files."""

import os
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta
from functools import partial
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc, group_client as gc, month_cache as mc  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from webapp import store  # noqa: E402


def _chat(group: bool, text_size: int = 2) -> ChatData:
    base = datetime(2025, 1, 1, 8, tzinfo=CST)
    messages = []
    for i, uid in enumerate(("u1", "u2")):
        when = base + timedelta(minutes=i)
        text = ("a" if i == 0 else "b") * text_size
        messages.append(
            Message(
                id=str(i),
                timestamp=int(when.timestamp() * 1000),
                time_str=when.strftime("%Y-%m-%d %H:%M:%S"),
                sender_name=uid,
                sender_uid=uid,
                text=text,
                raw_text=text,
                msg_type="text",
                has_image=False,
                is_reply=False,
            )
        )
    return ChatData(
        chat_name="group" if group else "",
        self_uid="u1",
        self_name="A",
        other_uid="" if group else "u2",
        other_name="group" if group else "B",
        messages=messages,
        is_group_chat=group,
        mode="group" if group else "private",
        chat_type="group" if group else "private",
    )


class TestDialogCacheCompatibility(unittest.TestCase):
    def setUp(self):
        salts = mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": "", "QQCHAT_GROUP_CACHE_SALT": ""})
        salts.start()
        self.addCleanup(salts.stop)

    def test_same_custom_configuration_reuses_paid_dimension_and_month_results(self):
        # Captured using the pre-issue-11 AST formula, not the new implementation.
        cases = (
            (False, 10_000, "", "d305632c87ea"),
            (True, 10_000, "", "04a4e41ad76d"),
            (False, 12_345, "review-salt", "0d4934d937e2"),
            (True, 12_345, "review-salt", "60fb47176e88"),
        )
        for group, budget, salt, historical in cases:
            with self.subTest(group=group, budget=budget, salt=salt), tempfile.TemporaryDirectory() as tmp:
                chat = _chat(group)
                module, knob = (gc, "GROUP_MAX_DIALOG_CHARS") if group else (dc, "MAX_DIALOG_CHARS")
                dimension = "group_dynamics" if group else "emotion"
                make_prompt = gc._group_month_prompt if group else dc._month_prompt
                with (
                    mock.patch.object(module, knob, budget),
                    mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": salt, "QQCHAT_MONTH_CACHE": "1"}),
                    mock.patch.object(store, "AI_CACHE_DIR", tmp),
                    mock.patch.object(mc, "_MONTH_CACHE_DIR", tmp),
                    mock.patch.object(dc, "thinking_enabled", return_value=False),
                    mock.patch.object(
                        dc, "_call_api", side_effect=AssertionError("paid cache missed")
                    ) as api,
                ):
                    current = dc.analysis_cache_fingerprint(dimension, chat)
                    self.assertEqual(current, historical)
                    store._write_cache(dimension, "compat", {"paid": True}, fingerprint=historical)
                    self.assertEqual(
                        store._read_cache(dimension, "compat", fingerprint=current), {"paid": True}
                    )
                    prompt = make_prompt(chat, "2025-01", chat.messages)
                    self.assertEqual(prompt.cache_fingerprint, historical)
                    key = mc._month_key("SYS", prompt, historical)
                    mc._write_month_cache(key, {"paid": True}, thinking=False)
                    result = dc._analyze_periods(
                        chat.months(),
                        "SYS",
                        partial(make_prompt, chat),
                        max_tokens=16,
                        tag=dimension,
                        chat_hash="compat",
                    )
                    self.assertTrue(result["2025-01"]["paid"])
                    api.assert_not_called()

    def test_increased_budget_rejects_old_sampled_dimension_and_pinned_legacy_results(self):
        for group in (False, True):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as tmp:
                chat = _chat(group, text_size=350_000)
                module, knob = (gc, "GROUP_MAX_DIALOG_CHARS") if group else (dc, "MAX_DIALOG_CHARS")
                base_name = "GROUP_PROMPT_FINGERPRINT" if group else "PROMPT_FINGERPRINT"
                dimension = "group_dynamics" if group else "emotion"
                historical = "30c7d6356e3a" if group else "23fa36bc1d2a"
                make_prompt = gc._group_month_prompt if group else dc._month_prompt
                with mock.patch.object(module, knob, 600_000):
                    sampled = make_prompt(chat, "2025-01", chat.messages).cache_fingerprint
                    self.assertNotEqual(sampled, historical)
                legacy = dc.legacy_fingerprints_for_dimension(dimension)
                with mock.patch.object(store, "AI_CACHE_DIR", tmp):
                    for old in (historical, *legacy):
                        store._write_cache(dimension, "compat", {"old_sampled": True}, fingerprint=old)
                    with mock.patch.object(module, knob, 1_000_000):
                        current = dc.analysis_cache_fingerprint(dimension, chat)
                        self.assertNotEqual(current, historical)
                        self.assertEqual(
                            make_prompt(chat, "2025-01", chat.messages).cache_fingerprint, current
                        )
                        # Simulate startup under the new budget: the module base key also changes.
                        with mock.patch.object(module, base_name, current):
                            self.assertEqual(dc.legacy_fingerprints_for_dimension(dimension), ())
                            self.assertIsNone(store._read_cache(dimension, "compat", fingerprint=current))

    def test_custom_compatibility_does_not_hide_formatter_changes(self):
        original = inspect.getsource
        for group in (False, True):
            with self.subTest(group=group):
                module, knob = (gc, "GROUP_MAX_DIALOG_CHARS") if group else (dc, "MAX_DIALOG_CHARS")
                formatter = gc.build_group_dialog if group else dc._build_dialog_with_meta
                fingerprint = gc.group_prompt_fingerprint if group else dc._prompt_fingerprint

                def changed_source(func, formatter=formatter):
                    source = original(func)
                    return source.replace("统计：", "统计(修订)：") if func is formatter else source

                with mock.patch.object(module, knob, 10_000):
                    historical = fingerprint()
                    with mock.patch("inspect.getsource", side_effect=changed_source):
                        self.assertNotEqual(fingerprint(), historical)

    def test_sampling_fingerprints_track_all_rendering_helpers(self):
        """抽样路径依赖的截断/兜底函数变化必须切换缓存键。"""
        original = inspect.getsource

        def changed_source(func):
            source = original(func)
            if func is dc._truncate_dialog_line:
                return source.replace(
                    "return prefix + line[len(prefix) : max_chars]",
                    "return prefix + line[len(prefix) : max_chars - 1]",
                )
            if func is gc._trim_group_entries:
                return source.replace("used += len(line) + 1", "used += len(line) + 2")
            if func is gc._message_line:
                return source.replace(
                    'return f"{prefix} {text}".strip()',
                    'return f"V2 {prefix} {text}".strip()',
                )
            return source

        private_base = dc.sampled_prompt_fingerprint()
        with mock.patch("inspect.getsource", side_effect=changed_source):
            self.assertNotEqual(private_base, dc.sampled_prompt_fingerprint())

        group_base = gc.group_sampled_prompt_fingerprint()
        with mock.patch("inspect.getsource", side_effect=changed_source):
            self.assertNotEqual(group_base, gc.group_sampled_prompt_fingerprint())

        group_unsampled_base = gc.group_prompt_fingerprint()
        with mock.patch("inspect.getsource", side_effect=changed_source):
            self.assertNotEqual(group_unsampled_base, gc.group_prompt_fingerprint())

        def changed_gap_source(func):
            source = original(func)
            if func is dc._gap_mark:
                return source.replace('return f"(+{minutes}m)"', 'return f"(+{minutes}m)!"')
            return source

        with mock.patch("inspect.getsource", side_effect=changed_gap_source):
            self.assertNotEqual(private_base, dc.sampled_prompt_fingerprint())
            self.assertNotEqual(group_base, gc.group_sampled_prompt_fingerprint())
            self.assertNotEqual(group_unsampled_base, gc.group_prompt_fingerprint())

    def test_unsampled_group_cache_tracks_rendering_helper(self):
        """未抽样路径也经过重绘函数，改动它必须让旧维度缓存失效。"""
        original = inspect.getsource
        base = gc.group_prompt_fingerprint()

        def changed_source(func):
            source = original(func)
            if func is gc._render_group_entries:
                return source.replace("lines.append(line)", "lines.append('V2 ' + line)")
            return source

        with mock.patch("inspect.getsource", side_effect=changed_source):
            self.assertNotEqual(base, gc.group_prompt_fingerprint())

    def test_each_formatter_dependency_invalidates_cache_independently(self):
        # Mutate one helper at a time. Changing several helpers together can
        # mask a missing dependency because another changed helper switches keys.
        original = inspect.getsource
        cases = (
            (
                dc._truncate_dialog_line,
                "return prefix + line[len(prefix) : max_chars]",
                "return prefix + line[len(prefix) : max_chars - 1]",
                (dc.sampled_prompt_fingerprint, gc.group_sampled_prompt_fingerprint),
            ),
            (
                gc._trim_group_entries,
                "used += len(line) + 1",
                "used += len(line) + 2",
                (gc.group_sampled_prompt_fingerprint,),
            ),
            (
                gc._entry_line,
                "return entry[3]",
                "return entry[3] + 'V2'",
                (gc.group_sampled_prompt_fingerprint,),
            ),
            (
                dc._gap_mark,
                'return f"(+{minutes}m)"',
                'return f"(+{minutes}m)!"',
                (dc._unsampled_prompt_fingerprint, gc.group_prompt_fingerprint),
            ),
            (
                gc._short_time,
                "time_str[5:16]",
                "time_str[6:16]",
                (dc._unsampled_prompt_fingerprint, gc.group_prompt_fingerprint),
            ),
            (
                gc._fit_lines,
                "sampled = lines[::step]",
                "sampled = lines[::step + 1]",
                (gc.group_prompt_fingerprint,),
            ),
            (
                gc._render_group_entries,
                "lines.append(line)",
                "lines.append('V2 ' + line)",
                (gc.group_prompt_fingerprint,),
            ),
        )
        for helper, old, new, fingerprints in cases:
            source = original(helper)
            self.assertIn(old, source)
            changed = source.replace(old, new)
            for fingerprint in fingerprints:
                with self.subTest(helper=helper.__name__, fingerprint=fingerprint.__name__):
                    base = fingerprint()

                    def changed_source(func, helper=helper, changed=changed):
                        return changed if func is helper else original(func)

                    with mock.patch("inspect.getsource", side_effect=changed_source):
                        self.assertNotEqual(base, fingerprint())
