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
"""QQ 表情原图（可选功能，默认关闭）

界面默认用 Unicode emoji 渲染表情排行（完全离线）。打开 `QQCHAT_FACE_IMAGES`
后可以在页面上点一次「获取原始表情图」，把 QQ 的原始表情图下载到本地缓存
（`face_cache/`），此后离线复用。

三个来源，按优先级：
1. **本地表情包目录**（`QQCHAT_FACE_DIR`）：你自己放的表情图，文件名支持
   `<经典编号>.gif`、`e<经典编号+100>.gif`、`<表情名>.gif`（png/jpg/webp 亦可）；
2. **联网抓取**（只在开关打开且用户点了按钮时发生）：
   - 经典黄脸：Qzone 公开表情 CDN `qzonestyle.gtimg.cn/qzone/em/e{编号+100}.gif`；
   - 商城表情：导出文件里自带的 `url`（`gxh.vip.qq.com/.../raw300.gif`）；
3. 两者都没有的（QQ 超级表情：吃糖、大怨种、菜汪、宕机…）**不猜地址、不抓**，
   继续用 emoji / 表情名渲染。

为什么"按名字"而不是"按导出文件里的 id"取图：实测某份 QQNT 导出的 id 空间与
CDN 编号并不一致（导出里 id=0 叫「惊讶」而 e100 是「微笑」、id=34 叫「晕」而 e134
是「折磨」），按 id 取图会张冠李戴，实测只有 21% 的用量对得上；而**名称**是界面
真正显示的东西，用名称反查编号，语义才对得上（已逐一核对：可怜→e153、流泪→e105…）。
"""
import hashlib
import os
import time
import urllib.error
import urllib.request

from config import (
    FACE_CACHE_DIR,
    FACE_FETCH_LIMIT,
    FACE_FETCH_TIMEOUT,
    FACE_IMAGES_ENABLED,
)
from analyzer.logger import get_logger

logger = get_logger("faces")

#: 经典黄脸在 Qzone CDN 上的地址规律：e{经典编号 + 100}.gif（实测可用）
QZONE_PATTERN = "https://qzonestyle.gtimg.cn/qzone/em/e{0}.gif"

#: 只允许从这两个已知的表情 CDN 取图，避免这段代码被当成任意 URL 下载器
ALLOWED_HOSTS = ("qzonestyle.gtimg.cn", "gxh.vip.qq.com")

#: 经典黄脸编号 → 名称（用于按名称反查编号；名称对不上的一律不取图）
CLASSIC_NAMES = {
    0: "微笑", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞", 7: "闭嘴",
    8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙", 14: "惊讶",
    15: "难过", 16: "酷", 17: "冷汗", 18: "抓狂", 19: "吐", 20: "偷笑", 21: "可爱",
    22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑",
    29: "大兵", 30: "奋斗", 31: "疑问", 32: "嘘", 33: "晕", 34: "折磨", 35: "衰",
    36: "骷髅", 37: "敲打", 38: "再见", 39: "擦汗", 40: "抠鼻", 41: "鼓掌", 42: "糗大了",
    43: "坏笑", 44: "左哼哼", 45: "右哼哼", 46: "哈欠", 47: "鄙视", 48: "委屈",
    49: "快哭了", 50: "阴险", 51: "亲亲", 52: "吓", 53: "可怜", 54: "菜刀", 55: "西瓜",
    56: "啤酒", 57: "篮球", 58: "乒乓", 59: "咖啡", 60: "饭", 61: "猪头", 62: "玫瑰",
    63: "凋谢", 64: "示爱", 65: "爱心", 66: "心碎", 67: "蛋糕", 68: "闪电", 69: "炸弹",
    70: "刀", 71: "足球", 72: "瓢虫", 73: "便便", 74: "月亮", 75: "太阳", 76: "礼物",
    77: "拥抱", 78: "强", 79: "弱", 80: "握手", 81: "胜利", 82: "抱拳", 83: "勾引",
    84: "拳头", 85: "差劲", 86: "爱你", 87: "NO", 88: "OK", 89: "爱情", 90: "飞吻",
    91: "跳跳", 92: "发抖", 93: "怄火", 94: "转圈", 95: "磕头", 96: "回头", 97: "跳绳",
    98: "挥手", 99: "激动", 100: "街舞", 101: "献吻",
}
NAME_TO_CLASSIC = {name: fid for fid, name in CLASSIC_NAMES.items()}

