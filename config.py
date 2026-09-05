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
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# 应用配置
BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
SESSION_FILE_DIR = os.path.join(BASE_DIR, "flask_session")
AI_CACHE_DIR = os.path.join(BASE_DIR, "ai_cache")  # AI 分析结果缓存（含敏感内容，勿提交/定期清理）
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB


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
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000") or 5000)

# 访问口令：设置后所有页面需先登录；绑定非回环地址时强制要求（否则拒绝启动）
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "").strip()
