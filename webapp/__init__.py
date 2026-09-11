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
"""webapp 包 —— 从原 app.py 巨石拆出的各层

security  CSRF/Origin/登录限流        （原 app.py 的"访问防护"段）
store     哈希/ChatData 复用/统计与 AI 缓存（原"缓存与统计"段）
cleanup   临时文件与日志的生命周期回收    （原"清理"段 + 新增 before_request 触发）
jobs      后台分析任务表与执行体          （原"AI 分析 API"的任务段）
views     页面路由                        （原"页面路由"段）
api       /api/* 路由                     （原"AI 分析 API"的路由段）

app.py 保留为组装入口，并把历史测试用到的符号重导出为别名。
"""
