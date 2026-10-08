// Citation checks use only the current session's cached results and local messages.
(function () {
    var sequence = 0, contextSequence = 0, trigger = null;
    var labels = { unique: '原文唯一匹配', multiple: '存在多个候选', not_found: '未找到一致来源', insufficient: '信息不足 / 非原文证据' };

    function messageRow(message, highlight) {
        var row = $('<div>').addClass('msg-row').toggleClass('msg-hit', message.id === highlight);
        row.append($('<span>').addClass('msg-time').text(message.time + ' '));
        row.append($('<span>').addClass('msg-who').text((message.name || '未知') + '：'));
        row.append($('<span>').addClass('msg-body').text(message.text));
        if (message.recalled) row.append($('<span>').addClass('badge bg-secondary ms-2').text('已撤回'));
        if (message.system) row.append($('<span>').addClass('badge bg-secondary ms-2').text('系统'));
        if (message.is_reply) row.append($('<span>').addClass('badge bg-info ms-2').text('回复'));
        return row;
    }

    function openContext(messageId) {
        var token = ++contextSequence;
        var host = $('#evidenceContext').empty().text('正在读取原始时间线…').attr('aria-busy', 'true');
        $.get('/api/messages', { around: messageId, context: 1 }, function (result) {
            if (token !== contextSequence) return;
            host.empty().attr('aria-busy', 'false');
            (result.messages || []).forEach(function (message) { host.append(messageRow(message, messageId)); });
            var hit = host.find('.msg-hit')[0];
            if (hit) hit.scrollIntoView({ block: 'center' });
        }).fail(function (xhr) {
            if (token !== contextSequence) return;
            host.attr('aria-busy', 'false').text((xhr.responseJSON && xhr.responseJSON.error) || '读取上下文失败，请重试。');
        });
    }

    function openEvidence(entry, button) {
        trigger = button;
        var dialog = document.getElementById('evidenceDialog');
        if (!dialog.open) dialog.showModal();
        if (entry.status === 'unique') {
            openContext(entry.candidates[0].id);
            return;
        }
        ++contextSequence;
        var host = $('#evidenceContext').empty().attr('aria-busy', 'false');
        host.append($('<p>').addClass('small text-muted').text('请选择要查看的候选消息；存在多个来源时不会自动认定其中一个。'));
        if (entry.candidate_count > entry.candidates.length) {
            host.append($('<p>').text('候选较多，仅显示前 ' + entry.candidates.length + ' 条。'));
        }
        entry.candidates.forEach(function (message) {
            var candidate = $('<button type="button">').addClass('btn btn-outline-secondary text-start w-100 mb-2');
            candidate.append(messageRow(message));
            candidate.on('click', function () { openContext(message.id); });
            host.append(candidate);
        });
    }

    window.updateEvidence = function (dimension) {
        if (!document.getElementById('evidencePanel')) return;
        var token = ++sequence;
        $.get('/api/evidence/' + encodeURIComponent(dimension), function (result) {
            if (token !== sequence) return;
            var host = $('#evidenceList').empty();
            $('#evidencePanel').removeClass('d-none');
            if (!(result.entries || []).length) host.text('当前结果没有可独立核对的原文证据字段。');
            (result.entries || []).forEach(function (entry) {
                var row = $('<div>').addClass('border-bottom pb-2 mb-2');
                row.append($('<div>').addClass('small mb-1').text(entry.text));
                row.append($('<span>').addClass('badge bg-secondary me-2').text(labels[entry.status] || labels.insufficient));
                if (entry.candidates && entry.candidates.length) {
                    var button = $('<button type="button">').addClass('btn btn-sm btn-outline-primary')
                        .text(entry.status === 'unique' ? '查看原文' : '查看候选（' + entry.candidate_count + '）');
                    button.on('click', function () { openEvidence(entry, this); });
                    row.append(button);
                }
                host.append(row);
            });
            if ((result.entries || []).length >= result.limit) host.append($('<p>').text('证据较多，本页仅展示前 ' + result.limit + ' 项。'));
        }).fail(function (xhr) {
            if (token !== sequence) return;
            if (xhr.status === 404) { $('#evidencePanel').addClass('d-none'); return; }
            $('#evidencePanel').removeClass('d-none');
            $('#evidenceList').text('原文核验暂时不可用，请稍后重试。');
        });
    };

    $(function () {
        var dialog = document.getElementById('evidenceDialog');
        if (!dialog) return;
        $('#evidenceClose').on('click', function () { dialog.close(); });
        $(dialog).on('click', function (event) { if (event.target === dialog) dialog.close(); });
        dialog.addEventListener('close', function () {
            ++contextSequence;
            if (trigger && document.contains(trigger)) trigger.focus();
        });
    });
})();
