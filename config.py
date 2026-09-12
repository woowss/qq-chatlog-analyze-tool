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
"""应用配置：从 .env 读取 DeepSeek API 配置"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
# 官方当前模型：deepseek-flash（V4.1-Flash）/ deepseek-v4-pro；
# 旧名 deepseek-chat 仍可调用但会被路由到 Flash，这里直接用真名，避免误导
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# 应用配置
BASE_DIR = Path(__file__).parent


def _is_source_checkout() -> bool:
    """当前是"源码检出"还是"pip 安装后的 site-packages"

    用构建元数据判断：源码检出里 config.py 旁边有 pyproject.toml 与 app.py；
    wheel 装出来的目录只有 .py 文件与 dist-info。两者的数据目录默认值不同——
    源码跑沿用历史行为（数据在仓库内，README/测试脚本都这么描述），
    安装后不能往 site-packages 写 uploads/ai_cache（可能只读，升级/卸载还会丢数据）。
    """
    return (BASE_DIR / "pyproject.toml").is_file() and (BASE_DIR / "app.py").is_file()


def _user_data_dir() -> Path:
    """安装态的用户级数据目录，按各平台惯例取值（不为这点事引入 platformdirs 依赖）"""
    if os.name == "nt":
        root = os.getenv("LOCALAPPDATA", "").strip() or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = os.getenv("XDG_DATA_HOME", "").strip() or Path.home() / ".local" / "share"
    return Path(root) / "qqchatlog"


# 数据目录：源码运行时默认在项目内（历史行为）；pip 安装后落到用户数据目录。
# 都可用 QQCHAT_DATA_DIR 整体迁移（测试/多实例友好），
# 也可用 UPLOAD_DIR / SESSION_DIR / AI_CACHE_DIR 单独覆盖。
DEFAULT_DATA_DIR = BASE_DIR if _is_source_checkout() else _user_data_dir()
DATA_DIR = Path(os.getenv("QQCHAT_DATA_DIR", "").strip() or DEFAULT_DATA_DIR)
UPLOAD_FOLDER = os.getenv("UPLOAD_DIR", "").strip() or str(DATA_DIR / "uploads")
SESSION_FILE_DIR = os.getenv("SESSION_DIR", "").strip() or str(DATA_DIR / "flask_session")
AI_CACHE_DIR = os.getenv("AI_CACHE_DIR", "").strip() or str(DATA_DIR / "ai_cache")
STATS_CACHE_DIR = os.getenv("STATS_CACHE_DIR", "").strip() or str(DATA_DIR / "stats_cache")
# 日志目录此前固定写在项目内（logger.py 自算路径），QQCHAT_DATA_DIR 迁移时会被漏下——
# 现在统一归入数据目录，测试进程也不再往真实 logs/ 里写。
LOG_DIR = os.getenv("LOG_DIR", "").strip() or str(DATA_DIR / "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")
TOKEN_USAGE_FILE = os.getenv("TOKEN_USAGE_FILE", "").strip() or str(DATA_DIR / "logs" / "token_usage.json")

def _env_int(name: str, default: int, low: int, high: int) -> int:
    """读取整型环境变量：非法值不再让应用崩在 import 阶段，而是回退默认值并提示"""
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[WARN] {name}={raw!r} 不是整数，已回退为 {default}", file=sys.stderr)
        return default
    if not low <= value <= high:
        print(f"[WARN] {name}={value} 超出范围 [{low}, {high}]，已回退为 {default}", file=sys.stderr)
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    """布尔环境变量：留空取默认值；任何写法都归约为真/假，不会崩"""
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# 单次上传体积上限（MB）：超长聊天（实测 数万条私聊）导出的 JSON 会逼近 50MB，
# 撞上限制时 Flask 直接回 413，用户只看到"上传失败"却不知为何，所以留一个可调口子。
MAX_CONTENT_LENGTH = _env_int("QQCHAT_MAX_UPLOAD_MB", 50, 1, 4096) * 1024 * 1024


# 日志隐私闭环：按天轮转并只保留 N 天（轮转文件由 handler 自行删除，
# 历史遗留的 5×5MB 式 app.log.1/.2 由启动/定期清理按时间回收）。
LOG_RETENTION_DAYS = _env_int("LOG_RETENTION_DAYS", 7, 1, 90)
# 日志里的昵称/原始文件名默认脱敏：昵称与导出文件名常含真实称呼，属于敏感数据；
# 本地排查问题时可在 .env 设 LOG_REDACT_NAMES=false 恢复原文。
LOG_REDACT_NAMES = _env_bool("LOG_REDACT_NAMES", True)
# 内存任务表条目的存活时间（结果早已落盘缓存，内存只服务轮询）
JOB_TTL_SECONDS = _env_int("QQCHAT_JOB_TTL_SECONDS", 900, 60, 86400)

# 月份级增量缓存开关（默认开）：重新导出同一段对话时只为新增月份付费。
# 关掉后行为回到"整份文件哈希"的维度级缓存。
MONTH_CACHE_ENABLED = _env_bool("QQCHAT_MONTH_CACHE", True)

# ---------------------------------------------------------------------------
# 图片理解（视觉）：让模型"看"聊天里的截图/照片/表情包
# ---------------------------------------------------------------------------
# 媒体根目录：导出器把图片放在导出的 resources/ 下，而本工具只接收 JSON，
# 因此需要你告诉它资源在哪（通常就是导出目录本身，url 字段形如 resources/images/xx.jpg）。
# 留空 = 不做图片理解（其余功能完全不受影响）。
MEDIA_ROOT = os.getenv("QQCHAT_MEDIA_DIR", "").strip()
# 视觉开关：默认开（deepseek-flash 原生支持图片输入）。
# 关掉即完全不上传图片，回到纯文本分析。
VISION_ENABLED = _env_bool("LLM_VISION", True)
# 每月最多送几张图：每张最多 1024 tokens（官方按约 1300x1300 折算）。
# 准确性优先：默认 20 张（约 2 万 tokens/月，成本可忽略），能覆盖更多截图与表情包。
VISION_MAX_PER_MONTH = _env_int("LLM_VISION_MAX_PER_MONTH", 20, 0, 50)
# 送图清晰度：high 保留原图（截图里的字才看得清）；low 压到 512x512（更省 token）
VISION_DETAIL = (os.getenv("LLM_VISION_DETAIL", "high").strip().lower() or "high")
# 太小的图基本是表情包/缩略图，跳过以省 token（按最长边像素判断）
VISION_MIN_SIDE = _env_int("LLM_VISION_MIN_SIDE", 200, 0, 4000)
# 单张图片体积上限（官方 base64 上限 32 MiB，这里留一半余量）
VISION_MAX_BYTES = _env_int("LLM_VISION_MAX_BYTES", 12 * 1024 * 1024, 65536, 32 * 1024 * 1024)
# 一次摘要请求的图片总体积上限：官方请求体上限 48 MiB，而 base64 会膨胀约 1/3，
# 所以原始字节控制在 32 MiB 以内（图片按顺序贪心装入，装不下的留到下次）
VISION_MAX_TOTAL_BYTES = _env_int("LLM_VISION_MAX_TOTAL_BYTES", 32 * 1024 * 1024,
                                  1024 * 1024, 32 * 1024 * 1024)

# ---------------------------------------------------------------------------
# 表情图（可选，默认关）：把 QQ 表情的原始图片缓存到本地，界面直接显示真表情
# ---------------------------------------------------------------------------
# 关闭时界面用 Unicode emoji / 表情名渲染（完全离线、零外部请求）。
# 打开后可手动触发一次抓取：经典黄脸走 Qzone 的公开表情 CDN，商城表情用导出文件
# 自带的地址；QQ 超级表情（吃糖/大怨种…）没有公开地址，只能靠本地已有的表情包文件。
FACE_CACHE_DIR = os.getenv("FACE_CACHE_DIR", "").strip() or str(DATA_DIR / "face_cache")
FACE_IMAGES_ENABLED = _env_bool("QQCHAT_FACE_IMAGES", False)
# 一次抓取的上限与超时：失败逐条跳过，抓不到就继续用 emoji/文字
FACE_FETCH_LIMIT = _env_int("QQCHAT_FACE_FETCH_LIMIT", 300, 1, 2000)
FACE_FETCH_TIMEOUT = _env_int("QQCHAT_FACE_FETCH_TIMEOUT", 6, 1, 60)


def _load_or_create_secret() -> str:
    """读取持久化的 SECRET_KEY；首次运行生成并落盘，保证重启后 session 仍有效。

    查找顺序：环境变量 SECRET_KEY → 数据目录下的 `.secret_key` → 项目根目录的
    `.secret_key`（旧位置，读到即迁移到数据目录）。密钥跟着数据走，
    这样 QQCHAT_DATA_DIR 迁到别处时不会出现"数据搬了、密钥留在原地"的错位。
    """
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    target = DATA_DIR / ".secret_key"
    legacy = BASE_DIR / ".secret_key"
    candidates = [target] if legacy == target else [target, legacy]
    for index, path in enumerate(candidates):
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not key:
            continue
        if index > 0:                     # 旧位置的密钥：顺手迁到数据目录
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                target.write_text(key, encoding="utf-8")
            except OSError:
                pass
        return key
    key = os.urandom(24).hex()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        target.write_text(key, encoding="utf-8")
    except OSError:
        pass  # 写失败时退化为本次进程内随机值
    return key


SECRET_KEY = _load_or_create_secret()

# 调试模式开关：默认关闭（避免暴露 Werkzeug 调试器导致任意代码执行风险），
# 本地开发时可设环境变量 FLASK_DEBUG=true 开启自动重载
FLASK_DEBUG = _env_bool("FLASK_DEBUG", False)

# 绑定地址与端口：默认仅本机。Windows 上 5000 常被 AirPlay/Hyper-V 占用，可改 FLASK_PORT
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1").strip() or "127.0.0.1"
FLASK_PORT = _env_int("FLASK_PORT", 5000, 1, 65535)

# 访问口令：设置后所有页面需先登录；绑定非回环地址时强制要求（否则拒绝启动）
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "").strip()

# 额外允许的浏览器来源主机名（逗号分隔），用于局域网/自定义域名访问。
# POST 的 Origin 校验默认只放行回环地址与 FLASK_HOST（不再信任请求自带的 Host，
# 否则 DNS rebinding 场景下 "Origin == Host" 会让校验形同虚设）。
# 例：ALLOWED_ORIGINS=192.168.1.5,chat.lan
ALLOWED_ORIGINS = frozenset(
    h.strip().lower()
    for h in (os.getenv("ALLOWED_ORIGINS", "") or "").split(",")
    if h.strip()
)
