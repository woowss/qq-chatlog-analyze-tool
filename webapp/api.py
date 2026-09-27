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
"""AI 分析 API：发起/轮询/取消、结果缓存读取、用量与状态"""

import io
import os
import threading
import time

from flask import jsonify, request, send_file, session

from analyzer.deepseek_client import QuotaExhaustedError, is_api_configured, test_connection
from analyzer.logger import get_logger
from analyzer.usage import get_usage
from config import UPLOAD_FOLDER
from webapp import store
from webapp.jobs import (
    ALL_DIMENSION_NAMES,
    JOBS,
    analyze_func_for,
    dimensions_for_mode,
    is_group_dimension,
    is_private_only_dimension,
    JOBS_LOCK,
    _get_or_create_job,
    _prune_jobs,
    _run_analyze_all,
    _run_job,
    _session_chat_file,
)
from webapp.security import _guard_post

logger = get_logger("app")

# 表情图抓取是同步且耗时的操作，加锁避免连点触发重复下载
_FACE_FETCH_LOCK = threading.Lock()


def _dimension_guard(dimension: str):
    """维度校验：必须存在于总表，且与当前会话的模式匹配。

    私聊文件跑群聊维度（或反之）不只是"没意义"——群聊维度会把"我 vs 对方"的数字
    当成群的数字讲给模型听，产出看着像结论、其实口径错位的东西。所以直接拒绝，
    并明确告诉用户当前是什么模式。

    recap 需要单独一条：`is_group_dimension()` 按"在不在群聊表里"判断，
    而 recap 既不在群聊表也不在私聊的 ANALYZE_FUNCS 表里（它是第三套独立注册表
    RECAP_DIMENSIONS），于是它被判成"非群聊维度"，群聊会话 `is_group=True` 与之
    相等 → 守卫**放行**。口径归属见 jobs.is_private_only_dimension。
    """
    if analyze_func_for(dimension) is None:
        return jsonify({"error": f"未知维度: {dimension}"}), 400
    is_group = session.get("chat_mode") == "group"
    if is_group and is_private_only_dimension(dimension):
        label = ALL_DIMENSION_NAMES.get(dimension, dimension)
        return jsonify({"error": f"「{label}」不适用于当前记录（这是群聊记录）"}), 400
    if is_group_dimension(dimension) != is_group:
        want = "群聊" if is_group else "私聊"
        label = ALL_DIMENSION_NAMES.get(dimension, dimension)
        return jsonify({"error": f"「{label}」不适用于当前记录（这是{want}记录）"}), 400
    return None


def api_analyze(dimension: str):
    """发起维度分析：命中缓存直接返回，否则启动后台任务并返回 job id"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    bad = _dimension_guard(dimension)
    if bad:
        return bad

    if not is_api_configured():
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400

    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]

    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)

    # 缓存命中（除非显式 refresh=1 强制重跑）
    if request.args.get("refresh") != "1":
        cached = store._read_cache(dimension, chat_hash)
        if cached is not None:
            logger.info("%s 命中缓存，直接返回", ALL_DIMENSION_NAMES.get(dimension, dimension))
            return jsonify({"cached": True, "result": cached})

    _prune_jobs()
    # 全量任务已覆盖本维度：拒绝而不是另起一个任务（否则同一维度会被分析两遍、双倍计费）
    job_id, reused, conflict = _get_or_create_job(
        session.sid, dimension, chat_hash, total=0, conflict_dimension="all"
    )
    if conflict:
        logger.info("已有全量任务在运行，拒绝重复启动 %s", dimension)
        return jsonify({"error": "一键全量分析正在运行，请等它完成或先取消（避免重复调用 API）"}), 409
    if reused:
        logger.info("复用进行中的 %s 任务 %s", dimension, job_id[:8])
        return jsonify({"job": job_id, "reused": True})

    threading.Thread(target=_run_job, args=(job_id, dimension, filepath, chat_hash), daemon=True).start()
    return jsonify({"job": job_id})


def api_analyze_job(job_id: str):
    """查询后台任务进度；完成时附带结果"""
    # 轮询是最高频的 API 入口，顺手在这里修剪过期任务：
    # 只在新任务启动时修剪的话，"跑完就不再发起分析"的进程会把结果一直挂在内存里
    _prune_jobs()
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j.get("sid") != session.sid:
            return jsonify({"error": "任务不存在"}), 404
        resp = {"status": j["status"], "done": j["done"], "total": j["total"]}
        if j.get("detail"):
            resp["detail"] = j["detail"]
        if j["status"] == "done":
            resp["result"] = j["result"]
        elif j["status"] == "error":
            resp["error"] = j.get("error", "未知错误")
    return jsonify(resp)


def api_analyze_cancel(job_id: str):
    """请求取消后台任务（协作式：已发出的 API 请求会自然结束，结果丢弃）"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j.get("sid") != session.sid:
            return jsonify({"error": "任务不存在"}), 404
        j["cancel"] = True
    return jsonify({"status": "cancelling"})


