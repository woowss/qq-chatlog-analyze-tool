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

// startAnalyze(dim, {refresh, onProgress(done,total), onDone(result, meta), onError(msg)})
// 先展示只读计划；确认所选单元后重新核对并派发任务。
function startAnalyze(dim, opts) {    opts.dim = dim;      // 轮询遇到 404 时用它回读磁盘缓存（见 pollAnalyzeJob）
    var done = opts.onDone;
    opts.planMode = true;
    opts.onDone = function (result, meta) {
        if (meta && meta.partial) {
            done(result, meta);
            return;
        }
        done(result, meta);
        if (window.updateEvidence) window.updateEvidence(dim);
    };
    openAnalysisPlan([dim], opts);
}

// 轮询任务进度：指数退避 + 后台标签页降频。
// 早先是固定 1.5s 的 setInterval：一次全量分析几分钟就是几百次请求，而且标签页被
// 切到后台时浏览器本来就限流，继续按前台频率发只是白烧请求。现在流式输出期间
// 有增量时快速刷新（400ms），无新输出时指数退避到 8s，回到前台立刻补查一次。
var POLL_MIN_DELAY_MS = 400;
var POLL_MAX_DELAY_MS = 8000;

var _AI_DIM_LABELS = {
    emotion: '情绪分析',
    topics: '话题趋势',
    relationship: '人际关系',
    habits: '说话习惯',
    profile: '人物锐评',
    recap: '整体总括',
    group_dynamics: '群聊动态',
    group_topics: '群聊话题',
    group_emotion: '群聊情绪',
    member_profiles: '成员画像',
    all: '一键全量',
    plan: '自选计划分析'
};

function _aiDimTitle(dim, fallbackLabel) {
    if (fallbackLabel) return fallbackLabel + ' · 实时输出';
    if (dim && _AI_DIM_LABELS[dim]) return _AI_DIM_LABELS[dim] + ' · 实时输出';
    return 'AI 实时输出';
}

