// Copyright (C) 2026 woowss
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.
//
//
// AI 分析任务前端：发起 → 轮询进度 → 完成回调；支持取消与强制重分析
// ====================================================================

// startAnalyze(dim, {refresh, onProgress(done,total), onDone(result), onError(msg)})
// POST /api/analyze/<dim>：命中缓存立即回调；否则轮询后台任务进度
function startAnalyze(dim, opts) {
    var url = '/api/analyze/' + dim + (opts.refresh ? '?refresh=1' : '');
    $.post(url, function(data) {
        if (data.error) { opts.onError(data.error); return; }
        if (data.cached) {
            if (opts.onProgress) opts.onProgress(1, 1);
            opts.onDone(data.result);
            return;
        }
        if (data.job) pollAnalyzeJob(data.job, opts);
        else opts.onError('未知响应格式');
    }).fail(function(xhr) {
        opts.onError((xhr.responseJSON && xhr.responseJSON.error) || '请求失败');
    });
}

function pollAnalyzeJob(jobId, opts) {
    var timer = setInterval(function() {
        $.get('/api/analyze-job/' + jobId, function(s) {
            if (opts.onProgress) opts.onProgress(s.done || 0, s.total || 0);
            if (s.status === 'done') {
                clearInterval(timer);
                opts.onDone(s.result);
            } else if (s.status === 'error') {
                clearInterval(timer);
                opts.onError(s.error || '分析失败');
            } else if (s.status === 'cancelled') {
                clearInterval(timer);
                opts.onError('分析已取消');
            }
        }).fail(function(xhr) {
            clearInterval(timer);
            opts.onError((xhr.responseJSON && xhr.responseJSON.error) || '任务状态查询失败');
        });
    }, 1500);
    opts.timer = timer;
    opts.jobId = jobId;
    return timer;
}

function cancelAnalyze(opts) {
    if (!opts.jobId) return;
    $.post('/api/analyze-job/' + opts.jobId + '/cancel', function() {
        if (opts.onProgress) opts.onProgress(-1, -1);
    });
}

// loadAnalysis(dim, cb)：优先读服务端磁盘缓存（跨标签页/重启浏览器仍有效），
// 失败时回退 sessionStorage（兼容旧会话）
function loadAnalysis(dim, cb) {
    $.get('/api/analysis/' + dim, function(data) {
        if (data && data.result) {
            try { sessionStorage.setItem('ai_' + dim, JSON.stringify(data.result)); } catch (e) { /* 配额满忽略 */ }
            cb(data.result);
        } else {
            cb(null);
        }
    }).fail(function() {
        var raw = sessionStorage.getItem('ai_' + dim);
        var parsed = null;
        try { parsed = raw ? JSON.parse(raw) : null; } catch (e) { parsed = null; }
        cb(parsed);
    });
}