def api_analysis_result(dimension: str):
    """读取已缓存的分析结果（页面加载时优先于 sessionStorage 使用）"""
    bad = _dimension_guard(dimension)
    if bad:
        return bad
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    cached = store._read_cache(dimension, session.get("chat_hash") or store._chat_hash(filepath))
    if cached is None:
        return jsonify({"error": "暂无该维度的分析结果"}), 404
    return jsonify({"cached": True, "result": cached})


def api_analyze_all():
    """一键全量分析：五个维度顺序跑完，进度按维度汇报"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    if not is_api_configured():
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400

    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]

    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    refresh = request.args.get("refresh") == "1"
    is_group = session.get("chat_mode") == "group"

    _prune_jobs()
    job_id, reused, conflict = _get_or_create_job(
        session.sid, "all", chat_hash, total=len(dimensions_for_mode(is_group)), conflict_dimension="*"
    )
    if reused:
        logger.info("复用进行中的一键全量任务 %s", job_id[:8])
        return jsonify({"job": job_id, "reused": True})
    if conflict:
        # 已有单维度任务在跑：全量任务会把这些维度再跑一遍（重复计费），先拒绝
        logger.info("已有 %s 任务在运行，拒绝启动全量分析", conflict)
        return jsonify(
            {
                "error": f"已有「{ALL_DIMENSION_NAMES.get(conflict, conflict)}」任务在运行，"
                "请等它完成或先取消（避免重复调用 API）"
            }
        ), 409

    threading.Thread(
        target=_run_analyze_all, args=(job_id, filepath, chat_hash, refresh, is_group), daemon=True
    ).start()
    return jsonify({"job": job_id})


def api_faces_fetch():
    """抓取表情原图到本地缓存（可选功能；只在用户点击时联网）。

    抓不到就如实报告：离线、404、超时都会被逐条跳过，界面继续用 emoji/文字渲染。
    抓取是同步且要几十秒的，用一把锁避免连点造成重复下载。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    from analyzer import face_images

    if not face_images.enabled():
        return jsonify({"error": "表情图功能未开启：请在 .env 中设置 QQCHAT_FACE_IMAGES=true"}), 403

    if not _FACE_FETCH_LOCK.acquire(blocking=False):
        return jsonify({"error": "正在抓取中，请等这次结束"}), 409
    try:
        filepath = session.get("filepath")
        if not filepath or not os.path.exists(filepath):
            return jsonify({"error": "请先上传聊天记录"}), 400
        chat_hash = session.get("chat_hash", "")
        chat = store._load_chat_cached(filepath)
        faces = face_images.collect_cached(chat, chat_hash)
        total = len(faces)
        before = len(face_images.url_map(face_images.ensure(faces, allow_network=False)))
        # 带上时长上限：抓取同步跑在请求线程里，不设预算时 300 张 × 6s 超时能挂住半小时
        have = face_images.ensure(faces, allow_network=True, max_seconds=face_images.FETCH_BUDGET_SECONDS)
        after = len(face_images.url_map(have))
        return jsonify(
            {
                "total": total,
                "available": after,
                "fetched": max(0, after - before),
                # 仍未拿到图的（超级表情 + 本次没抓完的），前端据此如实提示
                "pending": max(0, total - after),
                "budget_seconds": face_images.FETCH_BUDGET_SECONDS,
            }
        )
    finally:
        _FACE_FETCH_LOCK.release()