_IMAGE_EXT = (".gif", ".png", ".jpg", ".jpeg", ".webp")
_MAGIC = (b"GIF8", b"\x89PNG", b"\xff\xd8\xff", b"RIFF")

#: 单次点击"获取原始表情图"的联网抓取时长上限（秒）：请求是同步的，
#: 宁可少抓几张下次接着抓，也不要把页面挂住十几分钟。
FETCH_BUDGET_SECONDS = 60.0


def enabled() -> bool:
    return bool(FACE_IMAGES_ENABLED)


def cache_dir() -> str:
    os.makedirs(FACE_CACHE_DIR, exist_ok=True)
    return FACE_CACHE_DIR


def clean_name(name: str) -> str:
    """表情名归一：去掉 "/"、"[ ]"、"[[ ]]" 与空白（导出器几种写法都见得到）"""
    return (name or "").strip().lstrip("/").strip("[]").strip()


def market_key(url: str) -> str:
    return "m" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def name_key(name: str) -> str:
    """按名字生成的稳定键：给"经典表里没有、也没有商城地址"的表情用（如超级表情）。

    这类表情抓不到原图，但只要用户提供了本地表情包（文件名 = 表情名），
    依然可以按这个键读出来——所以 key 必须存在，不能因为"抓不到"就整条跳过。
    """
    return "n" + hashlib.sha1(clean_name(name).encode("utf-8")).hexdigest()[:16]


def key_for(name: str, market_url: str = "") -> str:
    """缓存键：经典表情按名称反查的编号（跨导出稳定），商城表情按其地址"""
    if market_url:
        return market_key(market_url)
    clean = clean_name(name)
    if not clean:
        return ""
    fid = NAME_TO_CLASSIC.get(clean)
    return f"c{fid}" if fid is not None else name_key(clean)


def _url_allowed(url: str) -> bool:
    return any(url.startswith(f"https://{h}/") for h in ALLOWED_HOSTS)


def url_for(name: str, market_url: str = ""):
    """该表情能从哪个地址取原图；没有则 None（超级表情/未收录名称一律不猜）"""
    if market_url and _url_allowed(market_url):
        return market_url
    fid = NAME_TO_CLASSIC.get(clean_name(name))
    if fid is not None:
        return QZONE_PATTERN.format(fid + 100)
    return None


def cached_path(key: str):
    """缓存里该表情的图片路径（任意支持的扩展名），没有则 None"""
    if not key:
        return None
    for ext in _IMAGE_EXT:
        path = os.path.join(FACE_CACHE_DIR, key + ext)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def local_pack_path(key: str, name: str = "", local_dir: str = ""):
    """在本地表情包目录里找同名/同编号的图片（离线来源，优先于联网）"""
    root = (local_dir or os.getenv("QQCHAT_FACE_DIR", "") or "").strip()
    if not root or not os.path.isdir(root):
        return None
    clean = clean_name(name)
    candidates = [c for c in (key, clean) if c]
    fid = NAME_TO_CLASSIC.get(clean)
    if fid is not None:
        candidates += [str(fid), f"e{fid + 100}"]
    root_abs = os.path.abspath(root)
    for base in candidates:
        for ext in _IMAGE_EXT:
            path = os.path.join(root_abs, base + ext)
            if not os.path.isfile(path):
                continue
            # 表情名来自导出文件（可被构造），确认没被 ../ 带出表情包目录
            try:
                if os.path.commonpath([root_abs, os.path.abspath(path)]) == root_abs:
                    return path
            except ValueError:
                continue
    return None


def _looks_like_image(data: bytes) -> bool:
    return any(data.startswith(sig) for sig in _MAGIC)


def _download(url: str):
    """下载一张表情图；任何失败都返回 None（离线时静默跳过，不抛异常）"""
    if not _url_allowed(url):
        logger.info("跳过非白名单地址的表情图: %s", url[:60])
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "qqchatlog/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=FACE_FETCH_TIMEOUT) as resp:
            data = resp.read(512 * 1024)
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.info("表情图下载失败（跳过）: %s (%s)", url[:60], type(e).__name__)
        return None
    if not data or not _looks_like_image(data):
        return None
    return data