function pollAnalyzeJob(jobId, opts) {
    var timer = null;
    var delay = POLL_MIN_DELAY_MS;
    var stopped = false;
    var afterSeq = Number(opts.afterSeq || 0);
    opts.jobId = jobId;
    window.__activePollJobId = jobId;
    if (!opts._isRestore && window.AiOutputFeed) {
        window.AiOutputFeed.resetForJob(jobId, {
            title: _aiDimTitle(opts.dim || (opts.planMode ? 'plan' : ''), opts.dimLabel),
            show: true
        });
    }

    function stop() {
        stopped = true;
        if (window.__activePollJobId === jobId) window.__activePollJobId = null;
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
        $.get('/api/analyze-job/' + jobId, { after: afterSeq }, function(s) {
            if (stopped) return;
            var incoming = Array.isArray(s.events) ? s.events : [];
            if (typeof s.stream_seq === 'number' && s.stream_seq > afterSeq) {
                afterSeq = s.stream_seq;
            }
            if (window.AiOutputFeed) {
                window.AiOutputFeed.appendEvents(incoming, s);
                window.AiOutputFeed.setStatus(s.status, s);
            }
            if (opts.onProgress) opts.onProgress(s.done || 0, s.total || 0, s.detail || '');
            if (s.status === 'done') {
                stop();
                var resultMeta = {
                    partial: !!s.partial, summary: s.summary || {},
                    partialResults: s.partial_results || {}, results: s.results || {},
                    chat_hash: s.chat_hash, scope_id: s.scope_id
                };
                if (!window.validateAnalysisIdentity) {
                    opts.onError('无法确认当前聊天与分析范围，未显示任务结果。');
                    return;
                }
                window.validateAnalysisIdentity(resultMeta, function(valid, identity, error) {
                    if (!valid) {
                        if (error && error.indexOf('正在刷新') !== -1) return;
                        opts.onError(error || '无法确认当前聊天与分析范围，未显示任务结果。');
                        return;
                    }
                    if (!resultMeta.partial && window.cacheAnalysisResult) {
                        if (opts.dim && s.result !== undefined) window.cacheAnalysisResult(opts.dim, s.result, identity);
                        else Object.keys(resultMeta.results).forEach(function(dim) {
                            window.cacheAnalysisResult(dim, resultMeta.results[dim], identity);
                        });
                    }
                    notifyAnalyzeDone('完成');
                    opts.onDone(s.result, resultMeta);
                });
            } else if (s.status === 'error') {
                stop();
                var errorMeta = {
                    status: s.status, partial: !!s.partial, result: s.result,
                    results: s.results || {}, summary: s.summary || {},
                    partialResults: s.partial_results || {},
                    chat_hash: s.chat_hash, scope_id: s.scope_id
                };
                validateTerminalIdentity(errorMeta, function(valid, error) {
                    if (!valid) {
                        if (error && error.indexOf('正在刷新') !== -1) return;
                        opts.onError(error || '无法确认当前聊天与分析范围，未显示任务结果。');
                        return;
                    }
                    notifyAnalyzeDone('失败');
                    opts.onError(s.error || '分析失败', errorMeta);
                });
            } else if (s.status === 'cancelled') {
                stop();
                var cancelledMeta = {
                    status: s.status, partial: !!s.partial, result: s.result,
                    results: s.results || {}, summary: s.summary || {},
                    partialResults: s.partial_results || {},
                    chat_hash: s.chat_hash, scope_id: s.scope_id
                };
                validateTerminalIdentity(cancelledMeta, function(valid, error) {
                    if (!valid) {
                        if (error && error.indexOf('正在刷新') !== -1) return;
                        opts.onError(error || '无法确认当前聊天与分析范围，未显示任务结果。');
                        return;
                    }
                    opts.onError(s.error || '分析已取消', cancelledMeta);
                });
            } else {
                delay = incoming.length > 0
                    ? POLL_MIN_DELAY_MS
                    : Math.min(POLL_MAX_DELAY_MS, Math.round(delay * 1.5));
                schedule();
            }
        }).fail(function(xhr) {
            stop();
            // 任务记录只在内存里（TTL 修剪 / 服务重启都会丢），但**已完成的维度结果已落盘**。
            // 这里若直接把"任务不存在"当失败报出去，用户会以为分析白跑了并再点一次（重复付费）。
            if (xhr && xhr.status === 404 && opts.dim && !opts.planMode) {
                loadAnalysis(opts.dim, function(result, identity) {
                    if (result) opts.onDone(result, identity);
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
                if (!opts.dim || opts.planMode) { // 计划任务可能只完成了部分单元，不能回读旧维度结果冒充本次结果
                    opts.onError(offline);
                    return;
                }
                loadAnalysis(opts.dim, function(result, identity) {
                    if (result) opts.onDone(result, identity);
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

function validateTerminalIdentity(metadata, callback) {
    if (!window.validateAnalysisIdentity) {
        callback(false, '无法确认当前聊天与分析范围，未显示任务结果。');
        return;
    }
    window.validateAnalysisIdentity(metadata, function(valid, identity, error) {
        callback(valid, error);
    });
}

// 一键全量分析（五个维度顺序执行，进度按维度汇报）
function startAnalyzeAll(opts) {
    opts.planMode = true;
    openAnalysisPlan([], opts);
}

function cancelAnalyze(opts) {
    if (!opts.jobId) return;
    $.post('/api/analyze-job/' + opts.jobId + '/cancel', function() {
        if (opts.onProgress) opts.onProgress(-1, -1);
    });
}

// loadAnalysis(dim, cb)：优先读服务端磁盘缓存；离线回退只使用身份匹配的会话缓存。
// 注意区分"确实还没有分析结果"（404）与"读取失败"（网络/服务异常）：
// 后者若也显示成"尚无分析结果"，用户会以为没跑过而重复付费分析。
function loadAnalysis(dim, cb) {
    $.get('/api/analysis/' + dim, function(data) {
        if (data && data.result) {
            if (!window.acceptAnalysisResult) {
                setAnalysisEmptyState(dim, 'error', '无法确认当前聊天与分析范围，未显示该结果。');
                cb(null);
                return;
            }
            window.acceptAnalysisResult(dim, data.result, data, function(valid, identity, error) {
                if (!valid) {
                    var refreshing = error && error.indexOf('正在刷新') !== -1;
                    setAnalysisEmptyState(dim, refreshing ? 'empty' : 'error', refreshing ? '分析范围已改变，正在刷新页面。' : error);
                    cb(null);
                    return;
                }
                setAnalysisEmptyState(dim, 'ready');
                cb(data.result, identity);
                if (window.updateEvidence) window.updateEvidence(dim);
            }, true);
        } else {
            setAnalysisEmptyState(dim, 'empty');
            cb(null);
        }
    }).fail(function(xhr) {
        var status = xhr && xhr.status;
        var canFallback = status === 404 || status === 0;
        function finishWithoutCache(identityError) {
            // 服务端给了人话就直接用它（例如会话过期），比裸状态码更可执行。
            var serverMsg = xhr && xhr.responseJSON && xhr.responseJSON.error;
            if (status && status !== 404) {
                showLoadWarning(identityError || serverMsg || ('读取分析结果失败（HTTP ' + status + '），请刷新页面重试；已生成的结果不会丢失。'));
                setAnalysisEmptyState(dim, 'error');
            } else {
                setAnalysisEmptyState(dim, identityError ? 'error' : 'empty', identityError || null);
            }
            cb(null);
        }
        if (canFallback && window.getAnalysisPageIdentity && window.validateAnalysisIdentity && window.readCachedAnalysisResult) {
            window.getAnalysisPageIdentity(function(pageIdentity, pageError) {
                if (!pageIdentity) { finishWithoutCache(pageError || '无法确认当前聊天与分析范围，未读取会话缓存。'); return; }
                window.validateAnalysisIdentity(pageIdentity, function(valid, identity, error) {
                    if (!valid) { finishWithoutCache(error || '无法确认当前聊天与分析范围，未读取会话缓存。'); return; }
                    var cached = window.readCachedAnalysisResult(dim, identity);
                    if (cached) {
                        setAnalysisEmptyState(dim, 'ready');
                        cb(cached, identity);
                        if (window.updateEvidence) window.updateEvidence(dim);
                        return;
                    }
                    finishWithoutCache(status === 0 ? '当前无法连接服务，且没有可确认身份的本地结果。' : null);
                });
            });
            return;
        }
        if (canFallback) { finishWithoutCache('无法确认当前聊天与分析范围，未读取会话缓存。'); return; }
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

function _isAnalyzeCancelled(msg, meta) {
    return Boolean(
        (meta && meta.status === 'cancelled')
        || (typeof msg === 'string' && msg.indexOf('分析已取消') === 0)
    );
}

function _renderStatusAlert(statusEl, level, text, compact) {
    if (!statusEl || !statusEl.length) return;
    var cls = 'alert alert-' + level + (compact ? ' py-1 small' : '') + ' mb-0';
    statusEl.empty().append($('<div>').addClass(cls).text(text));
}

function _mountAnalyzeProgress(statusEl, opts, ui) {
    var progId = (ui && ui.progId) || 'aProg';
    var textId = (ui && ui.textId) || 'aText';
    var cancelId = (ui && ui.cancelId) || 'aCancel';
    var inline = Boolean(ui && ui.inline);
    if (inline) {
        statusEl.html(
            '<div class="d-flex align-items-center gap-2 flex-wrap">' +
            '<div class="spinner-border spinner-border-sm text-primary" role="status"></div>' +
            '<div class="progress" style="min-width:160px;height:8px;">' +
            '<div id="' + progId + '" class="progress-bar progress-bar-striped progress-bar-animated" style="width:0%"></div></div>' +
            '<span id="' + textId + '" class="small text-muted">准备中…</span>' +
            '<button id="' + cancelId + '" class="btn btn-sm btn-outline-secondary">取消</button></div>'
        );
    } else {
        statusEl.html(
            '<div class="spinner-border spinner-border-sm text-primary" role="status"></div>' +
            '<div class="progress mx-auto mt-2" style="max-width:320px;height:8px;">' +
            '<div id="' + progId + '" class="progress-bar progress-bar-striped progress-bar-animated" style="width:0%"></div></div>' +
            '<div id="' + textId + '" class="small text-muted mt-1">准备中…</div>' +
            '<button id="' + cancelId + '" class="btn btn-sm btn-outline-secondary mt-2">取消</button>'
        );
    }
    $('#' + cancelId).on('click', function () {
        $(this).prop('disabled', true).text('取消中…');
        cancelAnalyze(opts);
    });
    return function updateProgress(done, total, text) {
        if (done < 0) { $('#' + textId).text('正在取消…'); return; }
        if (total > 0) $('#' + progId).css('width', Math.round(done / total * 100) + '%');
        $('#' + textId).text(text || '准备中…');
    };
}

function runSingleDimension(config) {
    var dim = config.dim;
    var btn = config.btn;
    var status = config.status;
    var origLabel = config.origLabel || btn.data('orig') || btn.text();
    var runningLabel = config.runningLabel || '分析中…';
    var doneLabel = config.doneLabel || origLabel;
    var compact = Boolean(config.compactAlert);
    var updateEmptyState = Boolean(config.updateEmptyState);

    btn.prop('disabled', true).text(runningLabel);
    if (updateEmptyState) setAnalysisEmptyState(dim, 'running');

    var updateProgress = null;
    function finishButton(label) {
        btn.prop('disabled', false).text(label || origLabel);
        if (config.onFinish) config.onFinish();
    }

    var opts = {
        refresh: Boolean(config.refresh),
        onProgress: function (done, total, detail) {
            var text = config.formatProgress
                ? config.formatProgress(done, total, detail)
                : (total > 0
                    ? ('已完成 ' + done + '/' + total + '（' + Math.round(done / total * 100) + '%）')
                    : '准备中…');
            if (updateProgress) updateProgress(done, total, text);
        },
        onDone: function (result, meta) {
            if (meta && meta.partial) {
                var partialMsg = config.onPartialResult
                    ? config.onPartialResult(result, meta, false)
                    : null;
                _renderStatusAlert(
                    status,
                    'warning',
                    partialMsg || '本次只处理了所选单元，未标记为完整维度结果；可重新预览并补齐。',
                    compact
                );
                finishButton(origLabel);
                return;
            }
            if (updateEmptyState) setAnalysisEmptyState(dim, 'ready');
            var doneMsg = config.onFullDone ? config.onFullDone(result, meta) : null;
            _renderStatusAlert(status, 'success', doneMsg || '分析完成', compact);
            finishButton(doneLabel);
        },
        onError: function (msg, meta) {
            var cancelled = _isAnalyzeCancelled(msg, meta);
            var partial = meta && meta.partialResults && meta.partialResults[dim];
            var hasPartial = Boolean(partial && (typeof partial !== 'object' || Object.keys(partial).length > 0));
            if (hasPartial && config.onPartialResult) {
                var partialErrorMsg = config.onPartialResult(partial, meta, true);
                _renderStatusAlert(status, 'warning', partialErrorMsg || msg, compact);
                finishButton(origLabel);
                return;
            }
            if (updateEmptyState) {
                setAnalysisEmptyState(dim, cancelled ? 'empty' : 'error', cancelled ? null : msg);
            }
            var retained = Object.keys((meta && meta.results) || {});
            var suffix = retained.length ? '；任务响应中保留了这些维度的结果：' + retained.join('、') : '';
            var level = cancelled ? 'secondary' : (retained.length ? 'warning' : 'danger');
            _renderStatusAlert(status, level, (cancelled ? '分析已取消' : msg) + suffix, compact);
            finishButton(origLabel);
        }
    };

    updateProgress = _mountAnalyzeProgress(status, opts, config.progressIds);
    startAnalyze(dim, opts);
}

function runBatchAnalyze(config) {
    var btn = config.btn;
    var status = config.status;
    var idleLabel = config.idleLabel || '全量分析';
    var runningLabel = config.runningLabel || '全量分析中…';
    $('.btn-analyze, #btn-analyze-all').prop('disabled', true);
    btn.text(runningLabel);

    function restoreAllButtons() {
        $('.btn-analyze').prop('disabled', false).each(function () {
            var orig = $(this).data('orig');
            if (orig) $(this).text(orig);
        });
        btn.prop('disabled', false).text(idleLabel);
    }

    var updateProgress = null;
    var opts = {
        refresh: Boolean(config.refresh),
        onProgress: function (done, total, detail) {
            var text = config.formatProgress
                ? config.formatProgress(done, total, detail)
                : (detail || (total > 0 ? ('已完成 ' + done + '/' + total + ' 个维度') : '准备中…'));
            if (updateProgress) updateProgress(done, total, text);
        },
        onDone: function (summary, meta) {
            var outcome = config.onDone ? config.onDone(summary, meta, restoreAllButtons) : null;
            restoreAllButtons();
            if (outcome && outcome.text) {
                _renderStatusAlert(status, outcome.level || 'success', outcome.text, false);
            }
        },
        onError: function (msg, meta) {
            restoreAllButtons();
            var cancelled = _isAnalyzeCancelled(msg, meta);
            var retained = Object.keys((meta && meta.results) || {});
            var suffix = retained.length ? '；任务响应中保留了这些维度的结果：' + retained.join('、') : '';
            var level = (cancelled || retained.length) ? 'warning' : 'danger';
            var text = (cancelled ? '全量分析已取消。' : msg) + suffix;
            _renderStatusAlert(status, level, text, false);
        }
    };

    updateProgress = _mountAnalyzeProgress(status, opts, config.progressIds);
    startAnalyzeAll(opts);
}

window._analysisPageRefreshers = window._analysisPageRefreshers || {};

function registerAnalysisRefresher(dim, fn) {
    if (dim && typeof fn === 'function') {
        window._analysisPageRefreshers[dim] = fn;
    }
}
window.registerAnalysisRefresher = registerAnalysisRefresher;

var AiOutputFeed = (function () {
    var currentJobId = '';
    var renderedSeq = -1;
    var unitMap = new Map();
    var promptTotal = 0;
    var completionTotal = 0;
    var autoScroll = true;
    var latestDone = 0;
    var latestTotal = 0;
    var initializedDom = false;

    function el(id) {
        return document.getElementById(id);
    }

    function mountIntoSlot() {
        var panel = el('aiOutputPanel');
        var slot = document.querySelector('[data-ai-output-slot]');
        if (panel && slot && panel.parentNode !== slot) {
            slot.appendChild(panel);
        }
    }

    function initDomEvents() {
        if (initializedDom) return;
        var panel = el('aiOutputPanel');
        if (!panel) return;
        initializedDom = true;
        mountIntoSlot();

        var thinkBtn = el('aiOutputThinkingBtn');
        if (thinkBtn) {
            thinkBtn.addEventListener('click', function () {
                var hidden = panel.classList.toggle('ai-output-hide-thinking');
                thinkBtn.classList.toggle('active', !hidden);
                thinkBtn.setAttribute('aria-pressed', hidden ? 'false' : 'true');
            });
        }

        var toggleBtn = el('aiOutputToggleBtn');
        var collapseBody = el('aiOutputCollapseBody');
        if (toggleBtn && collapseBody) {
            toggleBtn.addEventListener('click', function () {
                var collapsed = collapseBody.classList.toggle('d-none');
                toggleBtn.textContent = collapsed ? '展开' : '收起';
                toggleBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            });
        }

        var body = el('aiOutputBody');
        var jumpWrap = el('aiOutputJumpWrap');
        var jumpBtn = el('aiOutputJumpBtn');
        if (body) {
            body.addEventListener('scroll', function () {
                autoScroll = (body.scrollHeight - body.scrollTop - body.clientHeight) <= 40;
                if (jumpWrap) jumpWrap.classList.toggle('d-none', autoScroll);
            });
        }
        if (jumpBtn && body) {
            jumpBtn.addEventListener('click', function () {
                body.scrollTop = body.scrollHeight;
                autoScroll = true;
                if (jumpWrap) jumpWrap.classList.add('d-none');
            });
        }
    }

    function showPanel() {
        initDomEvents();
        var panel = el('aiOutputPanel');
        if (panel) panel.classList.remove('d-none');
    }

    function expandPanel() {
        var collapseBody = el('aiOutputCollapseBody');
        var toggleBtn = el('aiOutputToggleBtn');
        if (collapseBody) collapseBody.classList.remove('d-none');
        if (toggleBtn) {
            toggleBtn.textContent = '收起';
            toggleBtn.setAttribute('aria-expanded', 'true');
        }
    }

    function updateMetaText() {
        var meta = el('aiOutputMeta');
        if (!meta) return;
        var parts = [];
        if (latestTotal > 0) parts.push('进度 ' + latestDone + '/' + latestTotal);
        if (promptTotal > 0 || completionTotal > 0) {
            parts.push('prompt ' + promptTotal.toLocaleString() + ' / completion ' + completionTotal.toLocaleString() + ' tok');
        }
        meta.textContent = parts.join(' · ');
    }

    function setStatus(status, meta) {
        var badge = el('aiOutputStatus');
        if (meta) {
            if (typeof meta.done === 'number') latestDone = meta.done;
            if (typeof meta.total === 'number') latestTotal = meta.total;
            updateMetaText();
            if (meta.dim_label || meta.dim) {
                var t = el('aiOutputTitle');
                if (t) t.textContent = _aiDimTitle(meta.dim || '', meta.dim_label || '');
            }
        }
        if (!badge || !status) return;
        var map = {
            running: ['进行中', 'badge bg-primary ai-output-status'],
            done: ['已完成', 'badge bg-success ai-output-status'],
            cancelled: ['已取消', 'badge bg-secondary ai-output-status'],
            error: ['异常', 'badge bg-danger ai-output-status']
        };
        var cfg = map[status] || [String(status), 'badge bg-secondary ai-output-status'];
        badge.textContent = cfg[0];
        badge.className = cfg[1];
    }

    function ensurePlaceholder(text) {
        var body = el('aiOutputBody');
        if (!body || body.children.length > 0) return;
        var empty = document.createElement('div');
        empty.className = 'ai-output-empty';
        empty.textContent = text;
        body.appendChild(empty);
    }

    function clearPlaceholder() {
        var body = el('aiOutputBody');
        if (!body) return;
        var empty = body.querySelector('.ai-output-empty');
        if (empty) empty.remove();
    }

    function resetForJob(jobId, opts) {
        initDomEvents();
        currentJobId = jobId || '';
        renderedSeq = -1;
        unitMap.clear();
        promptTotal = 0;
        completionTotal = 0;
        latestDone = 0;
        latestTotal = 0;
        autoScroll = true;

        var body = el('aiOutputBody');
        if (body) body.textContent = '';
        var jumpWrap = el('aiOutputJumpWrap');
        if (jumpWrap) jumpWrap.classList.add('d-none');

        var title = el('aiOutputTitle');
        if (title) title.textContent = (opts && opts.title) || 'AI 实时输出';
        setStatus((opts && opts.status) || 'running', opts && opts.meta);
        updateMetaText();
        ensurePlaceholder('等待模型响应与输出分片…');
        if (!opts || opts.show !== false) showPanel();
        expandPanel();
    }

    function isTimelineUnit(unit) {
        return !unit || unit === 'all' || unit === 'plan' || unit === 'system' || unit.indexOf('dim:') === 0;
    }

    function setUnitStateBadge(card, text, badgeClass) {
        card.stateBadge.textContent = text;
        card.stateBadge.className = badgeClass;
    }

    function getOrCreateUnitCard(unitKey, label) {
        if (unitMap.has(unitKey)) return unitMap.get(unitKey);
        var body = el('aiOutputBody');
        var root = document.createElement('div');
        root.className = 'ai-output-unit';

        var head = document.createElement('div');
        head.className = 'ai-output-unit-head';
        var title = document.createElement('span');
        title.className = 'ai-output-unit-title';
        title.textContent = label || unitKey;
        head.appendChild(title);

        var badges = document.createElement('div');
        badges.className = 'ai-output-unit-badges';
        var tokenBadge = document.createElement('span');
        tokenBadge.className = 'badge bg-light text-secondary d-none ai-output-unit-tokens';
        badges.appendChild(tokenBadge);
        var stateBadge = document.createElement('span');
        stateBadge.className = 'badge bg-primary ai-output-unit-state';
        stateBadge.textContent = '生成中';
        badges.appendChild(stateBadge);
        head.appendChild(badges);
        root.appendChild(head);

        var notes = document.createElement('div');
        notes.className = 'ai-output-unit-notes d-none';
        root.appendChild(notes);

        var thinkBox = document.createElement('details');
        thinkBox.className = 'ai-output-thinking d-none';
        thinkBox.open = true;
        var thinkSummary = document.createElement('summary');
        thinkSummary.textContent = '思考过程';
        thinkBox.appendChild(thinkSummary);
        var thinkPre = document.createElement('pre');
        thinkPre.className = 'ai-output-pre';
        thinkBox.appendChild(thinkPre);
        root.appendChild(thinkBox);

        var contentBox = document.createElement('div');
        contentBox.className = 'ai-output-content d-none';
        var contentLabel = document.createElement('div');
        contentLabel.className = 'ai-output-content-label';
        contentLabel.textContent = '生成内容';
        contentBox.appendChild(contentLabel);
        var contentPre = document.createElement('pre');
        contentPre.className = 'ai-output-pre';
        contentBox.appendChild(contentPre);
        root.appendChild(contentBox);

        if (body) body.appendChild(root);
        var rec = {
            root: root,
            titleEl: title,
            tokenBadge: tokenBadge,
            stateBadge: stateBadge,
            notes: notes,
            thinkBox: thinkBox,
            thinkPre: thinkPre,
            contentBox: contentBox,
            contentLabel: contentLabel,
            contentPre: contentPre
        };
        unitMap.set(unitKey, rec);
        return rec;
    }

    function safeKindClass(kind) {
        if (['stage', 'cache', 'warn', 'error', 'usage', 'thinking', 'content', 'result'].indexOf(kind) !== -1) {
            return 'kind-' + kind;
        }
        return 'kind-stage';
    }

    function appendTimelineNote(kind, text) {
        var body = el('aiOutputBody');
        if (!body || !text) return;
        var row = document.createElement('div');
        row.className = 'ai-output-timeline ' + safeKindClass(kind);
        row.textContent = text;
        body.appendChild(row);
    }

    function appendUnitNote(card, kind, text) {
        if (!text) return;
        card.notes.classList.remove('d-none');
        var note = document.createElement('div');
        note.className = 'ai-output-unit-note ' + safeKindClass(kind);
        note.textContent = text;
        card.notes.appendChild(note);
    }

    function resetUnitOutput(card) {
        if (!card) return;
        card.thinkPre.textContent = '';
        card.thinkBox.classList.add('d-none');
        card.contentPre.textContent = '';
        card.contentLabel.textContent = '生成内容';
        card.contentBox.classList.add('d-none');
    }

    function appendEvents(events, meta) {
        if (meta) {
            if (typeof meta.done === 'number') latestDone = meta.done;
            if (typeof meta.total === 'number') latestTotal = meta.total;
        }
        if (!Array.isArray(events) || !events.length) {
            updateMetaText();
            return;
        }
        showPanel();
        clearPlaceholder();

        events.slice().sort(function (a, b) {
            return Number(a.seq || 0) - Number(b.seq || 0);
        }).forEach(function (ev) {
            var seq = Number(ev.seq);
            if (!isNaN(seq)) {
                if (seq <= renderedSeq) return;
                renderedSeq = seq;
            }
            var kind = ev.kind || 'stage';
            var unit = ev.unit || '';
            var label = ev.label || unit;
            var text = ev.text || '';
            if (kind === 'usage' && ev.data) {
                var pTok = Number(ev.data.prompt_tokens || 0);
                var cTok = Number(ev.data.completion_tokens || 0);
                promptTotal += pTok;
                completionTotal += cTok;
                if (!isTimelineUnit(unit)) {
                    var uCard = getOrCreateUnitCard(unit, label);
                    uCard.tokenBadge.textContent = 'in ' + pTok.toLocaleString() + ' / out ' + cTok.toLocaleString() + ' tok';
                    uCard.tokenBadge.classList.remove('d-none');
                }
                return;
            }
            if (isTimelineUnit(unit)) {
                appendTimelineNote(kind, text);
                return;
            }
            var card = getOrCreateUnitCard(unit, label);
            if (label && !card.titleEl.textContent) card.titleEl.textContent = label;
            var shouldReset = Boolean(ev.data && ev.data.reset);
            if (kind === 'reset') {
                resetUnitOutput(card);
                if (text) appendUnitNote(card, 'warn', text);
                return;
            }
            if (shouldReset) {
                resetUnitOutput(card);
            }
            if (kind === 'thinking') {
                card.thinkBox.classList.remove('d-none');
                card.thinkPre.appendChild(document.createTextNode(text));
                card.thinkPre.scrollTop = card.thinkPre.scrollHeight;
            } else if (kind === 'content') {
                card.contentBox.classList.remove('d-none');
                card.contentLabel.textContent = '生成内容';
                card.contentPre.appendChild(document.createTextNode(text));
                card.contentPre.scrollTop = card.contentPre.scrollHeight;
            } else if (kind === 'result') {
                card.contentBox.classList.remove('d-none');
                card.contentLabel.textContent = '结构化结果';
                if (!card.contentPre.textContent) {
                    card.contentPre.textContent = text;
                }
            } else if (kind === 'cache') {
                appendUnitNote(card, kind, text);
                setUnitStateBadge(card, '命中缓存', 'badge bg-secondary ai-output-unit-state');
            } else if (kind === 'error') {
                appendUnitNote(card, kind, text);
                setUnitStateBadge(card, '失败', 'badge bg-danger ai-output-unit-state');
            } else if (kind === 'warn') {
                appendUnitNote(card, kind, text);
            } else {
                appendUnitNote(card, kind, text);
                if (text.indexOf('已取消') !== -1) {
                    setUnitStateBadge(card, '已取消', 'badge bg-secondary ai-output-unit-state');
                } else if (text.indexOf('完成') !== -1) {
                    setUnitStateBadge(card, '已完成', 'badge bg-success ai-output-unit-state');
                }
            }
        });

        updateMetaText();
        var body = el('aiOutputBody');
        var jumpWrap = el('aiOutputJumpWrap');
        if (body) {
            if (autoScroll) {
                body.scrollTop = body.scrollHeight;
                if (jumpWrap) jumpWrap.classList.add('d-none');
            } else if (jumpWrap) {
                jumpWrap.classList.remove('d-none');
            }
        }
    }

    function restoreSnapshot(snap) {
        if (!snap || !snap.job_id) return;
        resetForJob(snap.job_id, {
            title: _aiDimTitle(snap.dim || '', snap.dim_label || ''),
            status: snap.status || 'done',
            meta: snap,
            show: true
        });
        appendEvents(snap.events || [], snap);
        setStatus(snap.status || 'done', snap);
        if ((!snap.events || !snap.events.length) && snap.status !== 'running') {
            clearPlaceholder();
            ensurePlaceholder('本次任务暂无详细流式文本。');
        }
    }

    return {
        initDomEvents: initDomEvents,
        resetForJob: resetForJob,
        appendEvents: appendEvents,
        setStatus: setStatus,
        restoreSnapshot: restoreSnapshot
    };
})();
window.AiOutputFeed = AiOutputFeed;

function _unlockRestoredButtons() {
    $('.btn-analyze, #inPageAnalyze, #btn-analyze-all, #btn-recap').prop('disabled', false).each(function () {
        var orig = $(this).data('orig');
        if (orig) $(this).text(orig);
    });
    if ($('#btn-analyze-all').length) $('#btn-analyze-all').text('全量分析');
    if ($('#btn-recap').length && !$('#btn-recap').data('orig')) $('#btn-recap').text('生成整体总括');
}

function _applyRestoredCompletion(dim, result, meta) {
    var refreshers = window._analysisPageRefreshers || {};
    var isBatch = !dim || dim === 'all' || dim === 'plan';
    var summary = (meta && meta.summary) || {};
    if (!isBatch && refreshers[dim]) {
        var singleInfo = {
            partial: Boolean(meta && meta.partial),
            fromError: Boolean(meta && meta.status && meta.status !== 'done')
        };
        if (result) refreshers[dim](result, singleInfo);
        else loadAnalysis(dim, function (data) { if (data) refreshers[dim](data, { partial: false, fromError: false }); });
        if (window.updateEvidence && !(meta && meta.partial)) window.updateEvidence(dim);
        return;
    }
    Object.keys(refreshers).forEach(function (d) {
        var direct = meta && ((meta.results && meta.results[d]) || (meta.partialResults && meta.partialResults[d]));
        var dimState = summary[d];
        if (direct) {
            var fromError = Boolean(
                (meta && meta.status && meta.status !== 'done')
                || dimState === 'error'
                || dimState === 'aborted'
                || dimState === 'cancelled'
                || (meta && meta.partialResults && meta.partialResults[d] && dimState !== 'partial')
            );
            var isPartial = Boolean(
                fromError
                || dimState === 'partial'
                || (meta && meta.partial && dimState !== 'done' && dimState !== 'cached')
            );
            refreshers[d](direct, { partial: isPartial, fromError: fromError });
            if (window.updateEvidence && !isPartial) window.updateEvidence(d);
        } else {
            loadAnalysis(d, function (data) {
                if (data) refreshers[d](data, { partial: false, fromError: false });
            });
        }
    });
}

function restoreLatestAnalysisJob() {
    if (!$('#aiOutputPanel').length) return;
    var pageId = window.ANALYSIS_PAGE_IDENTITY || null;
    var query = (pageId && pageId.scope_id) ? { scope_id: pageId.scope_id } : {};
    $.get('/api/analyze-jobs/latest', query, function (snap) {
        if (!snap || !snap.found) return;
        if (window.__activePollJobId && window.__activePollJobId !== snap.job_id) return;
        if (pageId && ((snap.chat_hash && pageId.chat_hash && snap.chat_hash !== pageId.chat_hash)
            || ((snap.scope_id || '') !== (pageId.scope_id || '')))) {
            return;
        }
        AiOutputFeed.restoreSnapshot(snap);
        if (snap.status !== 'running' || window.__activePollJobId === snap.job_id) return;

        var statusEl = $('#analyzeStatus').length
            ? $('#analyzeStatus')
            : ($('#inPageStatus').length ? $('#inPageStatus') : $('#recapStatus'));
        var isInline = Boolean(!$('#analyzeStatus').length && ($('#inPageStatus').length || $('#recapStatus').length));
        $('.btn-analyze, #btn-analyze-all, #inPageAnalyze, #btn-recap').prop('disabled', true);

        var singleDim = (snap.dim && snap.dim !== 'all' && snap.dim !== 'plan') ? snap.dim : null;
        var updateProgress = null;
        var opts = {
            _isRestore: true,
            afterSeq: snap.stream_seq || 0,
            dim: singleDim,
            dimLabel: snap.dim_label || '',
            planMode: Boolean(snap.dim === 'all' || snap.dim === 'plan'),
            onProgress: function (done, total, detail) {
                var txt = detail || (total > 0 ? ('已完成 ' + done + '/' + total) : '分析中…');
                if (updateProgress) updateProgress(done, total, txt);
            },
            onDone: function (result, meta) {
                _unlockRestoredButtons();
                if (statusEl.length) {
                    _renderStatusAlert(
                        statusEl,
                        (meta && meta.partial) ? 'warning' : 'success',
                        (meta && meta.partial) ? '部分分析已完成。' : '分析已完成。',
                        isInline
                    );
                }
                _applyRestoredCompletion(snap.dim, result, meta);
            },
            onError: function (msg, meta) {
                _unlockRestoredButtons();
                var cancelled = _isAnalyzeCancelled(msg, meta);
                var hasPartial = meta && ((meta.results && Object.keys(meta.results).length)
                    || (meta.partialResults && Object.keys(meta.partialResults).length));
                if (statusEl.length) {
                    var level = cancelled ? 'secondary' : (hasPartial ? 'warning' : 'danger');
                    _renderStatusAlert(statusEl, level, cancelled ? '分析已取消' : (msg || '分析未完成'), isInline);
                }
                if (hasPartial) _applyRestoredCompletion(snap.dim, meta.result, meta);
            }
        };
        if (statusEl.length) {
            updateProgress = _mountAnalyzeProgress(statusEl, opts, {
                progId: 'aProg',
                textId: 'aText',
                cancelId: 'aCancel',
                inline: isInline
            });
            updateProgress(snap.done || 0, snap.total || 0, snap.detail || '恢复连接，继续输出…');
        }
        pollAnalyzeJob(snap.job_id, opts);
    });
}
window.restoreLatestAnalysisJob = restoreLatestAnalysisJob;

// 私聊维度页「页内运行本维度分析」的五页共用接线（此前只能回仪表盘触发，群聊页早有页内按钮）。
// 页面模板提供 #inPageAnalyze 按钮 / #inPageStatus 状态区 / #inPageRefresh 复选框；
// 没有这些元素的页面调用它会直接返回，因此对仪表盘、群聊页等零影响。
// 进度、取消、完成回调与仪表盘完全同一套（都走 startAnalyze → pollAnalyzeJob）。
function analyzeInPage(dim, renderFn) {
    if (renderFn) {
        registerAnalysisRefresher(dim, function (data, info) {
            if (info && info.partial) {
                setAnalysisEmptyState(dim, 'empty', info.fromError
                    ? '本次分析未完整完成，当前显示已完成部分。'
                    : '本次只分析了所选单元，尚未生成完整维度结果。');
            } else {
                setAnalysisEmptyState(dim, 'ready');
            }
            renderFn(data);
        });
    }
    var btn = $('#inPageAnalyze');
    if (!btn.length) return;
    btn.on('click', function () {
        var orig = btn.data('orig') || '运行本维度分析';
        runSingleDimension({
            dim: dim,
            btn: btn,
            status: $('#inPageStatus'),
            refresh: $('#inPageRefresh').length ? $('#inPageRefresh').is(':checked') : false,
            origLabel: orig,
            progressIds: { progId: 'ipProg', textId: 'ipText', cancelId: 'ipCancel', inline: true },
            compactAlert: true,
            updateEmptyState: true,
            formatProgress: function (done, total) {
                return total > 0 ? ('已完成 ' + done + '/' + total + ' 个月') : '准备中…';
            },
            onPartialResult: function (partialResult, _meta, isFromError) {
                setAnalysisEmptyState(dim, 'empty', isFromError
                    ? '本次分析未完整完成，当前显示已完成部分。'
                    : '本次只分析了所选单元，尚未生成完整维度结果。');
                if (renderFn) renderFn(partialResult);
                return isFromError
                    ? '分析未完整完成；已显示可用的部分结果，未保存为完整维度结果。'
                    : '部分分析已显示，但未保存为完整结果；可重新预览并补齐。';
            },
            onFullDone: function (result) {
                if (renderFn) renderFn(result);
                return '分析完成';
            }
        });
    });
}

$(function () {
    AiOutputFeed.initDomEvents();
    restoreLatestAnalysisJob();
});