def api_media_upload():
    """接收 WebUI 上传的图片副本（只挑"看图需要的那几十张"）。

    安全与容量约束：
    - 只接受图片扩展名，按 basename 平铺存到 uploads/media/<chat_hash>/，
      彻底规避 ../ 之类的路径穿越（导出器的文件名自带 md5 前缀，重名极罕见）；
    - 单文件大小与总量都有上限，超限跳过而不是写爆磁盘；
    - chat_hash 必须与当前会话一致，防止往别人的目录里塞文件。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    from analyzer import vision

    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    # 前端会上报它认为的 chat_hash：不一致说明页面还停在旧会话上（用户中途换过文件），
    # 这时接收图片副本只会写进一个已经没人引用的目录，所以直接拒绝。
    # 不传该字段是允许的（老客户端/脚本），此时以 session 里的哈希为准。
    form_hash = (request.form.get("chat_hash") or "").strip()
    if form_hash and form_hash != chat_hash:
        return jsonify({"error": "会话与文件不匹配，请刷新页面重新上传"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"saved": 0, "skipped": 0})

    target_dir = vision.session_media_dir(chat_hash)
    os.makedirs(target_dir, exist_ok=True)
    saved = skipped = 0
    total_bytes = 0
    for fs in files[: vision.MEDIA_UPLOAD_MAX_FILES]:
        name = os.path.basename((fs.filename or "").replace("\\", "/"))
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in vision.SUPPORTED_EXT:
            skipped += 1
            continue
        data = fs.read(vision.VISION_MAX_BYTES + 1)
        if not data or len(data) > vision.VISION_MAX_BYTES:
            skipped += 1
            continue
        if total_bytes + len(data) > vision.MEDIA_UPLOAD_MAX_BYTES:
            skipped += 1
            continue
        path = os.path.join(target_dir, name)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            logger.warning("图片副本写入失败 %s: %s", name, e)
            skipped += 1
            continue
        total_bytes += len(data)
        saved += 1
    if saved:
        logger.info("已接收 %d 张图片副本（%.1f MB），供图片理解使用", saved, total_bytes / 1048576)
    return jsonify({"saved": saved, "skipped": skipped, "bytes": total_bytes})


def api_usage():
    """LLM token 用量统计（按天 × 维度聚合，仅数字无聊天内容）"""
    return jsonify(get_usage())


def api_status():
    """API 配置状态"""
    status = is_api_configured()
    logger.debug("API 状态查询: %s", "已配置" if status else "未配置")
    return jsonify({"api_ok": status})


def api_status_test():
    """连通性实测：真实发一个 1-token 请求，把端点/Key/模型名的错误当场说清。

    POST 而不是 GET：它会打一次网络请求、可能产生费用（1 token），不能被
    预取或爬虫触发；错误详情原样回传（Key 永远不会出现在里面）。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    ok, detail = test_connection()
    return jsonify({"ok": ok, "detail": detail})


