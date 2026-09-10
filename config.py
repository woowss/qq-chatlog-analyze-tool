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

# 数据目录：默认在项目内，可用 QQCHAT_DATA_DIR 整体迁移（测试/多实例友好），
# 也可用 UPLOAD_DIR / SESSION_DIR / AI_CACHE_DIR 单独覆盖。
DATA_DIR = Path(os.getenv("QQCHAT_DATA_DIR", "").strip() or BASE_DIR)
UPLOAD_FOLDER = os.getenv("UPLOAD_DIR", "").strip() or str(DATA_DIR / "uploads")
SESSION_FILE_DIR = os.getenv("SESSION_DIR", "").strip() or str(DATA_DIR / "flask_session")
AI_CACHE_DIR = os.getenv("AI_CACHE_DIR", "").strip() or str(DATA_DIR / "ai_cache")
STATS_CACHE_DIR = os.getenv("STATS_CACHE_DIR", "").strip() or str(DATA_DIR / "stats_cache")
TOKEN_USAGE_FILE = os.getenv("TOKEN_USAGE_FILE", "").strip() or str(DATA_DIR / "logs" / "token_usage.json")
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB

# 月份级增量缓存开关（默认开）：重新导出同一段对话时只为新增月份付费。
# 关掉后行为回到"整份文件哈希"的维度级缓存。
MONTH_CACHE_ENABLED = os.getenv("QQCHAT_MONTH_CACHE", "true").strip().lower() not in (
    "0", "false", "no", "off")


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


def _load_or_create_secret() -> str:
    """读取持久化的 SECRET_KEY；首次运行生成并落盘，保证重启后 session 仍有效。

    优先使用环境变量 SECRET_KEY，其次 `.secret_key` 文件（该文件已被 .gitignore 忽略）。
    """
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_file = BASE_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    key = os.urandom(24).hex()
    try:
        key_file.write_text(key, encoding="utf-8")
    except OSError:
        pass  # 写失败时退化为本次进程内随机值
    return key


SECRET_KEY = _load_or_create_secret()

# 调试模式开关：默认关闭（避免暴露 Werkzeug 调试器导致任意代码执行风险），
# 本地开发时可设环境变量 FLASK_DEBUG=true 开启自动重载
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").strip().lower() in ("1", "true", "yes", "on")

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
