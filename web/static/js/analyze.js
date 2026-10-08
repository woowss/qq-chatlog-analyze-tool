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

// 任务落定的浏览器通知 + 提示音（只在轮询路径调用：那正是"用户已切走标签页"的场景，
// 秒回的缓存命中用不着它）。三层退化，缺一层都照常工作：
// 1. Notification API：未授权则静默跳过（授权由第一次有任务完成时**经用户手势**申请，
//    不在页面加载时弹窗——那会被浏览器直接拒掉并把体验搞糟）；
// 2. 提示音：WebAudio 合成两声短促上行音，不引入任何音频文件；
// 3. document.title：通知被拒时标题就是最后的提醒位。
function notifyAnalyzeDone(text) {
    try {
        if (window.Notification) {
            if (Notification.permission === 'granted') {
                var n = new Notification('QQ 聊天分析 · ' + text, { silent: true });
                setTimeout(function() { try { n.close(); } catch (e) { /* 已关 */ } }, 8000);
            } else if (Notification.permission !== 'denied') {
                // 等下一次点击再申请：这里申请多半发生在轮询回调（非用户手势）里
                window.__notifyAskOnNextClick = true;
            }
        }
    } catch (e) { /* 通知失败不影响分析结果展示 */ }
    try {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (AC) {
            var ctx = new AC();
            [0, 0.18].forEach(function(delay, i) {
                var osc = ctx.createOscillator(), gain = ctx.createGain();
                osc.frequency.value = i ? 880 : 660;
                gain.gain.setValueAtTime(0.001, ctx.currentTime + delay);
                gain.gain.exponentialRampToValueAtTime(0.08, ctx.currentTime + delay + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + 0.15);
                osc.connect(gain).connect(ctx.destination);
                osc.start(ctx.currentTime + delay);
                osc.stop(ctx.currentTime + delay + 0.16);
            });
        }
    } catch (e) { /* 音频失败同理忽略 */ }
    var base = document.title.replace(/^[✅⚠️]\s*/, '');
    document.title = (text.indexOf('完成') === 0 ? '✅ ' : '⚠️ ') + base;
}

// 一次性把通知授权挂到下一次点击（点击是用户手势，授权弹窗在这个时机才被浏览器放行）
document.addEventListener('click', function askOnce() {
    if (window.__notifyAskOnNextClick && window.Notification && Notification.permission === 'default') {
        window.__notifyAskOnNextClick = false;
        Notification.requestPermission().catch(function() { /* 拒绝就用 title 提醒兜底 */ });
    }
});