def api_chat_delete():
    """主动删除当前聊天：上传原件 + 全部派生缓存（统计/维度/月份/图片摘要/图片副本）。

    此前级联清理的唯一触发点是"换一份新文件"——想"看完就清、不留隐私"只能先随便
    传个文件顶一下，或手删目录再重启。清理逻辑复用与换文件同一口径：先松开本会话
    引用，仍有其他会话在用这份内容时**不**删它们共享的已付费结果（与换文件路径一致）。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "当前会话没有聊天记录"}), 400
    sid = getattr(session, "sid", "") or ""
    removed = 0
    filepath = session.get("filepath") or ""
    # 只回收本工具写进 uploads/ 的副本（与换文件路径同一条守卫）
    if filepath and os.path.dirname(filepath) == UPLOAD_FOLDER and os.path.exists(filepath):
        try:
            os.remove(filepath)
            removed += 1
        except OSError as e:
            logger.warning("上传副本删除失败（可能仍残留敏感内容）: %s", e)
    store.forget_live_chat(chat_hash, sid)
    # 显式删除：额外核对会话后端，别让"没点退出就关掉的浏览器"留下幽灵引用把清理挡住
    shared = store.other_live_sessions(chat_hash, exclude_sid=sid, require_live_session=True)
    if shared:
        logger.info("该聊天仍被其它 %d 个会话使用，本次仅解除本会话引用", len(shared))
    else:
        removed += store._purge_chat_caches(chat_hash)
    for key in (
        "chat_hash",
        "filepath",
        "chat_name",
        "self_name",
        "other_name",
        "total_messages",
        "chat_mode",
        "overview",  # 旧会话兼容键
    ):
        session.pop(key, None)
    return jsonify({"ok": True, "removed": removed, "kept_for_other_sessions": bool(shared)})


def api_export_chat():
    """导出当前聊天的统计 + 已付费 AI 结果为 zip（备份/换机器迁移）。

    月份缓存按 manifest 一起打包——只带维度文件的话，新机器重跑会整月重新付费。
    GET 无副作用；文件经会话鉴权与 SameSite=Lax 双重保护（跨站 <img> 拿不到 cookie）。
    """
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    buf = io.BytesIO()
    count = store.write_chat_bundle(chat_hash, buf)
    if not count:
        return jsonify({"error": "该聊天还没有可导出的缓存（统计尚未算好？）"}), 404
    buf.seek(0)
    from datetime import datetime

    name = f"qqchatlog_export_{chat_hash[:8]}_{datetime.now():%Y%m%d-%H%M}.zip"
    logger.info("导出 %d 个结果文件（聊天 %s…）", count, chat_hash[:8])
    return send_file(buf, as_attachment=True, download_name=name, mimetype="application/zip")


def api_import_chat():
    """导入本工具导出的结果包：恢复已付费分析（跨机器不换血）。

    只按文件名落回 ai_cache/ 与 stats_cache/ 两个目录，条目路径一律不信；
    导入后若当前会话还没有聊天、而包里 meta 的 chat_hash 与随后上传的内容一致，
    分析会直接命中——这正是"迁移不重复付费"的完整闭环。

    两道判定（见 store.read_chat_bundle）：
    - **归属**：包里每个条目必须属于 meta 声明的那份聊天。少了这条，一个不含任何
      聊天数据的 zip 就能把 `stats_<你当前聊天>.json` 写成任意内容，导入后被当作
      真实统计与"已付费 AI 结论"渲染——本工具"结论可回溯到本地事实"的立论基础；
    - **确认**：包声明的正是当前会话打开的这份聊天时先要一次二次确认（409 +
      need_confirm）。换机器恢复是合法用法，但那也是"冒充当前聊天结果"最省事的路，
      所以让用户点两下，而不是静默覆盖正在看的数据。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    file = request.files.get("file")
    if file is None or not (file.filename or "").lower().endswith(".zip"):
        return jsonify({"error": "请选择本工具导出的 .zip 结果包"}), 400
    form = request.form
    confirm = str(form.get("confirm") or "") in ("1", "true", "on")
    out = store.read_chat_bundle(
        file.stream, confirm_overwrite=confirm, live_hash=session.get("chat_hash") or ""
    )
    if out.get("need_confirm"):
        return jsonify(out), 409
    if "error" in out:
        return jsonify({"error": out["error"]}), 400
    logger.info("导入结果包：写入 %d、跳过 %d", out["written"], out["skipped"])
    return jsonify({"ok": True, **out})


def _browse_body(m) -> str:
    """浏览视图下的"这条消息说了什么"：正文优先，空正文时由媒体/表情占位补出。

    同一份拼法同时供**展示**与**搜索**用（口径唯一）：此前搜索只查 m.text 的话，
    纯图片/纯语音这类"没有正文但有内容"的消息就永远搜不到——而它们在界面上
    明明显示着 [图片]/[语音] 标签，症状是"看得见、搜不着"。
    """
    if m.text:
        return m.text
    bits = []
    if m.media_kind:
        from parser.qq_parser import MEDIA_KINDS

        kind = MEDIA_KINDS.get(m.media_kind, m.media_kind)
        bits.append(f"[{kind}{':' + m.media_label if m.media_label else ''}]")
    if m.has_image:
        bits.append(f"[图片×{m.image_count or 1}]")
    if m.face_names:
        bits.append("[表情:" + "、".join(m.face_names) + "]")
    if m.face_ids:
        bits.append(f"[表情×{len(m.face_ids)}]")
    return " ".join(bits)


