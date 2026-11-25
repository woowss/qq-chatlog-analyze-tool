# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Regression tests for Vision cache identity, LRU memoization, and face images."""

import hashlib
import json
import os
import shutil as _shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import io
import re
from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import face_images as fi  # noqa: E402
from analyzer import vision  # noqa: E402
from webapp import cleanup as cleanupmod  # noqa: E402
from webapp import store as storemod  # noqa: E402


class TestVisionMemoLRU(unittest.TestCase):
    """进程内摘要缓存：每条背后都是一次付费视觉调用，淘汰只能淘汰最久未用的"""

    def test_overflow_evicts_oldest_only(self):
        with mock.patch.object(vision, "_MEMO", {}), mock.patch.object(vision, "_MEMO_MAX", 3):
            for k in "abc":
                vision._memo_put(k, k.upper())
            self.assertEqual(sorted(vision._MEMO), ["a", "b", "c"])
            vision._memo_put("d", "D")
            self.assertEqual(sorted(vision._MEMO), ["b", "c", "d"], "只该淘汰最久未用的 a")
            self.assertEqual(vision._MEMO["b"], "B", "旧条目内容不能被清掉")

    def test_recently_read_key_survives_eviction(self):
        with mock.patch.object(vision, "_MEMO", {}), mock.patch.object(vision, "_MEMO_MAX", 3):
            for k in "abc":
                vision._memo_put(k, k.upper())
            self.assertEqual(vision._memo_get("a"), "A")  # a 变成最近使用
            vision._memo_put("d", "D")  # 该淘汰 b
            self.assertIn("a", vision._MEMO)
            self.assertNotIn("b", vision._MEMO)

    def test_miss_returns_none(self):
        with mock.patch.object(vision, "_MEMO", {}):
            self.assertIsNone(vision._memo_get("没有这个键"))

    def test_disk_hit_path_also_respects_bound(self):
        """命中磁盘缓存那条路径原本没做容量检查，条目数可以越过上限"""
        keys = iter(f"k{i}" for i in range(6))
        with (
            mock.patch.object(vision, "_MEMO", {}),
            mock.patch.object(vision, "_MEMO_MAX", 2),
            mock.patch.object(vision, "available", return_value=True),
            mock.patch.object(vision, "pick_images", return_value=[{"path": "x", "key": "fp"}]),
            mock.patch.object(vision, "_images_key", side_effect=lambda _imgs: next(keys)),
            mock.patch.object(vision, "_read_cache", return_value="磁盘里的摘要"),
        ):
            for _ in range(6):
                self.assertEqual(vision.digest([], chat_hash="h"), "磁盘里的摘要")
            self.assertLessEqual(len(vision._MEMO), 2, "走磁盘缓存也必须受 _MEMO_MAX 约束")

    def test_plain_dict_still_works(self):
        """现有用例用 mock.patch.object(vision, "_MEMO", {}) 注入普通 dict，别把它换掉"""
        with mock.patch.object(vision, "_MEMO", {}):
            vision._memo_put("k", "v")
            self.assertEqual(vision._memo_get("k"), "v")
            vision._MEMO.clear()  # 用例里也这么清
            self.assertIsNone(vision._memo_get("k"))


