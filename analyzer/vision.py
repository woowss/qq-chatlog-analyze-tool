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
"""图片理解（视觉）：把聊天里的截图/照片/表情包交给多模态模型"看一眼"，产出文字摘要。

为什么要做成一月一次摘要，而不是每张图都塞进分析 prompt：
- deepseek-flash 每张图最多 1024 tokens（官方按约 1300x1300 折算），而同一批图片要被
  5 个维度分别使用；逐维度发图等于把图片 token 乘以 5。这里改成"每月一次摘要、
  摘要进所有维度的 prompt"，并把摘要按图片指纹落盘——重跑/换维度都不再重复付费。
- 图片本体只从本地 `QQCHAT_MEDIA_DIR` 读取，只发给用户自己配置的 LLM 端点；
  关掉 LLM_VISION 就完全回到纯文本。
"""

import base64
import hashlib
import json
import os
import shutil
import threading
import time

from config import (
    AI_CACHE_DIR,
    MEDIA_ROOT,
    UPLOAD_FOLDER,
    VISION_DETAIL,
    VISION_ENABLED,
    VISION_MAX_BYTES,
    VISION_MAX_PER_MONTH,
    VISION_MAX_TOTAL_BYTES,
    VISION_MIN_SIDE,
)
from analyzer.logger import get_logger

logger = get_logger("vision")

# WebUI 上传的图片副本存放处（uploads/media/<chat_hash>/，随 uploads/ 的 24 小时策略回收）。
# 图片本体只是"生产摘要"的原料：摘要按指纹落盘后，重跑分析不再需要原图。
MEDIA_SUBDIR = "media"

# 摘要工作的 system prompt（进缓存键，改了它旧摘要会自动失效）
VISION_SYSTEM = (
    "你是聊天记录的图像观察员。用户会给你一段私聊里出现的图片（按时间顺序）。"
    "请用中文逐条概括每张图的内容与用途：若是聊天/网页截图，说明在聊什么、有什么关键信息；"
    "若是表情包或梗图，说明它表达的情绪或笑点；若是照片，说明场景与人物关系。"
    "只写你确实看到的内容，看不清就说看不清；不要编造人名，不要复述手机号、地址等隐私细节。"
    "每条一行，以“- ”开头，总长不超过 240 字。"
)

_SUPPORTED = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
SUPPORTED_EXT = frozenset(_SUPPORTED)  # 供上传接口做扩展名白名单

# WebUI 上传图片副本的容量上限：一次最多接收多少张 / 多少字节
MEDIA_UPLOAD_MAX_FILES = 400
MEDIA_UPLOAD_MAX_BYTES = 256 * 1024 * 1024

_MEMO: dict[str, str] = {}  # 进程内摘要缓存：key -> 摘要文本
_MEMO_LOCK = threading.Lock()
_MEMO_MAX = 64


def available(chat_hash: str = "") -> bool:
    """是否具备图片理解条件：开关打开 + 允许送图 + （配了媒体目录 或 本次上传过图片）"""
    if not (VISION_ENABLED and VISION_MAX_PER_MONTH > 0):
        return False
    if MEDIA_ROOT:
        return True
    return bool(chat_hash and os.path.isdir(session_media_dir(chat_hash)))


def session_media_dir(chat_hash: str) -> str:
    """WebUI 上传的图片副本目录：uploads/media/<chat_hash>/"""
    return os.path.join(UPLOAD_FOLDER, MEDIA_SUBDIR, chat_hash or "nohash")


def purge_session_media(chat_hash: str) -> int:
    """删除某次上传的图片副本目录，返回删掉的文件数。

    聊天被替换或删除时级联调用：源文件及其派生结果都清了，图片本体没有理由留着
    （它的唯一用途是"生产摘要"，而摘要已按图片指纹另行缓存）。
    """
    if not chat_hash:
        return 0
    target = session_media_dir(chat_hash)
    if not os.path.isdir(target):
        return 0
    try:
        count = sum(1 for name in os.listdir(target) if os.path.isfile(os.path.join(target, name)))
    except OSError:
        count = 0
    shutil.rmtree(target, ignore_errors=True)
    return count


