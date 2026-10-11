(function() {
    var root = $('#periodCompare');
    if (!root.length) return;

    function esc(value) {
        return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function fmt(value, digits) {
        if (value == null || !Number.isFinite(Number(value))) return '样本不足';
        return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits == null ? 1 : digits });
    }
    function deltaText(delta, unit) {
        if (!delta || delta.absolute == null) return '—';
        var amount = Number(delta.absolute);
        var sign = amount > 0 ? '+' : '';
        var percent = delta.percent == null ? '（基期为 0）' : '（' + (delta.percent > 0 ? '+' : '') + delta.percent + '%）';
        return sign + fmt(amount, 1) + (unit || '') + ' ' + percent;
    }
    function period(key) {
        return { from: $('#period' + key.toUpperCase() + 'From').val(), to: $('#period' + key.toUpperCase() + 'To').val() };
    }
    function summaryCard(label, a, b, delta, unit) {
        return '<div class="col-6 col-lg-4"><div class="border rounded p-3 h-100">' +
            '<div class="small text-muted">' + esc(label) + '</div>' +
            '<div class="fs-6 mt-1">A：' + esc(fmt(a)) + esc(unit || '') + '</div>' +
            '<div class="fs-5 fw-semibold">B：' + esc(fmt(b)) + esc(unit || '') +
            '</div><div class="small text-muted">B − A：' + esc(deltaText(delta, unit)) + '</div></div></div>';
    }
    function sideName(side) { return side === 'self' ? '我回复' : '对方回复'; }
    function render(res) {
        var a = res.a, b = res.b, ma = a.metrics, mb = b.metrics, d = res.deltas;
        var html = '<div class="small text-muted mb-2">A：' + esc(a.from) + ' 至 ' + esc(a.to) + '（' + a.calendar_days + ' 个自然日）' +
            '；B：' + esc(b.from) + ' 至 ' + esc(b.to) + '（' + b.calendar_days + ' 个自然日）</div>';
        if (a.boundary_truncated.before || a.boundary_truncated.after || b.boundary_truncated.before || b.boundary_truncated.after) {
            html += '<div class="alert alert-warning small py-2">所选区间边缘与区间外消息相邻，可能截断原本连续的会话；开启次数和回复中位数只按区间内消息计算。</div>';
        }
        var values = [
            ['消息数', 'total_messages', ' 条'], ['活跃天数', 'active_days', ' 天'], ['日均消息（自然日）', 'avg_daily', ' 条/天']
        ];
        if (res.mode === 'group') {
            values.push(['活跃成员', 'active_members', ' 人'], ['活跃时段', 'active_hours', ' 小时']);
        } else {
            values.push(['平均消息长度（字符）', 'avg_message_chars', ' 字'], ['会话开启次数', 'session_starts', ' 次']);
        }
        html += '<div class="row g-2">' + values.map(function(item) {
            return summaryCard(item[0], ma[item[1]], mb[item[1]], d[item[1]], item[2]);
        }).join('') + '</div>';

        if (res.mode === 'group') {
            var shares = d.member_shares || [];
            html += '<h6 class="mt-3">成员发言占比（按 UID 对齐）</h6><div class="table-responsive"><table class="table table-sm">' +
                '<thead><tr><th>成员</th><th class="text-end">A</th><th class="text-end">B</th><th class="text-end">变化</th></tr></thead><tbody>' +
                (shares.length ? shares.map(function(item) {
                    return '<tr><td>' + esc(item.name) + '</td><td class="text-end">' + fmt(item.a * 100, 1) + '%</td>' +
                        '<td class="text-end">' + fmt(item.b * 100, 1) + '%</td><td class="text-end">' +
                        (item.percentage_points > 0 ? '+' : '') + fmt(item.percentage_points, 1) + ' 个百分点</td></tr>';
                }).join('') : '<tr><td colspan="4" class="text-muted">没有可归属成员的消息</td></tr>') + '</tbody></table></div>';
            html += '<h6 class="mt-3">活跃小时（UTC+8）</h6><div class="small">A：' +
                (ma.hour_counts.map(function(count, hour) { return count ? hour + '时（' + count + '）' : ''; }).filter(Boolean).map(esc).join('、') || '无') +
                '<br>B：' + (mb.hour_counts.map(function(count, hour) { return count ? hour + '时（' + count + '）' : ''; }).filter(Boolean).map(esc).join('、') || '无') + '</div>';
        } else {
            html += '<h6 class="mt-3">回复速度中位数</h6><div class="row g-2">' + ['self', 'other'].map(function(side) {
                var av = ma.reply_median_seconds[side], bv = mb.reply_median_seconds[side];
                function speed(result) {
                    return result.value == null ? '样本不足' : fmt(result.value) + ' 秒（' + result.samples + ' 个样本）';
                }
                var delta = d.reply_median_seconds[side];
                return '<div class="col-md-6"><div class="border rounded p-3"><div class="small text-muted">' + sideName(side) + '</div>' +
                    '<div>A：' + esc(speed(av)) + '</div><div>B：' + esc(speed(bv)) + '</div>' +
                    '<div class="small text-muted">B − A：' + esc(deltaText(delta, ' 秒')) + '</div></div></div>';
            }).join('') + '</div><div class="small text-muted mt-2">缺少有效回复样本时显示“样本不足”；区间首尾可能截断会话，开启次数受此影响。</div>';
        }
        $('#periodCompareResult').html(html);
    }

    $('#comparePeriods').on('click', function() {
        var a = period('a'), b = period('b');
        if (!a.from || !a.to || !b.from || !b.to) {
            $('#periodCompareResult').html('<div class="alert alert-warning small">请填写 A、B 两段的开始和结束日期。</div>');
            return;
        }
        var button = $(this).prop('disabled', true).text('正在计算…');
        $.ajax({
            url: '/api/stats/compare', method: 'POST', contentType: 'application/json',
            data: JSON.stringify({ a: a, b: b })
        }).done(function(res) { render(res); }).fail(function(xhr) {
            var message = (xhr.responseJSON && xhr.responseJSON.error) || '对比失败，请刷新页面后重试。';
            $('#periodCompareResult').html('<div class="alert alert-danger small">' + esc(message) + '</div>');
        }).always(function() { button.prop('disabled', false).text('比较时段'); });
    });
})();