def _store(key: str, data: bytes) -> str:
    ext = ".gif"
    if data.startswith(b"\x89PNG"):
        ext = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif data.startswith(b"RIFF"):
        ext = ".webp"
    path = os.path.join(cache_dir(), key + ext)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def ensure(faces: dict, allow_network: bool = True, max_seconds: float = 0.0) -> dict:
    """确保这些表情有本地图片，返回 {表情名: {"key","path","source"}}。

    faces: {表情名: {"market_url": str|None}}（键由 key_for 统一决定）
    已有缓存的直接用；本地表情包目录优先；联网失败逐条跳过。

    max_seconds > 0 时限制本次联网抓取的墙钟时长：抓取是同步跑在请求线程里的，
    上限 300 张 × 单张超时 6s 理论上能挂住半小时，用户只能看着转圈。
    到点就收工，剩下的留给下次点击（缓存已落盘，下次自然接着抓）。
    """
    if not enabled():
        return {}
    found: dict = {}
    pending = []
    for name, info in (faces or {}).items():
        key = info.get("key") or key_for(name, info.get("market_url") or "")
        if not key:
            continue
        path = cached_path(key) or local_pack_path(key, name)
        if path:
            found[name] = {"key": key, "path": path, "source": "cache"}
        else:
            pending.append((name, info, key))

    if pending and allow_network:
        fetched = 0
        deadline = (time.monotonic() + max_seconds) if max_seconds > 0 else None
        queue = pending[:FACE_FETCH_LIMIT]
        for idx, (name, info, key) in enumerate(queue):
            if deadline is not None and time.monotonic() > deadline:
                logger.info("表情图抓取已达单次时长上限（%.0fs），剩余 %d 个留到下次",
                            max_seconds, len(queue) - idx)
                break
            url = url_for(name, info.get("market_url") or "")
            if not url:
                continue                      # 超级表情：没有公开地址，保持 emoji/文字
            try:
                data = _download(url)
                path = _store(key, data) if data else None
            except Exception as e:            # 任何意外（网络/磁盘/编码）都只跳过这一张
                logger.info("表情图获取异常（跳过）: %s (%s)", type(e).__name__, e)
                continue
            if not path:
                continue
            found[name] = {"key": key, "path": path, "source": "network"}
            fetched += 1
            time.sleep(0.05)                  # 稍微客气一点，别把 CDN 打急了
        if fetched:
            logger.info("表情图：本次联网获取 %d 张，累计缓存 %d 个文件",
                        fetched, len(os.listdir(FACE_CACHE_DIR)))
    return found


def collect(chat, limit: int = 400) -> dict:
    """从聊天数据里收集用到的表情 → {表情名: {"key","market_url"}}"""
    faces: dict = {}
    for msg in getattr(chat, "messages", []):
        # 商城表情的 CDN 地址存在独立字段里（同一消息可能同时有图片）
        market_url = getattr(msg, "face_url", "") or ""
        for name in (msg.face_names or []):
            if name in faces:
                continue
            key = key_for(name, market_url)
            if not key:
                continue
            faces[name] = {"key": key, "market_url": market_url or None}
        if len(faces) >= limit:
            break
    return faces


_COLLECT_MEMO: dict = {}


def collect_cached(chat, chat_hash: str, limit: int = 400) -> dict:
    """collect 的进程内缓存版：习惯页与报告页都会用到，不必每页重扫 数万条消息"""
    if not chat_hash:
        return collect(chat, limit=limit)
    hit = _COLLECT_MEMO.get(chat_hash)
    if hit is None:
        hit = collect(chat, limit=limit)
        if len(_COLLECT_MEMO) > 32:
            _COLLECT_MEMO.clear()
        _COLLECT_MEMO[chat_hash] = hit
    return hit


def url_map(faces: dict) -> dict:
    """{表情名: /face/<key>}，供模板传给前端渲染真图"""
    return {name: f"/face/{info['key']}" for name, info in (faces or {}).items() if info.get("key")}


def serve_path(key: str):
    """/face/<key> 路由用：只允许缓存目录里的文件（键是受控格式，无穿越空间）"""
    if not key or len(key) > 40 or not key.replace("-", "").replace("_", "").isalnum():
        return None
    return cached_path(key)