def _fmt_browse_message(m, self_uid: str) -> dict:
    """单条消息 → 浏览视图（原文浏览是"如实呈现"：撤回与系统消息也列出，打标而非隐藏）。

    正文与媒体口径沿用解析结果：text 不含占位符（那是统计口径的干净设计），
    界面上用 media_label / 表情名补出"这条消息里发生过什么"。
    """
    return {
        "id": m.id,
        "time": m.time_str,
        "name": m.sender_name,
        "uid": m.sender_uid,
        "is_self": bool(self_uid) and m.sender_uid == self_uid,
        "text": _browse_body(m),
        "is_reply": m.is_reply,
        "recalled": m.recalled,
        "system": m.system,
        "mentions_all": m.mentions_all,
    }


def _match_browse(m, q: str, side: str, month: str, dt_from: str, dt_to: str, self_uid: str) -> bool:
    """过滤谓词：一条消息是否进结果集。q 匹配正文、媒体标签与表情名（大小写不敏感）。"""
    if month and not m.time_str.startswith(month):
        return False
    if dt_from and m.time_str[:10] < dt_from:
        return False
    if dt_to and m.time_str[:10] > dt_to:
        return False
    if side == "self" and m.sender_uid != self_uid:
        return False
    if side == "other" and m.sender_uid == self_uid:
        return False
    if side and side != "self" and side != "other" and m.sender_uid != side:
        # 群聊按成员 uid 过滤（前端下拉传的就是 uid）；与 self/other 两条分支互不冲突
        return False
    if q:
        hay = " ".join(
            x for x in (_browse_body(m), m.media_label, " ".join(m.face_names), m.sender_name) if x
        ).lower()
        if q.lower() not in hay:
            return False
    return True


def api_messages():
    """原始消息浏览与搜索（数据全在本机内存/磁盘，零 API 成本）。

    参数：q 关键词 | sender（self/other 或群成员 uid）| month=YYYY-MM |
    from/to=YYYY-MM-DD | page（1 起）| per_page（≤200）| around=消息 id（定位上下文，
    忽略分页，返回其前后各 ~10 条）。
    这是"AI 结论可回溯"的入口：锐评引用了某句话，用户从此能搜到原文看上下文。
    """
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    try:
        chat = store._load_chat_cached(filepath)
    except Exception as e:  # noqa: BLE001 —— 坏文件当场说清，不抛 500 堆栈
        return jsonify({"error": f"读取聊天记录失败: {e}"}), 500

    args = request.args
    q = (args.get("q") or "").strip()
    sender = (args.get("sender") or "").strip()
    month = (args.get("month") or "").strip()
    dt_from = (args.get("from") or "").strip()
    dt_to = (args.get("to") or "").strip()
    try:
        page = max(1, int(args.get("page") or 1))
        # 下限 1（不是 10）：客户端要小页（分页预览、调试）时，钳到 10 会让"第 2 页"
        # 因 lo 超过总数而翻出空列表——尊重合理的小 per_page，只卡上限。
        per_page = min(200, max(1, int(args.get("per_page") or 100)))
    except ValueError:
        return jsonify({"error": "page/per_page 必须是数字"}), 400

    matched = [m for m in chat.messages if _match_browse(m, q, sender, month, dt_from, dt_to, chat.self_uid)]
    total = len(matched)

    around = (args.get("around") or "").strip()
    if around:
        idx = next((i for i, m in enumerate(matched) if m.id == around), None)
        if idx is None:
            return jsonify({"error": "在当前位置的过滤条件下找不到该消息"}), 404
        lo = max(0, idx - 10)
        window = matched[lo : idx + 11]
        return jsonify(
            {
                "total": total,
                "around": around,
                "messages": [_fmt_browse_message(m, chat.self_uid) for m in window],
            }
        )

    lo = (page - 1) * per_page
    return jsonify(
        {
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
            "messages": [_fmt_browse_message(m, chat.self_uid) for m in matched[lo : lo + per_page]],
            "senders": [
                {"uid": p.uid, "name": p.name, "raw_name": p.raw_name, "is_self": p.is_self}
                for p in chat.participants()
            ],
            "chat_mode": session.get("chat_mode", "private"),
        }
    )