class VisionCacheIdentityTests(unittest.TestCase):
    def setUp(self):
        vision.purge_marks.clear()
        vision._MEMO.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.upload_root = os.path.join(self.temp.name, "uploads")
        self.cache_root = os.path.join(self.temp.name, "ai_cache")

    def tearDown(self):
        vision.purge_marks.clear()

    def _message(self, media_path="resources/images/shared.png", media_id=""):
        return SimpleNamespace(
            has_image=True,
            media_w=1024,
            media_h=1024,
            media_id=media_id,
            media_path=media_path,
            time_str="2026-10-11 12:00:00",
            sender_name="合成成员",
        )

    def _write_media(self, chat_hash, content, basename="shared.png"):
        directory = Path(self.upload_root) / "media" / chat_hash
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / basename
        path.write_bytes(content)
        return path

    def _vision_settings(self):
        return mock.patch.multiple(
            vision,
            AI_CACHE_DIR=self.cache_root,
            UPLOAD_FOLDER=self.upload_root,
            MEDIA_ROOT="",
            VISION_ENABLED=True,
            VISION_MAX_PER_MONTH=8,
            VISION_MAX_BYTES=1024 * 1024,
            VISION_MAX_TOTAL_BYTES=4 * 1024 * 1024,
            VISION_MIN_SIDE=1,
        )

    def _summary_from_bytes(self, _system, _user, images):
        image_bytes = Path(images[0]["path"]).read_bytes()
        return "summary-" + hashlib.sha256(image_bytes).hexdigest()[:12]

    def test_same_relative_path_with_different_chat_bytes_gets_distinct_summaries(self):
        bytes_a = b"synthetic image bytes for chat A"
        bytes_b = b"synthetic image bytes for chat B"
        self._write_media("chat-a", bytes_a)
        self._write_media("chat-b", bytes_b)
        message = self._message()

        with (
            self._vision_settings(),
            mock.patch("analyzer.deepseek_client._call_vision", side_effect=self._summary_from_bytes) as call,
            mock.patch.object(vision.purge_marks, "is_marked", return_value=False),
        ):
            first = vision.digest([message], chat_hash="chat-a")
            second = vision.digest([message], chat_hash="chat-b")

        self.assertNotEqual(first, second)
        self.assertEqual(call.call_count, 2)

    def test_process_memo_is_chat_scoped_even_for_identical_image_bytes(self):
        content = b"synthetic identical bytes in separate chats"
        self._write_media("chat-a", content)
        self._write_media("chat-b", content)
        message = self._message()

        with (
            self._vision_settings(),
            mock.patch("analyzer.deepseek_client._call_vision", return_value="chat-scoped-summary") as call,
            mock.patch.object(vision.purge_marks, "is_marked", return_value=False),
        ):
            first = vision.digest([message], chat_hash="chat-a")
            second = vision.digest([message], chat_hash="chat-b")

        self.assertEqual(first, second)
        self.assertEqual(call.call_count, 2)

    def test_replacing_same_chat_file_without_media_id_invalidates_memory_and_disk_keys(self):
        media = self._write_media("chat-a", b"synthetic original bytes")
        message = self._message()

        with (
            self._vision_settings(),
            mock.patch("analyzer.deepseek_client._call_vision", side_effect=self._summary_from_bytes) as call,
            mock.patch.object(vision.purge_marks, "is_marked", return_value=False),
        ):
            original = vision.digest([message], chat_hash="chat-a")
            media.write_bytes(b"synthetic replacement bytes")
            self.assertEqual(vision.preview_digest([message], chat_hash="chat-a"), ("", True))
            replacement = vision.digest([message], chat_hash="chat-a")

        self.assertNotEqual(original, replacement)
        self.assertEqual(call.call_count, 2)

    def test_untrusted_path_legacy_cache_is_not_migrated(self):
        self._write_media("chat-a", b"synthetic current file")
        message = self._message()
        with (
            self._vision_settings(),
            mock.patch("analyzer.deepseek_client._call_vision", return_value="new-content-summary") as call,
            mock.patch.object(vision.purge_marks, "is_marked", return_value=False),
        ):
            images = vision.pick_images([message], chat_hash="chat-a")
            self.assertFalse(images[0].get("trusted_md5", False))
            old_key = self._raw_key(images, dc.PROMPT_FINGERPRINT_LEGACY)
            old_path = vision._cache_path("chat-a", old_key)
            vision._write_cache(old_path, "stale-path-summary")
            result = vision.digest([message], chat_hash="chat-a")

        self.assertEqual(result, "new-content-summary")
        self.assertEqual(call.call_count, 1)
        self.assertTrue(os.path.exists(old_path))

    def test_verified_md5_migrates_both_historical_fingerprint_keys(self):
        content = b"synthetic image with matching exported md5"
        md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self._write_media("chat-a", content)
        message = self._message(media_id=md5)

        with (
            self._vision_settings(),
            mock.patch("analyzer.deepseek_client._call_vision", return_value="unexpected-model-call") as call,
            mock.patch.object(vision.purge_marks, "is_marked", return_value=False),
        ):
            images = vision.pick_images([message], chat_hash="chat-a")
            self.assertTrue(images[0]["trusted_md5"])
            for fingerprint in (dc.PROMPT_FINGERPRINT, dc.PROMPT_FINGERPRINT_LEGACY):
                with self.subTest(fingerprint=fingerprint):
                    vision._MEMO.clear()
                    old_key = self._raw_key(images, fingerprint)
                    old_path = vision._cache_path("chat-a", old_key)
                    vision._write_cache(old_path, "safe-old-md5-summary")
                    current_key = vision._images_key(images)
                    current_path = vision._cache_path("chat-a", current_key)
                    result = vision.digest([message], chat_hash="chat-a")

                    self.assertEqual(result, "safe-old-md5-summary")
                    self.assertTrue(os.path.exists(current_path))
                    self.assertFalse(os.path.exists(old_path))
                    Path(current_path).unlink()
            self.assertEqual(call.call_count, 0)

    def test_legacy_cache_read_during_purge_does_not_migrate_or_repopulate_memo(self):
        content = b"synthetic image with matching exported md5"
        md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self._write_media("chat-a", content)
        message = self._message(media_id=md5)

        with (
            self._vision_settings(),
            mock.patch("analyzer.deepseek_client._call_vision", return_value="unexpected-model-call") as call,
        ):
            images = vision.pick_images([message], chat_hash="chat-a")
            old_key = self._raw_key(images, dc.PROMPT_FINGERPRINT_LEGACY)
            old_path = vision._cache_path("chat-a", old_key)
            current_path = vision._cache_path("chat-a", vision._images_key(images))
            vision._write_cache(old_path, "safe-old-md5-summary")
            read_cache = vision._read_cache

            def read_then_mark(path, *, touch=True):
                result = read_cache(path, touch=touch)
                if path == old_path and result is not None:
                    vision.purge_marks.mark("chat-a")
                return result

            with (
                mock.patch.object(vision, "_read_cache", side_effect=read_then_mark),
                mock.patch.object(vision.os, "replace") as replace,
            ):
                result = vision.digest([message], chat_hash="chat-a")

            memo_key = vision._memo_key("chat-a", vision._images_key(images))
            self.assertEqual(result, "safe-old-md5-summary")
            self.assertTrue(os.path.exists(old_path))
            self.assertFalse(os.path.exists(current_path))
            self.assertNotIn(memo_key, vision._MEMO)
            replace.assert_not_called()
            call.assert_not_called()

    def test_trusted_md5_summary_reuses_within_chat_across_dimensions(self):
        content = b"synthetic stable image"
        md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self._write_media("chat-a", content)
        message = self._message(media_id=md5)

        with (
            self._vision_settings(),
            mock.patch("analyzer.deepseek_client._call_vision", return_value="one-summary") as call,
            mock.patch.object(vision.purge_marks, "is_marked", return_value=False),
        ):
            first = vision.digest([message], chat_hash="chat-a", label="合成月份")
            second = vision.digest([message], chat_hash="chat-a", label="合成人物维度")

        self.assertEqual(first, second)
        self.assertEqual(call.call_count, 1)

    def test_purge_memo_only_removes_one_chat_namespace(self):
        with self._vision_settings(), mock.patch.object(vision, "_MEMO", {}):
            vision._memo_put(vision._memo_key("chat-a", "key-a"), "summary-a")
            vision._memo_put(vision._memo_key("chat-b", "key-b"), "summary-b")
            removed = vision.purge_memo("chat-a")

            self.assertEqual(removed, 1)
            self.assertIsNone(vision._memo_get(vision._memo_key("chat-a", "key-a")))
            self.assertEqual(vision._memo_get(vision._memo_key("chat-b", "key-b")), "summary-b")

    def test_disk_hit_does_not_repopulate_memo_after_purge_mark(self):
        image = {
            "path": "synthetic.png",
            "key": "0123456789abcdef0123456789abcdef",
            "trusted_md5": True,
            "content_sha256": "a" * 64,
            "size": 1,
            "mime": "image/png",
        }
        with (
            self._vision_settings(),
            mock.patch.object(vision, "available", return_value=True),
            mock.patch.object(vision, "pick_images", return_value=[image]),
            mock.patch.object(vision, "_MEMO", {}),
            mock.patch.object(
                vision, "_read_cache", side_effect=lambda *_a, **_kw: self._mark_and_return("chat-a")
            ),
        ):
            result = vision.digest([], chat_hash="chat-a")
            memo_key = vision._memo_key("chat-a", vision._images_key([image]))

            self.assertEqual(result, "disk-summary-after-purge")
            self.assertNotIn(memo_key, vision._MEMO)

    def test_preview_reads_memo_without_changing_lru_order(self):
        image = {
            "path": "synthetic.png",
            "key": "0123456789abcdef0123456789abcdef",
            "trusted_md5": True,
            "content_sha256": "a" * 64,
            "size": 1,
            "mime": "image/png",
        }
        memo_key = vision._memo_key("chat-a", vision._images_key([image]))
        with (
            self._vision_settings(),
            mock.patch.object(vision, "available", return_value=True),
            mock.patch.object(vision, "pick_images", return_value=[image]),
            mock.patch.object(vision, "_MEMO", {"oldest": "a", memo_key: "cached-summary", "newest": "b"}),
        ):
            before = list(vision._MEMO)
            result = vision.preview_digest([], chat_hash="chat-a")

            self.assertEqual(result, ("cached-summary", False))
            self.assertEqual(list(vision._MEMO), before)

    def _mark_and_return(self, chat_hash):
        vision.purge_marks.mark(chat_hash)
        return "disk-summary-after-purge"

    @staticmethod
    def _raw_key(images, prompt_fingerprint):
        digest = hashlib.sha256()
        for part in (dc.DEEPSEEK_MODEL, prompt_fingerprint, vision.VISION_SYSTEM, vision.VISION_DETAIL):
            digest.update(str(part).encode("utf-8"))
            digest.update(b"\x00")
        for image in images:
            digest.update(str(image.get("legacy_key", image["key"])).encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()[:20]


class TestMediaCopyLifecycle(unittest.TestCase):
    """图片副本是盘上最敏感的一批派生数据：既要有 24 小时回收，也要随聊天级联删除"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-media-lifecycle-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)

    def _patched_dirs(self):
        """把清理涉及的目录全部指向临时目录，避免碰到真实数据"""
        return [
            mock.patch.object(cleanupmod, "UPLOAD_FOLDER", self.tmp),
            mock.patch.object(cleanupmod, "SESSION_FILE_DIR", os.path.join(self.tmp, "session")),
            mock.patch.object(cleanupmod, "AI_CACHE_DIR", os.path.join(self.tmp, "ai")),
            mock.patch.object(cleanupmod, "STATS_CACHE_DIR", os.path.join(self.tmp, "stats")),
            mock.patch.object(cleanupmod, "LOG_DIR", os.path.join(self.tmp, "logs")),
            mock.patch.object(vision, "UPLOAD_FOLDER", self.tmp),
        ]

    def test_expired_media_copy_is_reclaimed_recursively(self):
        """uploads/media/<哈希>/ 里的图片副本必须被回收，空目录也要清掉。

        原先 _purge_dir 只看目录下的普通文件（os.path.isfile），整个 media 子树
        被跳过——图片副本永远留在盘上，与 README 的 24 小时承诺不符。
        """
        media_dir = os.path.join(self.tmp, "media", "abc123")
        os.makedirs(media_dir, exist_ok=True)
        old_file = os.path.join(media_dir, "old.jpg")
        new_file = os.path.join(media_dir, "new.jpg")
        for path in (old_file, new_file):
            with open(path, "wb") as f:
                f.write(b"\xff\xd8\xff" + b"x" * 16)
        ancient = time.time() - 48 * 3600
        os.utime(old_file, (ancient, ancient))

        for patcher in self._patched_dirs():
            patcher.start()
            self.addCleanup(patcher.stop)
        removed = cleanupmod.cleanup_old_files()

        self.assertGreaterEqual(removed, 1)
        self.assertFalse(os.path.exists(old_file), "过期图片副本应被删除")
        self.assertTrue(os.path.exists(new_file), "未过期的副本不该被误删")

        # 再跑一次：这次全部过期 → 文件与空目录一起清掉，不留空壳目录
        os.utime(new_file, (ancient, ancient))
        cleanupmod.cleanup_old_files()
        self.assertFalse(os.path.isdir(media_dir), "回收干净后应删掉空的哈希目录")
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "media")), "空的 media 目录也不该留着")

    def test_purge_chat_caches_removes_media_copy(self):
        """删聊天（换文件/清理）时必须连带删掉图片副本目录"""
        chat_hash = "deadbeefcafe0001"
        media_dir = os.path.join(self.tmp, "media", chat_hash)
        os.makedirs(media_dir, exist_ok=True)
        for name in ("a.jpg", "b.png"):
            with open(os.path.join(media_dir, name), "wb") as f:
                f.write(b"x" * 8)

        with mock.patch.object(vision, "UPLOAD_FOLDER", self.tmp):
            removed = storemod._purge_chat_caches(chat_hash)

        self.assertGreaterEqual(removed, 2, "回收计数应包含图片副本")
        self.assertFalse(os.path.exists(media_dir), "图片副本目录应被整目录删除")

    def test_purge_session_media_is_idempotent(self):
        """没有副本目录时不该报错（旧聊天、没开图片理解都要走这条路径）"""
        with mock.patch.object(vision, "UPLOAD_FOLDER", self.tmp):
            self.assertEqual(vision.purge_session_media("nonexistent"), 0)


class TestVisionCallIsNonThinking(unittest.TestCase):
    """摘要的 max_tokens 只有 512：开思考模式必然被思维链吃满并截断"""

    def test_call_vision_sends_thinking_disabled(self):
        captured = {}

        class _FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                msg = mock.Mock()
                msg.content = "- 一张图"
                choice = mock.Mock()
                choice.message = msg
                choice.finish_reason = "stop"
                resp = mock.Mock()
                resp.choices = [choice]
                resp.usage = None
                return resp

        fake_client = mock.Mock()
        fake_client.chat.completions = _FakeCompletions()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
            img_path = f.name
        self.addCleanup(os.remove, img_path)

        with (
            mock.patch.object(dc, "_get_client", return_value=fake_client),
            mock.patch.object(dc, "_SEND_THINKING_PARAM", True),
            mock.patch.object(dc, "THINKING_DEFAULT", True),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
        ):
            text = dc._call_vision("sys", "看图", [{"path": img_path, "mime": "image/png", "key": "k1"}])

        self.assertEqual(text, "- 一张图")
        self.assertEqual(
            captured.get("extra_body"), {"thinking": {"type": "disabled"}}, "图片摘要必须显式关闭思考模式"
        )
        self.assertEqual(captured.get("max_tokens"), 512)


class TestFaceFetchBudget(unittest.TestCase):
    """表情图抓取是同步请求：必须有单次时长预算，抓不完就下次继续"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-face-budget-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)

    def test_budget_stops_network_loop(self):
        faces = {
            "可怜": {"key": fi.key_for("可怜"), "market_url": None},
            "流泪": {"key": fi.key_for("流泪"), "market_url": None},
        }
        calls = []
        with (
            mock.patch.object(fi, "FACE_IMAGES_ENABLED", True),
            mock.patch.object(fi, "FACE_CACHE_DIR", self.tmp),
            mock.patch.object(fi, "_download", side_effect=lambda url: calls.append(url)),
        ):
            # 极小预算：第一轮就该判定超时并收工
            fi.ensure(faces, allow_network=True, max_seconds=1e-9)
        self.assertEqual(calls, [], "预算用尽后不应再发起下载")

    def test_no_budget_means_no_limit(self):
        faces = {"可怜": {"key": fi.key_for("可怜"), "market_url": None}}
        calls = []
        with (
            mock.patch.object(fi, "FACE_IMAGES_ENABLED", True),
            mock.patch.object(fi, "FACE_CACHE_DIR", self.tmp),
            mock.patch.object(fi, "_download", side_effect=lambda url: calls.append(url)),
        ):
            fi.ensure(faces, allow_network=True)  # 默认 0 = 不限时长
        self.assertEqual(len(calls), 1, "默认行为不变：没有预算就一直抓到上限/抓完")


class TestVisionCacheHasCreatedStamp(unittest.TestCase):
    """含图片描述的缓存必须有创建时间，否则绝对 90 天上限对它永远不生效"""

    def test_write_includes_created_and_reads_legacy(self):
        path = os.path.join(os.environ["QQCHAT_DATA_DIR"], "vision_probe.json")
        try:
            vision._write_cache(path, "- 一张截图")
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.assertIsInstance(payload.get("_created"), float, "要写 _created 供硬上限回收")
            self.assertEqual(payload.get("digest"), "- 一张截图")
            self.assertEqual(vision._read_cache(path), "- 一张截图")
            self.assertGreater(
                cleanupmod._cache_created_at(path), time.time() - 60, "清理任务能读到刚写入的创建时间"
            )

            # 旧格式（没有 _created）仍要能读，避免升级后缓存集体失效、重复调用视觉模型
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"digest": "- 旧格式"}, f)
            self.assertEqual(vision._read_cache(path), "- 旧格式")
        finally:
            if os.path.exists(path):
                os.remove(path)