// startAnalyze(dim, {refresh, onProgress(done,total), onDone(result), onError(msg)})
// POST /api/analyze/<dim>：命中缓存立即回调；否则轮询后台任务进度
function startAnalyze(dim, opts) {    opts.dim = dim;      // 轮询遇到 404 时用它回读磁盘缓存（见 pollAnalyzeJob）
    var done = opts.onDone;
    opts.onDone = function (result) {
        done(result);
        if (window.updateEvidence) window.updateEvidence(dim);
    };
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

// 轮询任务进度：指数退避 + 后台标签页降频。
// 早先是固定 1.5s 的 setInterval：一次全量分析几分钟就是几百次请求，而且标签页被
// 切到后台时浏览器本来就限流，继续按前台频率发只是白烧请求。现在起步 1s，最多 8s，
// 回到前台立刻补查一次，让用户不必干等。
var POLL_MIN_DELAY_MS = 1000;
var POLL_MAX_DELAY_MS = 8000;

function pollAnalyzeJob(jobId, opts) {
    var timer = null;
    var delay = POLL_MIN_DELAY_MS;
    var stopped = false;
    opts.jobId = jobId;

    function stop() {
        stopped = true;
        if (timer) { clearTimeout(timer); timer = null; }
        document.removeEventListener('visibilitychange', onVisibilityChange);
    }

    function schedule() {
        if (stopped) return;
        var wait = document.hidden ? POLL_MAX_DELAY_MS * 2 : delay;
        timer = setTimeout(tick, wait);
        opts.timer = timer;
    }

    function onVisibilityChange() {
        if (!document.hidden && !stopped) {     // 回到前台：立刻查一次
            if (timer) { clearTimeout(timer); timer = null; }
            tick();
        }
    }

    function tick() {
        if (stopped) return;
        $.get('/api/analyze-job/' + jobId, function(s) {
            if (stopped) return;
            if (opts.onProgress) opts.onProgress(s.done || 0, s.total || 0, s.detail || '');
            if (s.status === 'done') {
                stop();
                notifyAnalyzeDone('完成');
                opts.onDone(s.result);
            } else if (s.status === 'error') {
                stop();
                notifyAnalyzeDone('失败');
                opts.onError(s.error || '分析失败');
            } else if (s.status === 'cancelled') {
                stop();
                opts.onError('分析已取消');
            } else {
                delay = Math.min(POLL_MAX_DELAY_MS, Math.round(delay * 1.5));
                schedule();
            }
        }).fail(function(xhr) {
            stop();
            // 任务记录只在内存里（TTL 修剪 / 服务重启都会丢），但**已完成的维度结果已落盘**。
            // 这里若直接把"任务不存在"当失败报出去，用户会以为分析白跑了并再点一次（重复付费）。
            if (xhr && xhr.status === 404 && opts.dim) {
                loadAnalysis(opts.dim, function(result) {
                    if (result) opts.onDone(result);
                    else opts.onError('任务不存在（服务可能已重启），请重新发起分析');
                });
                return;
            }
            // status === 0：请求根本没拿到响应（服务已停止/被重启、断网、连接被重置）。
            // 这种情况与 404 一样不能当"分析失败"报——后台任务可能还在跑，结果也已落盘。
            // 先回读磁盘缓存；读不到就如实说明"连接中断、结果不会丢"，让用户刷新页面而不是重跑。
            if (xhr && xhr.status === 0) {
                var offline = '与服务的连接中断（服务可能已停止或重启）。已完成的维度结果已保存，'
                    + '刷新页面后即可查看；重启服务后重新发起也只会命中缓存，不会重复付费。';
                if (!opts.dim) {                 // 一键全量：没有单一维度可回读，只能如实提示
                    opts.onError(offline);
                    return;
                }
                loadAnalysis(opts.dim, function(result) {
                    if (result) opts.onDone(result);
                    else opts.onError(offline);
                });
                return;
            }
            opts.onError((xhr.responseJSON && xhr.responseJSON.error) || '任务状态查询失败');
        });
    }

    document.addEventListener('visibilitychange', onVisibilityChange);
    opts.stopPolling = stop;
    tick();
    return stop;
}

// 一键全量分析（五个维度顺序执行，进度按维度汇报）
function startAnalyzeAll(opts) {
    var url = '/api/analyze-all' + (opts.refresh ? '?refresh=1' : '');
    $.post(url, function(data) {
        if (data.error) { opts.onError(data.error); return; }
        if (data.job) pollAnalyzeJob(data.job, opts);
        else opts.onError('未知响应格式');
    }).fail(function(xhr) {
        opts.onError((xhr.responseJSON && xhr.responseJSON.error) || '请求失败');
    });
}

function cancelAnalyze(opts) {
    if (!opts.jobId) return;
    $.post('/api/analyze-job/' + opts.jobId + '/cancel', function() {
        if (opts.onProgress) opts.onProgress(-1, -1);
    });
}

// loadAnalysis(dim, cb)：优先读服务端磁盘缓存（跨标签页/重启浏览器仍有效），
// 失败时回退 sessionStorage（兼容旧会话）。
// 注意区分"确实还没有分析结果"（404）与"读取失败"（网络/服务异常）：
// 后者若也显示成"尚无分析结果"，用户会以为没跑过而重复付费分析。
function loadAnalysis(dim, cb) {
    $.get('/api/analysis/' + dim, function(data) {
        if (data && data.result) {
            try { sessionStorage.setItem('ai_' + dim, JSON.stringify(data.result)); } catch (e) { /* 配额满忽略 */ }
            setAnalysisEmptyState(dim, 'ready');
            cb(data.result);
            if (window.updateEvidence) window.updateEvidence(dim);
        } else {
            setAnalysisEmptyState(dim, 'empty');
            cb(null);
        }
    }).fail(function(xhr) {
        var status = xhr && xhr.status;
        var raw = null;
        try { raw = sessionStorage.getItem('ai_' + dim); } catch (e) { raw = null; }
        var parsed = null;
        try { parsed = raw ? JSON.parse(raw) : null; } catch (e) { parsed = null; }
        if (parsed) {
            setAnalysisEmptyState(dim, 'ready'); cb(parsed);
            if (window.updateEvidence) window.updateEvidence(dim);
            return;
        }
        // 服务端给了人话就直接用它（例如会话过期时的"请刷新页面重新登录"），
        // 比报一个裸 HTTP 状态码更可执行。
        var serverMsg = xhr && xhr.responseJSON && xhr.responseJSON.error;
        if (status && status !== 404) {
            showLoadWarning(serverMsg || ('读取分析结果失败（HTTP ' + status + '），请刷新页面重试；'
                + '已生成的结果不会丢失，重跑也会命中缓存。'));
            setAnalysisEmptyState(dim, 'error');
        } else {
            setAnalysisEmptyState(dim, 'empty');
        }
        cb(null);
    });
}

function setAnalysisEmptyState(dim, state, message) {
    var box = document.querySelector('[data-analysis-empty="' + dim + '"]');
    if (!box) return;
    box.dataset.state = state;
    if (state === 'ready') { box.classList.add('d-none'); return; }
    box.classList.remove('d-none');
    var title = box.querySelector('.analysis-empty-title');
    var description = box.querySelector('.analysis-empty-description');
    var configured = box.dataset.apiConfigured === 'true';
    var unconfigured = state === 'unconfigured' || (!configured && state === 'empty');
    if (title) {
        title.textContent = (unconfigured ? box.dataset.unconfiguredTitle : box.dataset[state + 'Title']) || '尚未分析';
    }
    if (description) {
        description.textContent = message || (unconfigured
            ? box.dataset.unconfiguredDescription
            : box.dataset[state + 'Description']);
    }
}

// 顶部提示条：只在第一次出现时插入，避免刷屏
function showLoadWarning(message) {
    var box = document.getElementById('loadWarnBox');
    if (!box) {
        box = document.createElement('div');
        box.id = 'loadWarnBox';
        box.className = 'alert alert-warning small mb-3';
        var host = document.querySelector('.container');
        if (!host) return;
        host.insertBefore(box, host.firstChild);
    }
    if (box.textContent !== message) box.textContent = message;
}

// 私聊维度页「页内运行本维度分析」的五页共用接线（此前只能回仪表盘触发，群聊页早有页内按钮）。
// 页面模板提供 #inPageAnalyze 按钮 / #inPageStatus 状态区 / #inPageRefresh 复选框；
// 没有这些元素的页面调用它会直接返回，因此对仪表盘、群聊页等零影响。
// 进度、取消、完成回调与仪表盘完全同一套（都走 startAnalyze → pollAnalyzeJob）。
function analyzeInPage(dim, renderFn) {
    var btn = $('#inPageAnalyze');
    if (!btn.length) return;
    btn.on('click', function () {
        var status = $('#inPageStatus');
        var orig = btn.data('orig') || '运行本维度分析';
        btn.prop('disabled', true).text('分析中…');
        setAnalysisEmptyState(dim, 'running');
        var opts = {
            refresh: $('#inPageRefresh').length ? $('#inPageRefresh').is(':checked') : false,
            onProgress: function (done, total) {
                if (done < 0) { $('#ipText').text('正在取消…'); return; }
                if (total > 0) {
                    var pct = Math.round(done / total * 100);
                    $('#ipProg').css('width', pct + '%');
                    $('#ipText').text('已完成 ' + done + '/' + total + ' 个月');
                }
            },
            onDone: function (result) {
                try { sessionStorage.setItem('ai_' + dim, JSON.stringify(result)); } catch (e) { /* 配额满忽略 */ }
                setAnalysisEmptyState(dim, 'ready');
                status.html('<div class="alert alert-success py-1 mb-0 small">分析完成</div>');
                btn.prop('disabled', false).text(orig);
                if (renderFn) renderFn(result);
            },
            onError: function (msg) {
                var cancelled = msg === '分析已取消';
                setAnalysisEmptyState(dim, cancelled ? 'empty' : 'error', cancelled ? null : msg);
                status.html('<div class="alert alert-' + (cancelled ? 'secondary' : 'danger')
                    + ' py-1 mb-0 small">' + esc(msg) + '</div>');
                btn.prop('disabled', false).text(orig);
            }
        };
        status.html(
            '<div class="d-flex align-items-center gap-2 flex-wrap">' +
            '<div class="spinner-border spinner-border-sm text-primary" role="status"></div>' +
            '<div class="progress" style="min-width:160px;height:8px;">' +
            '<div id="ipProg" class="progress-bar progress-bar-striped progress-bar-animated" style="width:0%"></div></div>' +
            '<span id="ipText" class="small text-muted">准备中…</span>' +
            '<button id="ipCancel" class="btn btn-sm btn-outline-secondary">取消</button></div>'
        );
        $('#ipCancel').on('click', function () { $(this).prop('disabled', true).text('取消中…'); cancelAnalyze(opts); });
        startAnalyze(dim, opts);
    });
}
