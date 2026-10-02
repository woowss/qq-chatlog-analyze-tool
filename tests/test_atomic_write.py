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
#
#
"""原子落盘（`analyzer.atomic_write`）的回归用例。

钉住两件事：

1. **临时名必须唯一**。原先这段代码被抄了 9 遍，其中 `vision` / `usage` / `face_images`
   三处漏修，仍用固定的 `f"{path}.tmp"`：两个并发写者踩同一个临时文件，任一方在失败
   分支里 `os.remove(tmp)` 就会删掉对方正在写的那份，`os.replace` 于是可能把**半截
   文件发布成正式缓存**——图片摘要与月份缓存丢的都是已付费结果。把任何一处还原成
   固定名，`test_production_writers_do_not_use_a_fixed_tmp_name` 必须变红。
2. **失败不许留半成品**。目录被用户删掉（README 教的"彻底清除数据"）时写入会失败，
   残留的 `.tmp` 会被 manifest 扫描当成一份真清单，把含聊天原句引用的月份文件永久
   钉成"仍被引用"。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# 测试隔离 + 网络护栏：见 tests/_bootstrap.py（必须在 import 项目模块之前完成）
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer import atomic_write, face_images, usage, vision  # noqa: E402


class TmpNameSurface(unittest.TestCase):
    """把 `os.replace` 的源文件名收集起来，看每个写者实际用的临时名长什么样"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qqchatlog-atomic-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.seen = []

    def _capture(self):
        real = os.replace

        def spy(src, dst, *a, **kw):
            self.seen.append((src, dst))
            return real(src, dst, *a, **kw)

        return mock.patch.object(atomic_write.os, "replace", side_effect=spy)

    def test_production_writers_do_not_use_a_fixed_tmp_name(self):
        """三个曾被漏修的写点：临时名不能等于 `正式名 + ".tmp"`

        反向验证：把 `analyzer/vision.py` / `analyzer/usage.py` / `analyzer/face_images.py`
        里任何一处改回自己拼 `f"{path}.tmp"`，本用例立刻变红。
        """
        with self._capture():
            vision._write_cache(os.path.join(self.dir, "vision_abc.json"), "摘要")
            with mock.patch.object(usage, "TOKEN_USAGE_FILE", os.path.join(self.dir, "token_usage.json")):
                usage._dump({"days": {}, "dims": {}, "total": {"calls": 1, "prompt": 2, "completion": 3}})
            with mock.patch.object(face_images, "FACE_CACHE_DIR", self.dir):
                face_images._store("face_key", b"GIF89a" + b"\x00" * 32)

        self.assertEqual(len(self.seen), 3, "三处写入都应当走原子替换")
        for src, dst in self.seen:
            self.assertTrue(src.endswith(".tmp"), f"临时名要仍以 .tmp 结尾，清理侧才认得：{src}")
            self.assertNotEqual(src, dst + ".tmp", f"临时名必须是唯一名，不能是固定名：{src}")
            self.assertEqual(src[: len(dst)], dst, f"临时名必须是正式名的兄弟（同目录同名前缀）：{src}")

    def test_two_writers_of_one_key_do_not_publish_a_half_file(self):
        """并发写同一个键：正式文件必须始终是某一份完整内容，不能混进另一份的尾巴

        注意**败者抛 `OSError` 是允许的**：Windows 上两个 `os.replace` 同时指向同一个目标时，
        被拒的那个拿到 `PermissionError(13, 拒绝访问)`（POSIX 上则总有一个成功）。生产侧就是
        按这个事实写的——每个写点都 `except OSError` 出声后继续：丢一次缓存只是下次重算，
        而把半截文件发布成正式结果才是真损失。这里要钉的是**结果形状**，不是"不许有异常"。
        """
        path = os.path.join(self.dir, "month_contended.json")
        small = {"_created": 1.0, "result": {"tag": "A", "text": "x"}}
        big = {"_created": 2.0, "result": {"tag": "B", "text": "y" * 200000}}
        barrier = threading.Barrier(2)
        errors = []

        def worker(payload):
            try:
                barrier.wait(timeout=5)
                for _ in range(8):
                    atomic_write.write_json_atomic(path, payload)
            except Exception as e:  # 收集起来，别让子线程的异常静默
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(p,)) for p in (small, big)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for e in errors:
            self.assertIsInstance(e, OSError, f"并发写入只允许抛 OSError（替换被拒/磁盘问题），实际：{e!r}")
        self.assertTrue(os.path.exists(path), "两个写者都失败到连一份完整结果都没留下，才算问题")

        with open(path, "r", encoding="utf-8") as f:
            final = json.load(f)  # 半截文件会在这里 JSONDecodeError
        self.assertIn(final["result"]["tag"], ("A", "B"), "正式文件必须是其中一份完整结果")
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [], f"失败的写者要清掉自己那份半成品：{leftovers}")

    def test_failure_removes_its_own_tmp_and_raises(self):
        """写失败时：清掉自己的半成品，并把 `OSError` 原样交回调用方

        留着半成品的后果不是"多一个垃圾文件"：月份目录的 manifest 扫描按 `manifest_`
        前缀认领活清单，一个 `.tmp` 会被当成一份真清单读进引用集，于是含聊天原句引用的
        月份文件被永久钉成"仍被引用"，purge 与孤儿回收都收不走它。
        """
        path = os.path.join(self.dir, "will_fail.json")
        tmp = os.path.join(self.dir, "planted.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("半个文件")
        with (
            mock.patch.object(atomic_write, "tmp_sibling", return_value=tmp),
            mock.patch.object(atomic_write.os, "replace", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                atomic_write.write_json_atomic(path, {"a": 1})
        self.assertFalse(os.path.exists(tmp), "失败分支必须删掉自己那份半成品")
        self.assertFalse(os.path.exists(path), "失败不该留下正式文件")

    def test_missing_parent_directory_raises_instead_of_silently_dropping(self):
        """不补目录时父目录不存在要出声（`mkdir` 参数才是自愈路径）"""
        orphan = os.path.join(self.dir, "gone", "month_x.json")
        with self.assertRaises(OSError):
            atomic_write.write_json_atomic(orphan, {"a": 1})
        atomic_write.write_json_atomic(orphan, {"a": 1}, mkdir=os.path.dirname(orphan))
        self.assertTrue(os.path.exists(orphan), "传了 mkdir 就应当自建目录并写成功")


class TmpNameUniqueness(unittest.TestCase):
    """同一进程内不同线程、以及同线程连续两次，都必须拿到不同的临时名"""

    def test_names_are_unique_within_and_across_threads(self):
        base = os.path.join(tempfile.gettempdir(), "qqchatlog-name-probe.json")
        names = {atomic_write.tmp_sibling(base) for _ in range(50)}
        self.assertEqual(len(names), 50, "同线程连续取 50 次不该重名")

        found = []
        lock = threading.Lock()

        def grab():
            got = {atomic_write.tmp_sibling(base) for _ in range(50)}
            with lock:
                found.extend(got)

        threads = [threading.Thread(target=grab) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(set(found)), 300, "并发线程之间也不该撞名")


if __name__ == "__main__":
    unittest.main()