ROOT = Path(__file__).resolve().parent.parent


def _fix_wrap(msgs, senders, self_uid="uA"):
    """按导出格式包装（msgs 为完整消息字典，便于构造 system/type_23 等特殊条目）"""
    return json.dumps(
        {
            "chatInfo": {
                "name": senders.get("uB", "对方"),
                "selfUid": self_uid,
                "selfName": senders.get(self_uid, "我"),
            },
            "statistics": {
                "senders": [{"uid": uid, "name": name} for uid, name in senders.items()],
                "totalMessages": len(msgs),
            },
            "messages": msgs,
        },
        ensure_ascii=False,
    ).encode("utf-8")


class TestVisionDigest(unittest.TestCase):
    """图片理解：挑图规则、摘要缓存（同一批图只花一次视觉调用）、注入 prompt"""

    @staticmethod
    def _png(w, h, rgb):
        import struct
        import zlib

        raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

        def chunk(tag, data):
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    def _media_dir(self, tmp, specs):
        """specs: [(相对路径, 宽, 高, md5)] → 建立假图片文件并返回消息列表"""
        from parser.qq_parser import Message

        msgs = []
        for i, (rel, w, h, md5) in enumerate(specs):
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(self._png(max(w, 1), max(h, 1), (10, 20, 30)))
            msgs.append(
                Message(
                    id=str(i),
                    timestamp=1704067200000 + i * 1000,
                    time_str="2024-01-01 08:00:00",
                    sender_name="我",
                    sender_uid="uA",
                    text="",
                    raw_text="",
                    msg_type="type_3",
                    has_image=True,
                    is_reply=False,
                    media_path=rel,
                    media_w=w,
                    media_h=h,
                    media_id=md5 or "",
                )
            )
        return msgs

    def test_disabled_without_media_root(self):
        from analyzer import vision

        with mock.patch.object(vision, "MEDIA_ROOT", ""):
            self.assertFalse(vision.available())
            with mock.patch("analyzer.deepseek_client._call_vision") as call:
                self.assertEqual(vision.digest([], chat_hash="h"), "")
                self.assertFalse(call.called, "没配媒体目录时不该发起任何视觉调用")

    def test_pick_images_dedupes_small_and_missing(self):
        from analyzer import vision

        with tempfile.TemporaryDirectory() as tmp:
            msgs = self._media_dir(
                tmp,
                [
                    ("resources/images/a.png", 1080, 2400, "AAA"),
                    ("resources/images/a2.png", 1080, 2400, "AAA"),  # 同一张图（md5 相同）
                    ("resources/images/tiny.png", 64, 64, "TINY"),  # 表情包尺寸，跳过
                    ("resources/images/b.png", 1200, 900, "BBB"),
                ],
            )
            msgs.append(
                msgs[0].__class__(
                    id="9",
                    timestamp=1,
                    time_str="",
                    sender_name="我",
                    sender_uid="uA",
                    text="",
                    raw_text="",
                    msg_type="type_3",
                    has_image=True,
                    is_reply=False,
                    media_path="resources/images/missing.png",
                    media_w=1000,
                    media_h=1000,
                    media_id="MISSING",
                )
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", tmp),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
                mock.patch.object(vision, "VISION_MAX_BYTES", 10**7),
            ):
                picked = vision.pick_images(msgs, limit=8)
            self.assertEqual([p["key"] for p in picked], ["AAA", "BBB"])

    def test_digest_cached_across_dimensions_and_runs(self):
        from analyzer import vision

        with tempfile.TemporaryDirectory() as tmp:
            msgs = self._media_dir(
                tmp,
                [
                    ("resources/images/a.png", 1080, 2400, "AAA"),
                    ("resources/images/b.png", 1200, 900, "BBB"),
                ],
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", tmp),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
                mock.patch.object(vision, "_MEMO", {}),
                # 磁盘缓存也必须落在本次用例的临时目录里：缓存键只由 chat_hash + 图片指纹决定，
                # 用真实 ai_cache/ 的话，同一个数据目录跑第二遍时缓存已被上一遍写进去，
                # 第一次断言 call_count == 1 就会变成 0（状态相关的假失败）。
                mock.patch.object(vision, "AI_CACHE_DIR", os.path.join(tmp, "ai_cache")),
                mock.patch(
                    "analyzer.deepseek_client._call_vision",
                    return_value="- 截图：在讨论选课\n- 照片：路边的小猫",
                ) as call,
            ):
                first = vision.digest(msgs, chat_hash="hashV", label="2026-08 月")
                second = vision.digest(msgs, chat_hash="hashV", label="2026-08 月")
                self.assertIn("小猫", first)
                self.assertEqual(first, second)
                self.assertEqual(call.call_count, 1, "同一批图只该花一次视觉调用")
                # 模拟进程重启：内存缓存清空，磁盘缓存仍应命中
                vision._MEMO.clear()
                third = vision.digest(msgs, chat_hash="hashV", label="2026-08 月")
                self.assertEqual(third, first)
                self.assertEqual(call.call_count, 1, "磁盘缓存应跨进程复用")

    def test_digest_injected_into_dialog(self):
        import analyzer.deepseek_client as dc
        from analyzer import vision

        with tempfile.TemporaryDirectory() as tmp:
            msgs = self._media_dir(tmp, [("resources/images/a.png", 1080, 2400, "AAA")])
            text_msg = msgs[0].__class__(
                id="x",
                timestamp=1704067200000,
                time_str="2024-01-01 08:00:00",
                sender_name="我",
                sender_uid="uA",
                text="在吗",
                raw_text="在吗",
                msg_type="type_1",
                has_image=False,
                is_reply=False,
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", tmp),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
                mock.patch.object(vision, "_MEMO", {}),
                mock.patch("analyzer.deepseek_client._call_vision", return_value="- 截图：课程表"),
            ):
                dialog = dc._build_dialog(
                    [text_msg, msgs[0]], "uA", "我", "对方", chat_hash="h", vision_label="2026-08 月"
                )
        self.assertIn("图片内容摘要", dialog)
        self.assertIn("课程表", dialog)

    def test_vision_failure_does_not_break_text_analysis(self):
        """图片理解出任何非致命问题，都必须退回纯文本分析而不是让分析失败"""
        import analyzer.deepseek_client as dc
        from analyzer import vision

        with mock.patch.object(vision, "digest", side_effect=RuntimeError("视觉挂了")):
            self.assertEqual(dc._vision_digest([], "h", "标签"), "")
        with mock.patch.object(vision, "digest", side_effect=dc.QuotaExhaustedError("额度")):
            with self.assertRaises(dc.QuotaExhaustedError):
                dc._vision_digest([], "h", "标签")