def _within(root: str, path: str) -> bool:
    """路径是否确实位于 root 之内（防止 ../ 逃逸）"""
    try:
        root_abs = os.path.abspath(root)
        return os.path.commonpath([root_abs, os.path.abspath(path)]) == root_abs
    except ValueError:  # 不同盘符等
        return False


def resolve(media_path: str, chat_hash: str = ""):
    """把导出器给的相对路径（如 resources/images/x.jpg）落到本地实体文件。

    查找顺序：WebUI 上传的副本 → 环境变量指定的媒体根目录。
    上传副本按 basename 平铺存放（导出器的文件名自带 md5 前缀，天然唯一），
    这样既不用还原目录结构，也天然免疫路径穿越。

    media_path 来自**导出文件**（可被构造），所以解析结果必须落在允许的目录内：
    否则一个 `"url": "../../其他目录/私密.png"` 就能让本机别的图片被读出来发给模型。
    """
    if not media_path:
        return None
    rel = str(media_path).replace("\\", "/").lstrip("/")
    base = os.path.basename(rel)
    if chat_hash and base:
        candidate = os.path.join(session_media_dir(chat_hash), base)
        if os.path.isfile(candidate):
            return candidate
    if not MEDIA_ROOT:
        return None
    candidates = [os.path.join(MEDIA_ROOT, rel)]
    if rel.startswith("resources/"):
        candidates.append(os.path.join(MEDIA_ROOT, rel[len("resources/") :]))
    else:
        candidates.append(os.path.join(MEDIA_ROOT, "resources", rel))
    for cand in candidates:
        if os.path.isfile(cand) and _within(MEDIA_ROOT, cand):
            return cand
    return None


def _sample(msgs: list, limit: int) -> list:
    """按"去重 → 跳过小图 → 等间隔抽样"的规则挑消息（不检查文件是否存在）"""
    picked: list = []
    seen: set = set()
    for m in msgs:
        if not m.has_image:
            continue
        if m.media_w and m.media_h and max(m.media_w, m.media_h) < VISION_MIN_SIDE:
            continue  # 小图基本是表情包/缩略图，不值得花 token
        key = m.media_id or m.media_path
        if not key or key in seen:
            continue  # 同一张图反复发只算一次
        if not m.media_path:
            continue
        seen.add(key)
        picked.append(m)
    if limit and len(picked) > limit:
        stride = (len(picked) + limit - 1) // limit
        picked = picked[::stride][:limit]
    return picked


def plan_wanted(months: dict, limit_per_month: int = None) -> list[str]:
    """算出"要让模型看图的话，需要哪些图片"（返回导出器里的相对路径）。

    WebUI 上传流程用它：前端据此从用户选中的导出目录里只挑这些文件上传，
    避免为了看图把整个 resources/（实测近 1 GB）都搬一遍。
    """
    limit = VISION_MAX_PER_MONTH if limit_per_month is None else limit_per_month
    if not (VISION_ENABLED and limit > 0):
        return []
    wanted: list[str] = []
    seen: set = set()
    for msgs in months.values():
        for m in _sample(msgs, limit):
            rel = str(m.media_path)
            base = os.path.basename(rel.replace("\\", "/"))
            if base and base not in seen:
                seen.add(base)
                wanted.append(rel)
    return wanted


