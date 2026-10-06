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

"""聊天结果包的文件收集、校验与导入导出。

目录、日志、路径归属检查和原子写入由 webapp.store 注入。这样持久层继续保留原有
公开入口与可替换配置，同时打包安全逻辑可以独立阅读。
"""

import json
import os
import re
import time
import zipfile


BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,120}\.json$")
CHAT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
BUNDLE_MAX_ENTRIES = 5000
BUNDLE_MAX_TOTAL_BYTES = 512 * 1024 * 1024
BUNDLE_META_NAME = "meta.json"
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


def chat_bundle_files(
    chat_hash: str,
    *,
    ai_cache_dir: str,
    stats_cache_dir: str,
    cache_belongs_to,
    is_safe_month_key,
    logger,
) -> dict:
    """返回属于该聊天的缓存清单；月份文件通过 manifest 的引用收集。"""
    if not chat_hash:
        return {"ai_cache": [], "stats_cache": []}
    ai: list[str] = []
    month_keys: list[str] = []
    try:
        entries = os.listdir(ai_cache_dir)
    except OSError:
        entries = []
    for name in entries:
        if not cache_belongs_to(name, chat_hash):
            continue
        ai.append(name)
        if name.startswith("manifest_"):
            try:
                with open(os.path.join(ai_cache_dir, name), encoding="utf-8") as f:
                    month_keys += [str(k) for k in (json.load(f).get("months") or [])]
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
    for key in month_keys:
        if not is_safe_month_key(key):
            logger.warning("manifest 里有形状非法的月份键，导出时已跳过")
            continue
        name = f"month_{key}.json"
        if name not in ai and os.path.exists(os.path.join(ai_cache_dir, name)):
            ai.append(name)
    stats: list[str] = []
    stats_name = f"stats_{chat_hash}.json"
    if os.path.exists(os.path.join(stats_cache_dir, stats_name)):
        stats.append(stats_name)
    return {"ai_cache": sorted(ai), "stats_cache": stats}


def write_chat_bundle(
    chat_hash: str,
    stream,
    *,
    files_for_chat,
    ai_cache_dir: str,
    stats_cache_dir: str,
    meta_name: str,
) -> int:
    """把该聊天的统计与已付费结果写成 zip，返回缓存文件数。"""
    files = files_for_chat(chat_hash)
    count = 0
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            meta_name,
            json.dumps({"bundle": 1, "chat_hash": chat_hash, "created": time.time()}, ensure_ascii=False),
        )
        for logical, names in (("ai_cache", files["ai_cache"]), ("stats_cache", files["stats_cache"])):
            base = ai_cache_dir if logical == "ai_cache" else stats_cache_dir
            for name in names:
                try:
                    zf.write(os.path.join(base, name), f"{logical}/{name}")
                    count += 1
                except OSError:
                    pass
    return count


def bundle_leaf_ok(leaf: str, head: str, declared_hash: str, *, cache_belongs_to) -> bool:
    """验证缓存文件名是否属于包里声明的聊天。"""
    if head == "stats_cache":
        return leaf == f"stats_{declared_hash}.json"
    if leaf.startswith("month_"):
        # 月份缓存按内容寻址，由 manifest 引用；它可被不同聊天共同使用。
        return True
    if leaf.startswith("manifest_"):
        return leaf == f"manifest_{declared_hash}.json"
    return cache_belongs_to(leaf, declared_hash)


def bundle_payload_ok(raw: bytes) -> bool:
    """要求单个缓存条目是可解析、非空的 JSON 对象。"""
    if len(raw) > MAX_PAYLOAD_BYTES:
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and bool(data)


def read_chat_bundle(
    stream,
    confirm_overwrite: bool = False,
    live_hash: str = "",
    *,
    ai_cache_dir: str,
    stats_cache_dir: str,
    name_re,
    hash_re,
    max_entries: int,
    max_total_bytes: int,
    meta_name: str,
    leaf_ok,
    payload_ok,
    write_bytes_atomic,
    clear_chat_cache,
    logger,
) -> dict:
    """导入安全校验通过的缓存条目，路径只由白名单目录和叶子名构造。"""
    try:
        zf = zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, OSError):
        return {"error": "不是有效的导出包（zip 打不开）；请使用本工具的「导出」生成的文件"}
    with zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            return {"error": f"导出包条目过多（{len(infos)}），拒绝"}

        try:
            meta_raw = zf.read(meta_name)
        except KeyError:
            return {"error": "结果包缺少 meta.json，无法确认它属于哪份聊天，已拒绝"}
        except Exception as e:
            return {"error": f"结果包的 meta.json 读不出来（{type(e).__name__}），已拒绝"}
        try:
            declared = str(json.loads(meta_raw.decode("utf-8")).get("chat_hash") or "").strip()
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            declared = ""
        if not hash_re.fullmatch(declared):
            return {"error": "结果包的 meta.json 缺少合法的 chat_hash，已拒绝"}

        if live_hash and live_hash == declared and not confirm_overwrite:
            return {
                "need_confirm": True,
                "error": "这份结果包属于你当前正在打开的聊天，导入会覆盖现有的统计与已付费结果。"
                "确认要继续请再点一次「导入」。",
            }

        importable = []
        skipped = total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name == meta_name:
                continue
            head, sep, leaf = name.rpartition("/")
            if not sep or head not in ("ai_cache", "stats_cache") or not name_re.match(leaf):
                skipped += 1
                continue
            if not leaf_ok(leaf, head, declared):
                skipped += 1
                continue
            total += max(0, info.file_size)
            importable.append((info, head, leaf))

        # 先检查整包预计写入体积，再开始覆盖任何现有缓存；超限包保持原状。
        if total > max_total_bytes:
            return {"error": "导出包解压体积超限，已中止（导入不完整）"}

        written = 0
        for info, head, leaf in importable:
            target_dir = ai_cache_dir if head == "ai_cache" else stats_cache_dir
            path = os.path.join(target_dir, leaf)
            try:
                with zf.open(info) as src:
                    # 文件大小由 ZIP 元数据提供；限长读取，避免损坏或伪造元数据导致
                    # 单条目在 JSON 大小校验前先把过量解压数据读入内存。
                    raw = src.read(MAX_PAYLOAD_BYTES + 1)
            except Exception as e:
                logger.warning("结果包条目 %s 读不出来（%s），已跳过", leaf, type(e).__name__)
                skipped += 1
                continue
            if len(raw) > MAX_PAYLOAD_BYTES:
                logger.warning("结果包条目 %s 超过单文件大小上限，已跳过", leaf)
                skipped += 1
                continue
            if not payload_ok(head, leaf, raw):
                logger.warning("结果包条目 %s 形状不对，已跳过", leaf)
                skipped += 1
                continue
            try:
                write_bytes_atomic(path, raw, mkdir=target_dir)
                written += 1
            except OSError:
                skipped += 1
    if written:
        clear_chat_cache()
    return {"written": written, "skipped": skipped, "chat_hash": declared[:12]}
