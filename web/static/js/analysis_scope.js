// Session-scoped analysis controls. A scope is shared by preview, execution,
// cached result lookup and citation verification through the server session.
(function () {
    var state = {
        scope: {}, senders: [], loaded: false, waiters: [], data: null,
        identity: null, pageIdentity: window.ANALYSIS_PAGE_IDENTITY || null, scopeRequestId: 0
    };
    var scopeReloadPending = false;

    function identityFrom(data) {
        if (!data || typeof data.chat_hash !== 'string' || !data.chat_hash ||
            typeof data.scope_id !== 'string') return null;
        return { chat_hash: data.chat_hash, scope_id: data.scope_id };
    }

    function sameIdentity(left, right) {
        return !!left && !!right && left.chat_hash === right.chat_hash && left.scope_id === right.scope_id;
    }

    function applyScopeData(data) {
        state.data = data;
        state.scope = data.scope || {};
        state.senders = data.senders || [];
        state.summary = data.summary || '';
        state.warning = data.warning || '';
        state.identity = identityFrom(data);
        state.loaded = true;
        refreshButton();
    }

    function finishScopeLoad(error) {
        if (!state.loading) return;
        state.loading = false;
        if (!state.data) state.loaded = false;
        state.waiters.splice(0).forEach(function (done) { done(error || null); });
    }

    function fetchScopeData(callback) {
        var requestId = ++state.scopeRequestId;
        $.get('/api/analysis-scope').done(function (data) {
            if (requestId !== state.scopeRequestId) {
                callback(null, '分析范围读取已被较新的请求取代，请重试。', false);
                return;
            }
            var identity = identityFrom(data);
            var error = null;
            if (state.pageIdentity && identity && !sameIdentity(state.pageIdentity, identity)) {
                state.data = data;
                state.identity = identity;
                handleScopeChanged();
                error = '聊天或分析范围已在其他标签页更改，正在刷新。';
            } else {
                applyScopeData(data);
            }
            finishScopeLoad(error);
            callback(data, error, true);
        }).fail(function (xhr) {
            if (requestId !== state.scopeRequestId) {
                callback(null, '分析范围读取已被较新的请求取代，请重试。', false);
                return;
            }
            var error = (xhr.responseJSON && xhr.responseJSON.error) || '读取分析范围失败';
            finishScopeLoad(error);
            callback(null, error, true);
        });
    }

    // Legacy sessionStorage entries have no scope identity. Once a scoped page
    // is opened, remove them so clearing the scope cannot make an old result
    // appear to belong to the full conversation.
    function clearUnscopedSessionResults() {
        try {
            for (var i = sessionStorage.length - 1; i >= 0; i--) {
                var key = sessionStorage.key(i);
                if (key && key.indexOf('ai_') === 0) sessionStorage.removeItem(key);
            }
        } catch (e) { /* storage may be unavailable in private browsing */ }
    }

    // Old entries carry no chat or scope identity and must never be reused.
    clearUnscopedSessionResults();

    function refreshButton() {
        var button = document.getElementById('analysisScopeButton');
        if (!button) return;
        var active = !!(state.scope && Object.keys(state.scope).length);
        window.ANALYSIS_SCOPE_ACTIVE = active;
        if (active) clearUnscopedSessionResults();
        button.textContent = active ? '分析范围：已限定' : '分析范围：全量对话';
        button.title = state.warning || state.summary || (active ? '已设置分析范围' : '当前使用全部消息');
        button.classList.toggle('btn-outline-primary', !active);
        button.classList.toggle('btn-primary', active);
        var summary = document.getElementById('analysisScopeSummary');
        if (summary) {
            summary.textContent = active ? (state.summary || '已限定分析范围') : '';
            summary.title = active ? (state.warning || state.summary || '') : '';
            summary.classList.toggle('d-none', !active);
        }
    }

    function loadState(callback) {
        if (state.loaded) { if (callback) callback(); return; }
        state.waiters.push(callback || function () {});
        if (state.loading) return;
        state.loading = true;
        fetchScopeData(function (_data, _error, current) {
            // The latest scope request settles all queued callers. An older
            // response must not release those callers with stale identity.
            if (!current) return;
        });
    }

    window.getAnalysisPageIdentity = function (callback) {
        loadState(function (error) {
            callback(error ? null : state.pageIdentity, error || null);
        });
    };

    window.refreshAnalysisIdentity = function (callback) {
        fetchScopeData(function (data, error) {
            callback(data ? identityFrom(data) : null, error || null);
        });
    };

    window.validateAnalysisIdentity = function (metadata, callback) {
        var responseIdentity = identityFrom(metadata);
        if (!responseIdentity) {
            callback(false, null, '分析响应缺少聊天或范围身份，未显示该结果。');
            return;
        }
        window.getAnalysisPageIdentity(function (pageIdentity, pageError) {
            if (!pageIdentity) {
                callback(false, null, pageError || '无法确认当前聊天与分析范围，请刷新后重试。');
                return;
            }
            // Always re-read server state. This catches changes made by another
            // tab even when the broadcast event was missed or the session cookie
            // was updated outside this page.
            window.refreshAnalysisIdentity(function (currentIdentity, refreshError) {
                if (!currentIdentity) {
                    callback(false, null, refreshError || '无法确认当前聊天与分析范围，请刷新后重试。');
                    return;
                }
                if (!sameIdentity(currentIdentity, pageIdentity)) {
                    if (!scopeReloadPending) {
                        scopeReloadPending = true;
                        window.location.reload();
                    }
                    callback(false, null, '聊天或分析范围已在其他标签页更改，正在刷新。');
                    return;
                }
                if (!sameIdentity(responseIdentity, pageIdentity)) {
                    callback(false, null, '分析结果属于其他聊天或范围，未显示该结果。');
                    return;
                }
                callback(true, pageIdentity, null);
            });
        });
    };

    function resultCacheKey(dim, identity) {
        return 'ai:v2:' + encodeURIComponent(identity.chat_hash) + ':' +
            encodeURIComponent(identity.scope_id) + ':' + encodeURIComponent(dim);
    }

    window.cacheAnalysisResult = function (dim, result, identity) {
        if (!identity || !result) return false;
        try {
            sessionStorage.setItem(resultCacheKey(dim, identity), JSON.stringify({
                chat_hash: identity.chat_hash, scope_id: identity.scope_id, result: result
            }));
            return true;
        } catch (e) { return false; }
    };

    window.readCachedAnalysisResult = function (dim, identity) {
        if (!identity) return null;
        try {
            var raw = sessionStorage.getItem(resultCacheKey(dim, identity));
            var entry = raw ? JSON.parse(raw) : null;
            if (!entry || entry.chat_hash !== identity.chat_hash || entry.scope_id !== identity.scope_id || !entry.result) return null;
            return entry.result;
        } catch (e) { return null; }
    };

    window.acceptAnalysisResult = function (dim, result, metadata, callback, cacheResult) {
        window.validateAnalysisIdentity(metadata, function (valid, identity, error) {
            if (valid && cacheResult) window.cacheAnalysisResult(dim, result, identity);
            callback(valid, identity, error);
        });
    };

    function labelFor(uid) {
        var sender = state.senders.find(function (item) { return item.uid === uid; });
        if (!sender) return uid;
        var sameName = state.senders.filter(function (item) { return item.name === sender.name; }).length > 1;
        var label = (sender.is_self ? '我' : sender.name) + '（' + sender.name + '）';
        return sameName ? label + ' · ' + sender.uid : label;
    }

    function openEditor() {
        loadState(function (error) {
            if (error) { window.alert(error); return; }
            var old = document.getElementById('analysisScopeDialog');
            if (old) old.remove();
            var dialog = document.createElement('dialog');
            dialog.id = 'analysisScopeDialog';
            dialog.className = 'analysis-scope-dialog';
            dialog.innerHTML = '<form method="dialog" class="analysis-scope-form">' +
                '<div class="analysis-scope-head"><h3>设置分析范围</h3>' +
                '<button type="button" class="btn-close" aria-label="关闭"></button></div>' +
                '<p class="small text-muted">日期按 UTC+8 解释，起止日都包含。目标成员只决定生成哪些人物 / 成员画像；允许发送的成员决定哪些消息可进入模型输入。</p>' +
                '<div class="row g-2 mb-3"><div class="col-6"><label class="form-label" for="scopeDateFrom">开始日期</label>' +
                '<input class="form-control" id="scopeDateFrom" type="date"></div><div class="col-6">' +
                '<label class="form-label" for="scopeDateTo">结束日期</label><input class="form-control" id="scopeDateTo" type="date"></div></div>' +
                '<label class="form-check mb-2"><input class="form-check-input" id="scopeRestrictSenders" type="checkbox">' +
                '<span class="form-check-label">只发送选中成员的消息</span></label>' +
                '<select class="form-select mb-3" id="scopeAllowedSenders" multiple size="5" aria-label="允许发送消息的成员"></select>' +
                '<label class="form-check mb-2"><input class="form-check-input" id="scopeRestrictTargets" type="checkbox">' +
                '<span class="form-check-label">只评价选中成员</span></label>' +
                '<select class="form-select mb-2" id="scopeTargetMembers" multiple size="5" aria-label="画像目标成员"></select>' +
                '<div class="small text-muted mb-3" id="scopeFragmentSummary"></div>' +
                '<div class="alert alert-secondary py-2 small">仅使用范围内消息。不会自动补入范围外的回复、邻近消息或证据；片段模式最多 5000 条。</div>' +
                '<div class="analysis-scope-footer"><button type="button" class="btn btn-outline-secondary" id="scopeClear">恢复全量</button>' +
                '<div class="d-flex gap-2"><button type="button" class="btn btn-outline-secondary" id="scopeCancel">取消</button>' +
                '<button type="button" class="btn btn-primary" id="scopeSave">保存范围</button></div></div></form>';
            document.body.appendChild(dialog);

            var selectedAllowed = new Set(state.scope.allowed_uids || []);
            var selectedTargets = new Set(state.scope.target_uids || []);
            state.senders.forEach(function (sender) {
                [
                    [dialog.querySelector('#scopeAllowedSenders'), selectedAllowed],
                    [dialog.querySelector('#scopeTargetMembers'), selectedTargets]
                ].forEach(function (entry) {
                    var option = document.createElement('option');
                    option.value = sender.uid;
                    option.textContent = labelFor(sender.uid);
                    option.selected = entry[1].has(sender.uid);
                    entry[0].appendChild(option);
                });
            });
            var from = dialog.querySelector('#scopeDateFrom');
            var to = dialog.querySelector('#scopeDateTo');
            from.value = state.scope.date_from || '';
            to.value = state.scope.date_to || '';
            var restrictSenders = dialog.querySelector('#scopeRestrictSenders');
            var allowedSelect = dialog.querySelector('#scopeAllowedSenders');
            var restrictTargets = dialog.querySelector('#scopeRestrictTargets');
            var targetSelect = dialog.querySelector('#scopeTargetMembers');
            restrictSenders.checked = state.scope.allowed_uids != null;
            restrictTargets.checked = state.scope.target_uids != null;
            allowedSelect.disabled = !restrictSenders.checked;
            targetSelect.disabled = !restrictTargets.checked;
            restrictSenders.addEventListener('change', function () { allowedSelect.disabled = !restrictSenders.checked; });
            restrictTargets.addEventListener('change', function () { targetSelect.disabled = !restrictTargets.checked; });
            dialog.querySelector('#scopeFragmentSummary').textContent = state.warning || (state.scope.message_positions
                ? ('当前还限定为 ' + state.scope.message_positions.length + ' 条选中消息；若更改日期或发言人，这些条件会继续同时生效。')
                : '可在“消息”页勾选具体片段，再将这些消息作为分析范围。');

            function close() { if (dialog.open) dialog.close(); dialog.remove(); }
            dialog.querySelector('.btn-close').addEventListener('click', close);
            dialog.querySelector('#scopeCancel').addEventListener('click', close);
            dialog.addEventListener('click', function (event) { if (event.target === dialog) close(); });
            dialog.querySelector('#scopeSave').addEventListener('click', function () {
                if (restrictSenders.checked && !allowedSelect.selectedOptions.length) {
                    window.alert('请至少选择一位允许发送消息的成员。'); return;
                }
                if (restrictTargets.checked && !targetSelect.selectedOptions.length) {
                    window.alert('请至少选择一位画像目标成员。'); return;
                }
                var scope = {
                    date_from: from.value || '', date_to: to.value || '',
                    allowed_uids: restrictSenders.checked ? Array.from(allowedSelect.selectedOptions).map(function (item) { return item.value; }) : null,
                    target_uids: restrictTargets.checked ? Array.from(targetSelect.selectedOptions).map(function (item) { return item.value; }) : null,
                    message_positions: state.scope.message_positions || null
                };
                saveScope(scope, close, true);
            });
            dialog.querySelector('#scopeClear').addEventListener('click', function () { saveScope({}, close, true); });
            if (dialog.showModal) dialog.showModal(); else window.alert('当前浏览器不支持范围设置弹窗');
        });
    }

    var scopeChannel = null;

    function broadcastScopeChanged() {
        try {
            // Reuse the receiving channel: BroadcastChannel excludes the
            // sending object, not every object in the same document. Creating
            // a temporary sender here would make this page reload itself.
            if (scopeChannel) scopeChannel.postMessage({ type: 'scope-changed' });
        } catch (e) { /* storage event below remains available */ }
        try { localStorage.setItem('qqchat-analysis-scope-change', String(Date.now()) + Math.random()); }
        catch (e) { /* server identity validation still protects result rendering */ }
    }

    function handleScopeChanged() {
        if (scopeReloadPending) return;
        scopeReloadPending = true;
        window.location.reload();
    }

    try {
        if (window.BroadcastChannel) {
            scopeChannel = new BroadcastChannel('qqchat-analysis-scope');
            scopeChannel.addEventListener('message', function (event) {
                if (event.data && event.data.type === 'scope-changed') handleScopeChanged();
            });
        }
    } catch (e) { /* localStorage event is the fallback */ }
    window.addEventListener('storage', function (event) {
        if (event.key === 'qqchat-analysis-scope-change') handleScopeChanged();
    });

    function saveScope(scope, done, reloadPage) {
        $.ajax({ url: '/api/analysis-scope', method: 'POST', contentType: 'application/json', data: JSON.stringify({ scope: scope }) })
            .done(function () {
                broadcastScopeChanged();
                if (done) done();
                if (reloadPage) window.location.reload();
            })
            .fail(function (xhr) { window.alert((xhr.responseJSON && xhr.responseJSON.error) || '保存分析范围失败'); });
    }

    window.setAnalysisMessageScope = function (messagePositions, done) {
        loadState(function (error) {
            if (error) { window.alert(error); return; }
            if (!Array.isArray(messagePositions) || !messagePositions.length) { window.alert('请先勾选消息片段。'); return; }
            if (messagePositions.length > 5000) { window.alert('一次最多选择 5000 条消息。'); return; }
            var scope = Object.assign({}, state.scope, { message_positions: messagePositions });
            saveScope(scope, done, false);
        });
    };

    var button = document.getElementById('analysisScopeButton');
    if (button) button.addEventListener('click', openEditor);
    loadState();
})();
