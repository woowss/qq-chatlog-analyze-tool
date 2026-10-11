import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import vision  # noqa: E402


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
        from analyzer import deepseek_client as dc

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
        from analyzer import deepseek_client as dc

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
        from analyzer import deepseek_client as dc

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
        from analyzer import deepseek_client as dc

        digest = hashlib.sha256()
        for part in (dc.DEEPSEEK_MODEL, prompt_fingerprint, vision.VISION_SYSTEM, vision.VISION_DETAIL):
            digest.update(str(part).encode("utf-8"))
            digest.update(b"\x00")
        for image in images:
            digest.update(str(image.get("legacy_key", image["key"])).encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()[:20]


if __name__ == "__main__":
    unittest.main()