def pick_images(msgs: list, limit: int = None, chat_hash: str = "") -> list[dict]:
    """从消息里挑出**本地确实存在**的图片：去重 → 跳过小图/超大/找不到的 → 等间隔抽样。

    还需要卡住"一次请求的图片总体积"：官方请求体上限 48 MiB，而 base64 会膨胀约 1/3，
    20 张大图足以超限（整次摘要被拒 → 该月看不到图）。这里按顺序贪心装入，装不下就停。
    """
    limit = VISION_MAX_PER_MONTH if limit is None else limit
    picked: list[dict] = []
    total = 0
    for m in _sample(msgs, limit):
        path = resolve(m.media_path, chat_hash)
        if not path:
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in _SUPPORTED:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > VISION_MAX_BYTES or size == 0:
            continue
        if picked and total + size > VISION_MAX_TOTAL_BYTES:
            logger.info("图片摘要已达单次体积上限（%.1f MB），本月其余图片留到下次", total / 1048576)
            break
        total += size
        picked.append(
            {
                "path": path,
                "key": m.media_id or m.media_path,
                "size": size,
                "mime": _SUPPORTED[ext],
                "time": m.time_str,
                "sender": m.sender_name,
            }
        )
    return picked


def _images_key(images: list[dict]) -> str:
    """摘要缓存键：图片指纹（md5）+ 顺序 + 模型/系统提示词/清晰度都要进哈希"""
    from analyzer import deepseek_client as dc  # 延迟导入，避免循环依赖

    digest = hashlib.sha256()
    for part in (dc.DEEPSEEK_MODEL, dc.PROMPT_FINGERPRINT, VISION_SYSTEM, VISION_DETAIL):
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    for img in images:
        digest.update(str(img["key"]).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:20]


def _cache_path(chat_hash: str, key: str) -> str:
    os.makedirs(AI_CACHE_DIR, exist_ok=True)
    # 文件名里带上 chat_hash：删聊天/换文件时会被 _purge_chat_caches 一并回收，
    # 不会留下含图片描述的孤儿缓存
    return os.path.join(AI_CACHE_DIR, f"vision_{chat_hash}_{key}.json")


def _read_cache(path: str):
    """读摘要缓存；旧格式（只有 digest 字段）同样兼容"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("digest"), str):
        try:
            os.utime(path, None)  # 命中续期，配合 30 天滑动窗口
        except OSError:
            pass
        return data["digest"]
    return None


def _write_cache(path: str, digest: str) -> None:
    """写摘要缓存，并记下创建时间。

    创建时间是"绝对 90 天上限"的依据（清理任务读 _created）：只写 mtime 的话，
    天天看的报告会把 mtime 一直续期，这类含聊天图片描述的缓存就永远不会被回收。
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"_created": time.time(), "digest": digest}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("图片摘要缓存写入失败: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def digest(msgs: list, chat_hash: str = "", label: str = "") -> str:
    """返回该批消息的图片摘要（无图/不可用/失败时返回空串）。

    优先级：进程内缓存 → 磁盘缓存（跨维度、跨重跑复用）→ 调一次视觉模型。
    """
    if not available(chat_hash):
        return ""
    images = pick_images(msgs, chat_hash=chat_hash)
    if not images:
        return ""
    key = _images_key(images)
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None:
        return hit
    path = _cache_path(chat_hash or "nohash", key)
    cached = _read_cache(path)
    if cached is not None:
        with _MEMO_LOCK:
            _MEMO[key] = cached
        return cached

    from analyzer import deepseek_client as dc  # 延迟导入，避免循环依赖

    text = dc._call_vision(VISION_SYSTEM, _build_user_text(images, label), images)
    if not text:
        return ""
    _write_cache(path, text)
    with _MEMO_LOCK:
        if len(_MEMO) > _MEMO_MAX:
            _MEMO.clear()
        _MEMO[key] = text
    logger.info("图片摘要完成：%d 张（%s）", len(images), label or "本批")
    return text


def _build_user_text(images: list[dict], label: str) -> str:
    head = (
        f"以下是{label}出现的 {len(images)} 张图片（已按时间顺序排列）："
        if label
        else f"以下是对话中出现的 {len(images)} 张图片（已按时间顺序排列）："
    )
    return head + "\n请逐条概括。"


def load_image_b64(path: str, mime: str) -> str:
    """读图并编码成 data URL（base64 内联，官方推荐本地文件这么做）"""
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode('utf-8')}"