def api_job_history():
    """最近完成的分析任务列表（落盘账目，跨重启仍在）。只含维度/状态/计数——不含聊天内容。"""
    return jsonify({"history": store.read_job_history()})


#: 同题互斥：(chat_hash, 问题哈希) -> 持有该问题的 sid。
#: 提问的缓存是**调用返回之后**才写的（见 api_ask 结尾），所以两个并发相同问题
#: 都会读不到缓存、都去发一次真实调用 —— 用户点两下就是两次付费，正是本文件
#: _get_or_create_job 那条规则（"拒绝而不是另起一个任务，否则同一维度被分析两遍、
#: 双倍计费"）要防的事。提问走同步路径、进不了任务表，所以在这里补一道同口径的锁。
#: 带 TTL 兜底：持有者被硬杀时不许把这个问题永久锁死。
_ASK_IN_FLIGHT: dict = {}
_ASK_LOCK = threading.Lock()
_ASK_STALE_SECONDS = 600.0


def _ask_key_fingerprint(question: str) -> str:
    import hashlib

    return hashlib.sha1(question.strip().encode("utf-8")).hexdigest()[:16]


def _ask_acquire(chat_hash: str, question: str) -> tuple:
    """占用这个问题；返回 (token, busy_sid)。busy_sid 非空表示已有人在问同一题。"""
    sid = getattr(session, "sid", "") or ""
    key = (chat_hash, _ask_key_fingerprint(question))
    now = time.time()
    with _ASK_LOCK:
        holder = _ASK_IN_FLIGHT.get(key)
        if holder and now - holder[1] < _ASK_STALE_SECONDS:
            return None, holder[2]
        token = f"{sid}:{now}:{os.getpid()}"
        _ASK_IN_FLIGHT[key] = (token, now, sid)
        # 有界：只清过期项，避免长期跑下来这张表一直涨
        for k in [k for k, v in _ASK_IN_FLIGHT.items() if now - v[1] > _ASK_STALE_SECONDS]:
            _ASK_IN_FLIGHT.pop(k, None)
        return token, None


def _ask_release(chat_hash: str, question: str, token) -> None:
    key = (chat_hash, _ask_key_fingerprint(question))
    with _ASK_LOCK:
        holder = _ASK_IN_FLIGHT.get(key)
        # 只解自己占的那把：超时被别人接管后，迟到的持有者不许把别人的锁放掉
        if holder and holder[0] == token:
            _ASK_IN_FLIGHT.pop(key, None)


def _private_only_guard(label: str):
    """recap / ask 是私聊专用：群聊会话直接拒。

    这两族吃的同一份摘要（recap_client._recap_digest）结构性地是二人关系口径：
    头行写"参与者：{self_name}（我）与 {other_name}"，抽样对话里每一条非本人消息
    都被标成 other_name，而群聊文件里 other_name 是**群名**（parser 的兜底取值）。
    于是 N 个成员的话被合并成一个人格、"我 vs 大家"被讲成二人关系——正是
    _dimension_guard 文档里说的"产出看着像结论、其实口径错位"。
    /recap 页面早就按这个口径把群聊会话重定向走了（views.recap），ask 之前漏了，
    而且每漏一次就真花一次钱（约 15 万字符的输入）。
    """
    if session.get("chat_mode") == "group":
        return jsonify({"error": f"「{label}」不适用于当前记录（这是群聊记录）"}), 400
    return None


