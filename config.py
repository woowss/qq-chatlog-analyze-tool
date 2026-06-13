# Copyright (C) 2026 Zhang Yangming. All Rights Reserved.
# Licensed under MIT License.
# SPDX-License-Identifier: MIT
#
"""应用配置：从 .env 读取 DeepSeek API 配置"""
import os
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# 应用配置
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
SECRET_KEY = os.urandom(24).hex()
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