class TestWebUIMediaUpload(unittest.TestCase):
    """WebUI 两阶段上传：先传 JSON → 服务端告知要哪些图 → 浏览器只传这些图"""

    def setUp(self):
        import app as appmod
        from analyzer import vision

        self.appmod = appmod
        self.vision = vision
        self.client = appmod.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as s:
            self.token = s["csrf_token"]
        self.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": self.token}
        self._dir = tempfile.mkdtemp(prefix="qqchatlog-media-")
        # 用真实的图片元素构造导出：两条图片消息 + 一条文本
        self.payload = _fix_wrap(
            [
                {
                    "id": "1",
                    "timestamp": 1704067200000,
                    "time": "2024-01-01T00:00:00.000Z",
                    "sender": {"uid": "uA", "name": "我"},
                    "type": "type_3",
                    "content": {
                        "text": "",
                        "elements": [
                            {
                                "type": "image",
                                "data": {
                                    "filename": "pic.jpg",
                                    "md5": "AAA",
                                    "size": "2048",
                                    "width": "1080",
                                    "height": "2400",
                                    "url": "resources/images/aaa_pic.jpg",
                                },
                            }
                        ],
                    },
                },
                {
                    "id": "2",
                    "timestamp": 1704067260000,
                    "time": "2024-01-01T00:01:00.000Z",
                    "sender": {"uid": "uB", "name": "对方"},
                    "type": "type_1",
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )

    def tearDown(self):
        import shutil

        with self.client.session_transaction() as s:
            chat_hash, path = s.get("chat_hash"), s.get("filepath")
        if chat_hash:
            storemod._purge_chat_caches(chat_hash)
            shutil.rmtree(self.vision.session_media_dir(chat_hash), ignore_errors=True)
        if path and os.path.exists(path):
            os.remove(path)
        shutil.rmtree(self._dir, ignore_errors=True)

    def _upload_json(self):
        r = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(self.payload), "c.json")},
            headers={**self.headers, "X-Requested-With": "fetch"},
        )
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _png(self, w=1080, h=2400):
        return TestVisionDigest._png(w, h, (200, 100, 50))

    def test_ajax_upload_returns_wanted_media(self):
        body = self._upload_json()
        self.assertTrue(body["ok"])
        self.assertIn(
            "aaa_pic.jpg", [os.path.basename(p) for p in body["wanted_media"]], "应告知前端需要哪张图"
        )
        self.assertEqual(body["next"], "/dashboard")

    def test_plain_form_upload_still_redirects(self):
        """没有 JS/没选目录时，普通表单提交必须照旧 302 跳转"""
        r = self.client.post(
            "/upload", data={"file": (io.BytesIO(self.payload), "c.json")}, headers=self.headers
        )
        self.assertEqual(r.status_code, 302)

    def test_media_endpoint_stores_only_images_and_blocks_traversal(self):
        self._upload_json()
        data = {
            "files": [
                (io.BytesIO(self._png()), "aaa_pic.jpg"),
                (io.BytesIO(b"not an image"), "evil.exe"),  # 扩展名白名单拦下
                (io.BytesIO(self._png()), "../../../evil.jpg"),  # 路径穿越：只取 basename
            ],
        }
        r = self.client.post(
            "/api/media", data=data, headers=self.headers, content_type="multipart/form-data"
        )
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["saved"], 2)
        self.assertEqual(body["skipped"], 1)
        with self.client.session_transaction() as s:
            chat_hash = s["chat_hash"]
        media_dir = self.vision.session_media_dir(chat_hash)
        self.assertEqual(sorted(os.listdir(media_dir)), ["aaa_pic.jpg", "evil.jpg"])
        # 越界文件绝不能写到 uploads/ 之外
        self.assertFalse(
            os.path.exists(os.path.abspath(os.path.join(media_dir, "..", "..", "..", "evil.jpg")))
        )

    def test_digest_uses_uploaded_copy_without_media_root(self):
        """没配 QQCHAT_MEDIA_DIR 也应该能用（图片来自 WebUI 上传的副本）"""
        self._upload_json()
        self.client.post(
            "/api/media",
            data={"files": [(io.BytesIO(self._png()), "aaa_pic.jpg")]},
            headers=self.headers,
            content_type="multipart/form-data",
        )
        from parser.qq_parser import load_chat

        with self.client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        chat = load_chat(path)
        with (
            mock.patch.object(self.vision, "MEDIA_ROOT", ""),
            mock.patch.object(self.vision, "VISION_MIN_SIDE", 100),
            mock.patch.object(self.vision, "_MEMO", {}),
            mock.patch("analyzer.deepseek_client._call_vision", return_value="- 截图：宠物医院候诊") as call,
        ):
            text = self.vision.digest(chat.messages, chat_hash=chat_hash, label="2024-01 月")
        self.assertIn("宠物医院", text)
        self.assertEqual(call.call_count, 1)

    def test_media_endpoint_rejects_without_session(self):
        fresh = self.appmod.app.test_client()
        fresh.get("/")
        with fresh.session_transaction() as s:
            tok = s["csrf_token"]
        r = fresh.post(
            "/api/media",
            data={"files": [(io.BytesIO(self._png()), "x.jpg")]},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": tok},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 400)


class TestFaceEmoji(unittest.TestCase):
    """表情排行用 emoji 渲染：有对应就画表情，没有就回退名字（不猜）"""

    def test_known_faces_map_to_emoji(self):
        from analyzer.face_emoji import emoji_for

        self.assertEqual(emoji_for("/可怜"), "🥺")
        self.assertEqual(emoji_for("/流泪"), "😢")
        self.assertEqual(emoji_for("/doge"), "🐶")
        self.assertEqual(emoji_for("微笑"), "🙂")  # 不带斜杠也能认

    def test_qq_only_faces_fall_back_to_empty(self):
        """QQ 专属超级表情/商城表情没有 Unicode 对应，必须返回空串而不是硬凑"""
        from analyzer.face_emoji import emoji_for

        for name in ("/吃糖", "/大怨种", "/菜汪", "/宕机", "/偷感", "[[叉腰]]", "[13]"):
            self.assertEqual(emoji_for(name), "", f"{name} 不该被硬映射成某个 emoji")

    def test_emoji_map_only_keeps_mapped(self):
        from analyzer.face_emoji import emoji_map

        m = emoji_map(["/可怜", "/吃糖", "/流泪"])
        self.assertEqual(m, {"/可怜": "🥺", "/流泪": "😢"})

    def test_object_emoji_are_not_used_for_gestures(self):
        """曾经选错的几个：表情排行里画出来是"另一个东西"，而不是那个表情

        - 擦汗 画成了 🧻（卷纸）——物体，和擦汗无关；
        - 抠鼻 画成了 🤏（捏手指）——手势是反的；
        - 勾引 画成了 🫦（咬唇）——换了个动作；
        - 鄙视 与"撇嘴"共用 😒——两个表情在排行里长得一模一样，失去区分度。

        这几条都是"看着像 bug"的观感问题，不会让任何用例变红，所以只能靠断言钉住。
        """
        from analyzer.face_emoji import FACE_EMOJI, emoji_for

        self.assertEqual(emoji_for("/擦汗"), "💦")
        self.assertEqual(emoji_for("/抠鼻"), "👃")
        self.assertEqual(emoji_for("/勾引"), "😏")
        self.assertEqual(emoji_for("/鄙视"), "😠")
        self.assertNotEqual(FACE_EMOJI["鄙视"], FACE_EMOJI["撇嘴"], "鄙视 与 撇嘴 不该共用同一个 emoji")

    def test_no_mapping_points_at_a_non_emoji(self):
        """表里的每一个值都必须真的是 emoji（挡住"手滑贴进一个普通字符"）

        **不能用 `unicodedata.name()` 当唯一判据**：它查的是当前解释器自带的 Unicode
        版本。Python 3.10 还是 Unicode 13.0，而 🥹(U+1F979)、🫶(U+1FAF6) 是 14.0 才
        收录的，于是"这个 emoji 比解释器新"会被判成"这不是 emoji"——CI 的 3.10 就是
        这么红的（3.12+ 自带 Unicode 14/15，同一份代码全绿）。

        判据改成两条与 UCD 版本无关的条件：① 字符类别不是文字/数字/标点/分隔符；
        ② 有 Unicode 名字，或落在补充符号平面（U+1F000 以上，emoji 都在这一带）。
        这样"字母、数字、全角标点、空格"照旧判红，而"解释器还不认识的 emoji"不再误伤。
        """
        import unicodedata

        from analyzer.face_emoji import FACE_EMOJI

        for name, char in FACE_EMOJI.items():
            with self.subTest(face=name):
                self.assertTrue(char, f"{name} 映射到了空串")
                # 变体选择符 / ZWJ 是组合用码位，不是独立图形
                for ch in char:
                    if ch in ("\ufe0f", "\u200d"):
                        continue
                    cp = ord(ch)
                    cat = unicodedata.category(ch)
                    self.assertNotIn(
                        cat[0],
                        "LNPDZ",
                        f"{name} -> {char!r} U+{cp:04X} 类别 {cat}，不是图形字符",
                    )
                    self.assertTrue(
                        unicodedata.name(ch, None) is not None or cp >= 0x1F000,
                        f"{name} -> {char!r} 含非法码位 U+{cp:04X}"
                        "（既没有 Unicode 名字，也不在 U+1F000 以上的 emoji 平面）",
                    )

    def test_habits_page_embeds_emoji_map(self):
        """页面里真的把 emoji 传给了前端渲染"""
        import app as appmod

        payload = _fix_wrap(
            [
                {
                    "id": "1",
                    "timestamp": 1704067200000,
                    "time": "2024-01-01 08:00:00",
                    "sender": {"uid": "uA", "name": "我"},
                    "content": {
                        "text": "",
                        "elements": [{"type": "face", "data": {"id": "111", "name": "/可怜"}}],
                    },
                },
                {
                    "id": "2",
                    "timestamp": 1704067210000,
                    "time": "2024-01-01 08:00:10",
                    "sender": {"uid": "uB", "name": "对方"},
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            token = s["csrf_token"]
        client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )
        with client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        try:
            storemod.wait_for_stats(chat_hash, timeout=20)
            body = client.get("/habits").get_data(as_text=True)
            # tojson 会把 emoji 转成 \ud83e\udd7a 转义，这里还原后断言真实字符。
            # 现在是 renderFaceBarChart(dom, 排行数组, 名字, emoji 映射, 表情图映射)
            m = re.search(
                r"renderFaceBarChart\('selfFaceChart',\s*\[.*?\],\s*\"[^\"]*\",\s*"
                r"(\{.*?\}),\s*\{.*?\}\);",
                body,
            )
            self.assertIsNotNone(m, "习惯页应调用 renderFaceBarChart 并传入 emoji 映射")
            mapping = json.loads(m.group(1))
            self.assertEqual(mapping.get("/可怜"), "🥺")
            report = client.get("/report").get_data(as_text=True)
            self.assertIn("🥺", report, "报告页也应显示 emoji")
        finally:
            storemod._purge_chat_caches(chat_hash)
            if path and os.path.exists(path):
                os.remove(path)

    def test_face_ranking_keeps_count_order(self):
        """表情排行必须按次数排：Flask 的 tojson 默认 sort_keys=True，
        直接传 dict 会被按汉字码点重排，"排行"名不副实（真实踩到过）。"""
        import app as appmod

        payload = _fix_wrap(
            [
                {
                    "id": str(i),
                    "timestamp": 1704067200000 + i * 1000,
                    "time": "2024-01-01 08:00:00",
                    "sender": {"uid": "uA", "name": "我"},
                    "content": {
                        "text": "",
                        "elements": [{"type": "face", "data": {"id": "5", "name": face}}],
                    },
                }
                # 次数：/流泪 3 次 > /可怜 2 次 > /微笑 1 次，
                # 但按编码排序会变成 可怜 < 微笑 < 流泪，正好能验出问题
                for i, face in enumerate(["/流泪", "/流泪", "/流泪", "/可怜", "/可怜", "/微笑"])
            ]
            + [
                {
                    "id": "x",
                    "timestamp": 1704067260000,
                    "time": "2024-01-01 08:01:00",
                    "sender": {"uid": "uB", "name": "对方"},
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            token = s["csrf_token"]
        client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )
        with client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        try:
            storemod.wait_for_stats(chat_hash, timeout=20)
            body = client.get("/habits").get_data(as_text=True)
            m = re.search(r"renderFaceBarChart\('selfFaceChart',\s*(\[\[.*?\]\]),", body)
            self.assertIsNotNone(m, "表情排行应按二维数组传参（保序）")
            ranking = json.loads(m.group(1))
            self.assertEqual([r[0] for r in ranking][:3], ["/流泪", "/可怜", "/微笑"])
            self.assertEqual([r[1] for r in ranking][:3], [3, 2, 1], "必须按次数降序")
        finally:
            storemod._purge_chat_caches(chat_hash)
            if path and os.path.exists(path):
                os.remove(path)


class TestFaceImagesOptional(unittest.TestCase):
    """原始表情图是可选功能：默认关、联网失败必须优雅降级"""

    def setUp(self):
        from analyzer import face_images

        self.fi = face_images
        self._tmp = tempfile.mkdtemp(prefix="qqchatlog-faces-")
        self._patches = [
            mock.patch.object(face_images, "FACE_CACHE_DIR", self._tmp),
            mock.patch.object(face_images, "FACE_IMAGES_ENABLED", True),
            mock.patch.object(face_images, "_COLLECT_MEMO", {}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        import shutil

        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_disabled_by_default(self):
        import config

        self.assertFalse(config.FACE_IMAGES_ENABLED, "默认必须是关的（可选项）")
        with mock.patch.object(self.fi, "FACE_IMAGES_ENABLED", False):
            self.assertFalse(self.fi.enabled())
            self.assertEqual(self.fi.ensure({"/流泪": {"key": "c5"}}), {})

    def test_url_resolved_by_name_not_by_export_id(self):
        """按名字取图：同一张表情在不同导出里的 id 可能不同，名字才与显示一致"""
        self.assertEqual(self.fi.url_for("/可怜"), "https://qzonestyle.gtimg.cn/qzone/em/e153.gif")
        self.assertEqual(self.fi.url_for("流泪"), "https://qzonestyle.gtimg.cn/qzone/em/e105.gif")
        # 超级表情：既不在经典表里、也没有商城地址 → 绝不猜地址
        self.assertIsNone(self.fi.url_for("/吃糖"))
        self.assertIsNone(self.fi.url_for("/大怨种"))

    def test_market_face_uses_its_own_url(self):
        url = "https://gxh.vip.qq.com/club/item/parcel/item/3b/abc/raw300.gif"
        self.assertEqual(self.fi.url_for("[13]", url), url)
        self.assertNotEqual(self.fi.key_for("[13]", url), self.fi.key_for("[13]"))

    def test_foreign_host_is_refused(self):
        """只有已知表情 CDN 允许访问，避免这段代码被当成任意下载器"""
        self.assertIsNone(self.fi.url_for("x", "https://evil.example.com/a.gif"))
        with (
            mock.patch.object(self.fi, "urlopen", create=True) as _u,
            mock.patch("urllib.request.urlopen") as real,
        ):
            self.assertIsNone(self.fi._download("https://evil.example.com/a.gif"))
            self.assertFalse(real.called)

    def test_classic_wins_over_message_level_market_url(self):
        """一条消息混发"经典+商城"时，经典表情不许被那张贴纸的地址污染。

        `face_url` 是**消息级**字段（解析层把 face 与 market_face 两类名字塞进同一个
        face_names，只留一个地址）。原先 key_for/url_for 都先看地址，于是同一条消息里
        每个名字都拿到那张贴石的键与图："微笑""可怜"明明各自有跨导出稳定的
        c0/c53，却被并成同一个 m…，表情榜上三个不同表情显示同一张图，
        而且哪条消息先出现决定结果。这正是本模块改成"按名取图"时要修掉的
        张冠李戴，只是当时只修了"名字对不上不取图"那一半。
        """
        sticker = "https://gxh.vip.qq.com/club/item/parcel/item/3b/abc/raw300.gif"
        names = ["微笑", "可怜", "叉腰"]  # 前两个是经典黄脸，第三个是商城贴纸

        keys = {n: self.fi.key_for(n, sticker) for n in names}
        urls = {n: self.fi.url_for(n, sticker) for n in names}
        self.assertEqual(len(set(keys.values())), 3, f"三个名字塌成同一把键：{keys}")
        self.assertEqual(keys["微笑"], "c0")
        self.assertEqual(keys["可怜"], "c53")
        self.assertEqual(keys["叉腰"], self.fi.market_key(sticker), "真正的贴纸仍按地址走")
        self.assertEqual(
            urls["微笑"], "https://qzonestyle.gtimg.cn/qzone/em/e100.gif", "经典表情的图不许被贴纸顶掉"
        )
        self.assertEqual(urls["叉腰"], sticker)

        # 键与 URL 必须同源：否则 ensure() 拿 c0 去查缓存、却下载贴纸，
        # 正确的黄脸缓存会被反复判成未命中而重抓
        for name in names:
            with self.subTest(name=name):
                key = keys[name]
                got = urls[name]
                if key.startswith("c"):
                    self.assertIn("qzonestyle.gtimg.cn", got, "经典键必须配经典的官方地址")
                else:
                    self.assertEqual(got, sticker, "商城键必须配那个商城地址")

        # 顺序无关：谁先出现都不该改变结果
        self.assertEqual(keys, {n: self.fi.key_for(n, sticker) for n in reversed(names)})

    def test_foreign_market_url_cannot_override_classic_identity(self):
        """非白名单地址既不许被取用，也不许改写表情的身份。"""
        evil = "https://evil.example.com/a.gif"
        # 经典表情：走官方地址，外部地址根本参与不进来（顺带把 SSRF 面关死）
        self.assertEqual(self.fi.url_for("微笑", evil), "https://qzonestyle.gtimg.cn/qzone/em/e100.gif")
        self.assertEqual(self.fi.key_for("微笑", evil), "c0", "外部地址不许污染经典表的键")
        # 非经典名：白名单外的地址一律不给 URL（不许把这段代码当成任意下载器）
        self.assertIsNone(self.fi.url_for("[13]", evil))

    def test_offline_failure_degrades_gracefully(self):
        import urllib.error

        with mock.patch.object(self.fi, "_download", side_effect=urllib.error.URLError("no network")):
            out = self.fi.ensure({"/流泪": {}}, allow_network=True)
        self.assertEqual(out, {}, "离线时应静默返回空，交由 emoji/文字回退")

    def test_cached_file_is_reused_without_network(self):
        with open(os.path.join(self._tmp, "c5.gif"), "wb") as f:
            f.write(b"GIF89a" + b"y" * 10)
        with mock.patch.object(self.fi, "_download") as dl:
            out = self.fi.ensure({"/流泪": {}}, allow_network=True)
        self.assertFalse(dl.called, "已有缓存时不该联网")
        self.assertIn("/流泪", out)
        self.assertTrue(out["/流泪"]["path"].endswith("c5.gif"))

    def test_local_pack_wins_over_network(self):
        """本地表情包目录优先于联网（超级表情只能靠它拿到原图）"""
        pack = tempfile.mkdtemp(prefix="qqchatlog-pack-")
        try:
            with open(os.path.join(pack, "吃糖.gif"), "wb") as f:
                f.write(b"GIF89a" + b"z" * 10)
            with (
                mock.patch.dict(os.environ, {"QQCHAT_FACE_DIR": pack}),
                mock.patch.object(self.fi, "_download") as dl,
            ):
                out = self.fi.ensure({"/吃糖": {}}, allow_network=True)
            self.assertFalse(dl.called, "本地已有图就不该联网")
            self.assertIn("/吃糖", out)
        finally:
            import shutil

            shutil.rmtree(pack, ignore_errors=True)

    def test_serve_path_rejects_traversal(self):
        self.assertIsNone(self.fi.serve_path("../../etc/passwd"))
        self.assertIsNone(self.fi.serve_path("c5/../../x"))
        with open(os.path.join(self._tmp, "c5.gif"), "wb") as f:
            f.write(b"GIF89a")
        self.assertTrue(self.fi.serve_path("c5"))

    def test_fetch_endpoint_requires_switch(self):
        import app as appmod

        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            tok = s["csrf_token"]
        headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": tok}
        with mock.patch.object(self.fi, "FACE_IMAGES_ENABLED", False):
            r = client.post("/api/faces/fetch", headers=headers)
            self.assertEqual(r.status_code, 403, "开关关闭时接口必须拒绝")
        r2 = client.post("/api/faces/fetch", headers=headers)
        self.assertEqual(r2.status_code, 400, "没有聊天记录时提示重新上传")

    def test_habits_page_shows_button_when_enabled(self):
        import app as appmod

        payload = _fix_wrap(
            [
                {
                    "id": "1",
                    "timestamp": 1704067200000,
                    "time": "2024-01-01 08:00:00",
                    "sender": {"uid": "uA", "name": "我"},
                    "content": {
                        "text": "",
                        "elements": [{"type": "face", "data": {"id": "5", "name": "/流泪"}}],
                    },
                },
                {
                    "id": "2",
                    "timestamp": 1704067260000,
                    "time": "2024-01-01 08:01:00",
                    "sender": {"uid": "uB", "name": "对方"},
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            tok = s["csrf_token"]
        client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": tok},
        )
        with client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        try:
            storemod.wait_for_stats(chat_hash, timeout=20)
            on = client.get("/habits").get_data(as_text=True)
            self.assertIn('id="fetchFacesBtn"', on, "开关打开时应给出联网抓取入口")
            with mock.patch.object(self.fi, "FACE_IMAGES_ENABLED", False):
                off = client.get("/habits").get_data(as_text=True)
            self.assertNotIn('id="fetchFacesBtn"', off, "开关关闭时不该出现联网入口")
        finally:
            storemod._purge_chat_caches(chat_hash)
            if path and os.path.exists(path):
                os.remove(path)

    def test_report_export_does_not_swallow_the_body(self):
        """导出报告在 DOM 克隆体上删节点，不再对 HTML 字符串做正则手术。

        曾经的写法 `<script(?![^>]*src=)[^>]*>[\\s\\S]*?window\\.CSRF_TOKEN[\\s\\S]*?</script>`
        会从 <head> 的主题脚本一路匹配到页面底部的 CSRF 脚本，把整个 body 删掉
        （实测导出的 HTML 只剩 205 字节）。这里守住"按节点处理"的写法：
        跨脚本的贪婪正则不许再出现，CSRF 脚本仍要按内容精确剔除。
        """
        src = (ROOT / "web" / "templates" / "_report_assets.html").read_text(encoding="utf-8")
        # 注释里会引用旧写法作为反面教材，所以只检查真正的代码行
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("//"))
        self.assertNotIn("[\\s\\S]*?window\\.CSRF_TOKEN", code, "不许再用跨脚本的贪婪正则删 CSRF 脚本")
        self.assertNotIn(
            "document.documentElement.outerHTML", code, "导出快照必须走 cloneNode，不能拿整页 HTML 串做替换"
        )
        self.assertIn("cloneNode(true)", code, "应当在 DOM 克隆体上删改")
        self.assertIn("window\\.CSRF_TOKEN", code, "仍要剥掉带 token 的那个脚本")
        self.assertIn("querySelectorAll('script')", code, "按节点遍历脚本，逐块判断")

    def test_report_export_inlines_face_images(self):
        """导出的报告要自带表情图（内联成 data URL），否则分享出去就是裂图"""
        src = (ROOT / "web" / "templates" / "_report_assets.html").read_text(encoding="utf-8")
        self.assertIn("inlineFaceImages", src)
        self.assertIn("readAsDataURL", src)


if __name__ == "__main__":
    unittest.main()