def api_ask():
    """自定义提问：一次同步调用 + 问题级缓存（重问同一题免费）。

    与维度不同，它是"一问一答"的即席请求：单次调用没有多月份进度要直播，
    同步返回比塞进任务表更诚实（前端转圈等待，超过一分钟属正常）。
    缓存文件名含问题哈希、落在同一 ai_cache/ 目录——级联清理、结果导出、
    保留期回收全部自动覆盖，不给"提问"单造一套生命周期。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    if not is_api_configured():
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    bad = _private_only_guard("提问角")
    if bad:
        return bad
    payload = request.get_json(silent=True) or request.form
    question = str(payload.get("question") or "").strip()
    if len(question) < 2:
        return jsonify({"error": "问题太短（至少 2 个字）"}), 400
    if len(question) > 300:
        return jsonify({"error": "问题过长（上限 300 字）——把大问题拆成两问，答案质量也更好"}), 400
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    refresh = str(payload.get("refresh") or "") in ("1", "true", "on")
    if not refresh:
        cached = store.ask_cache_read(chat_hash, question)
        if cached is not None:
            return jsonify({"cached": True, "result": cached})
    # 同题互斥：答案缓存是调用之后才写的，所以并发同题会各付一次钱（见 _acquire 说明）
    token, busy = _ask_acquire(chat_hash, question)
    if busy is not None:
        return (
            jsonify(
                {
                    "error": "同一个问题正在分析中，请等这一次返回后再问（避免重复计费）",
                    "pending": True,
                }
            ),
            409,
        )
    # 释放在 finally 里：手写每条返回路径都放一次，正是本项目反复修的那类漏
    # （漏一条就把这个问题永久锁在 _ASK_STALE_SECONDS 里，用户看到的是"这个问题
    # 一直说正在分析中"）。异常路径同理。
    try:
        try:
            chat = store._load_chat_cached(filepath)
        except Exception as e:  # noqa: BLE001 —— 坏文件给人话，不抛 500 堆栈
            return jsonify({"error": f"读取聊天记录失败: {e}"}), 500
        from analyzer import recap_client

        result = recap_client.answer_question(chat, question, chat_hash)
        if not result:
            return jsonify({"error": "这次提问没有拿到结果（材料不足或输出被截断），换个问法再试"}), 502
        store.ask_cache_write(chat_hash, question, result)
        return jsonify({"ok": True, "result": result})
    except QuotaExhaustedError as e:
        # 这条路径原先没有 except：而 _call_api 在 402/余额不足、401/403 配额、TPM 耗尽时
        # 是**故意抛** QuotaExhaustedError 的，异常于是穿透视图，用户拿到一页 Flask 的
        # 500 HTML，而那句专门写给人的"额度耗尽，请充值"被丢掉了——前端只显示
        # "提问失败（HTTP 500）"，完全无从下手。is_api_configured() 只查 Key 是否存在，
        # 所以"Key 有效但余额为 0"的第一次提问必定撞在这里。其它 LLM 入口都由 _run_job
        # 统一兜住这类异常，只有 ask 漏了。
        logger.warning("提问因配额/限流中止: %s", e)
        return jsonify({"error": str(e), "quota": True}), 429
    finally:
        _ask_release(chat_hash, question, token)


def register(app):
    """API 路由注册（端点名与旧 app.py 一致）"""
    app.add_url_rule("/api/analyze/<dimension>", "api_analyze", api_analyze, methods=["POST"])
    app.add_url_rule("/api/analyze-job/<job_id>", "api_analyze_job", api_analyze_job)
    app.add_url_rule(
        "/api/analyze-job/<job_id>/cancel", "api_analyze_cancel", api_analyze_cancel, methods=["POST"]
    )
    app.add_url_rule("/api/analysis/<dimension>", "api_analysis_result", api_analysis_result)
    app.add_url_rule("/api/analyze-all", "api_analyze_all", api_analyze_all, methods=["POST"])
    app.add_url_rule("/api/media", "api_media_upload", api_media_upload, methods=["POST"])
    app.add_url_rule("/api/faces/fetch", "api_faces_fetch", api_faces_fetch, methods=["POST"])
    app.add_url_rule("/api/usage", "api_usage", api_usage)
    app.add_url_rule("/api/status", "api_status", api_status)
    app.add_url_rule("/api/status/test", "api_status_test", api_status_test, methods=["POST"])
    app.add_url_rule("/api/chat/delete", "api_chat_delete", api_chat_delete, methods=["POST"])
    app.add_url_rule("/api/export", "api_export_chat", api_export_chat)
    app.add_url_rule("/api/import", "api_import_chat", api_import_chat, methods=["POST"])
    app.add_url_rule("/api/ask", "api_ask", api_ask, methods=["POST"])
    app.add_url_rule("/api/messages", "api_messages", api_messages)
    app.add_url_rule("/api/jobs/history", "api_job_history", api_job_history)
