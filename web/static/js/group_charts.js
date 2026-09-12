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
// 群聊图表与 AI 结果渲染（只被 group_*.html 加载）
// ================================================
// 复用 charts.js 里已有的全局件：esc / themeTokens / T / applyChartTheme / mountChart。
// **不修改 charts.js**：它的每个函数都服务私聊页面，动它就得重跑私聊的全部渲染对照。
//
// 三条纪律：
// 1. 一切来自模型的数据都经 esc() 转义后再进 DOM —— AI 输出里可能有 HTML；
// 2. 模型偶尔把该给数组的字段写成字符串：统一用 asList 兜底，别让一处 TypeError
//    把整页渲染打断（后面的卡片会整块空白，用户以为"分析没结果"）；
// 3. 精确（回复/@）与推断（接话）在图上必须**看得出区别**（实线 vs 虚线），
//    不能把推断画得和事实一样重。

// ---------------------------------------------------------------- 小工具

function asList(v) {
    if (Array.isArray(v)) return v;
    return (v === undefined || v === null || v === '') ? [] : [v];
}

function joinEsc(arr) {
    return asList(arr).map(esc).join('、');
}

function pct(x) {
    return Math.round((Number(x) || 0) * 100) + '%';
}

// 逐月结果的稳定排序（服务端已按月份排序，这里再兜一层，防止对象键序在不同引擎上不同）
function monthsOf(data) {
    return Object.keys(data || {}).sort();
}

// ---------------------------------------------------------------- 图表

// 成员发言条数（横向柱状）：人数多的群只画前 N 位，其余在标题里说明
function renderMemberBar(domId, activity, maxN) {
    var chart = mountChart(domId);
    if (!chart) return;
    var limit = maxN || 15;
    var rows = asList(activity).slice(0, limit).slice().reverse();
    chart.setOption(applyChartTheme({
        grid: { left: 8, right: 40, top: 10, bottom: 10, containLabel: true },
        tooltip: {
            trigger: 'axis', axisPointer: { type: 'shadow' },
            formatter: function (ps) {
                var p = ps[0];
                var item = rows[p.dataIndex] || {};
                return esc(item.name) + '<br/>发言 ' + (item.msg_count || 0) + ' 条 · 占 ' + pct(item.share) +
                    '<br/>活跃 ' + (item.active_days || 0) + ' 天 · 平均 ' + (item.avg_chars || 0) + ' 字/条';
            }
        },
        xAxis: { type: 'value' },
        yAxis: { type: 'category', data: rows.map(function (m) { return m.name; }), axisLabel: { color: T.axis } },
        series: [{
            type: 'bar', barMaxWidth: 16,
            data: rows.map(function (m) { return m.msg_count || 0; }),
            itemStyle: {
                color: function (p) { return rows[p.dataIndex] && rows[p.dataIndex].is_self ? T.accent2 : T.primary; },
                borderRadius: [0, 3, 3, 0]
            },
            label: { show: true, position: 'right', color: T.text, fontSize: 11 }
        }]
    }));
}

// 互动热力矩阵：X = 谁先说，Y = 谁接话；对角线留空（自己不接自己的话）
function renderInteractionHeatmap(domId, interaction) {
    var chart = mountChart(domId);
    if (!chart) return;
    var members = asList(interaction && interaction.members);
    var matrix = asList(interaction && interaction.directed);
    var names = members.map(function (m) { return m.name; });
    var data = [];
    var max = 0;
    for (var i = 0; i < matrix.length; i++) {
        for (var j = 0; j < (matrix[i] || []).length; j++) {
            var v = matrix[i][j] || 0;
            if (i === j) v = null;
            if (v > max) max = v;
            data.push([j, i, v]);
        }
    }
    chart.setOption(applyChartTheme({
        tooltip: {
            position: 'top',
            formatter: function (p) {
                if (p.value[2] === null) return esc(names[p.value[1]]) + '（自己不接自己的话）';
                return esc(names[p.value[0]]) + ' 说完，' + esc(names[p.value[1]]) + ' 接了 ' + p.value[2] + ' 次';
            }
        },
        grid: { left: 8, right: 16, top: 16, bottom: 56, containLabel: true },
        xAxis: { type: 'category', data: names, splitArea: { show: true }, axisLabel: { rotate: 45, color: T.axis } },
        yAxis: { type: 'category', data: names, splitArea: { show: true }, axisLabel: { color: T.axis } },
        visualMap: {
            min: 0, max: Math.max(1, max), calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
            inRange: { color: T.heat }, textStyle: { color: T.text }
        },
        series: [{ type: 'heatmap', data: data, emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,.25)' } } }]
    }));
}

// 关系图：实线 = 精确回复/@（事实），虚线 = 相邻接话（推断）；节点大小 = 发言量
function renderRelationGraph(domId, interaction, activity) {
    var chart = mountChart(domId);
    if (!chart) return;
    interaction = interaction || {};
    var members = asList(interaction.members);
    var vols = {};
    asList(activity).forEach(function (a) { vols[a.uid] = a.msg_count || 0; });
    var maxVol = 1;
    members.forEach(function (m) { maxVol = Math.max(maxVol, vols[m.uid] || 0); });
    var nodes = members.map(function (m) {
        var vol = vols[m.uid] || 0;
        return {
            id: m.uid, name: m.name,
            symbolSize: 12 + 34 * Math.sqrt(vol / maxVol),
            itemStyle: { color: m.is_self ? T.accent2 : T.primary },
            value: vol
        };
    });
    var known = {};
    members.forEach(function (m) { known[m.uid] = true; });
    function edgesOf(list, dashed) {
        return asList(list).map(function (e) {
            var a = members[e.source], b = members[e.target];
            return {
                source: a ? a.uid : '', target: b ? b.uid : '',
                value: e.value,
                lineStyle: { width: 1 + Math.min(6, Math.sqrt(e.value)), type: dashed ? 'dashed' : 'solid', opacity: dashed ? 0.45 : 0.8 }
            };
        }).filter(function (e) { return e.source && e.target && known[e.source] && known[e.target]; });
    }
    var solid = edgesOf(interaction.explicit_edges);
    var mentions = edgesOf(
        (function () {
            // @点名没有现成的无向边表：从 directed 生成（双向合计）
            var m = asList(interaction.mention_directed), out = [];
            for (var i = 0; i < m.length; i++) {
                for (var j = i + 1; j < (m[i] || []).length; j++) {
                    var v = (m[i][j] || 0) + (m[j][i] || 0);
                    if (v) out.push({ source: i, target: j, value: v });
                }
            }
            return out;
        })()
    );
    var dashed = edgesOf(interaction.edges, true);
    chart.setOption(applyChartTheme({
        tooltip: {
            formatter: function (p) {
                if (p.dataType === 'edge') return '互动 ' + p.data.value + ' 次';
                return esc(p.data.name) + '<br/>发言 ' + (p.data.value || 0) + ' 条';
            }
        },
        legend: { data: ['精确回复（事实）', '@点名（事实）', '接话（推断）'], bottom: 0, textStyle: { color: T.text } },
        series: [{
            type: 'graph', layout: 'force', roam: true, draggable: true,
            force: { repulsion: 260, edgeLength: [50, 130], gravity: 0.08 },
            label: { show: true, position: 'right', color: T.text, fontSize: 11 },
            emphasis: { focus: 'adjacency' },
            data: nodes,
            edges: solid.concat(mentions).concat(dashed),
            categories: [
                { name: '精确回复（事实）', itemStyle: { color: T.primary } },
                { name: '@点名（事实）', itemStyle: { color: T.heat[1] } },
                { name: '接话（推断）', itemStyle: { color: T.axis } }
            ]
        }]
    }));
    // 图例分组：给三类边各自着色（ECharts 的 graph 边不分 category，这里按线型+颜色区分）
    chart.setOption({
        series: [{
            edges: solid.map(function (e) { return Object.assign({}, e, { lineStyle: Object.assign({}, e.lineStyle, { type: 'solid' }) }); })
                .concat(mentions.map(function (e) { return Object.assign({}, e, { lineStyle: Object.assign({}, e.lineStyle, { type: 'solid' }) }); }))
                .concat(dashed)
        }]
    });
}

// 成员活跃时段堆叠面积图：X = 24 小时，Y = 消息数，按成员堆叠（只画前 N 位）
function renderMemberHourlyArea(domId, memberHourly, maxN) {
    var chart = mountChart(domId);
    if (!chart) return;
    memberHourly = memberHourly || {};
    var hours = asList(memberHourly.hours);
    var series = asList(memberHourly.series).slice(0, maxN || 8);
    chart.setOption(applyChartTheme({
        tooltip: { trigger: 'axis' },
        legend: { type: 'scroll', top: 0, textStyle: { color: T.text } },
        grid: { left: 8, right: 16, top: 36, bottom: 24, containLabel: true },
        xAxis: { type: 'category', boundaryGap: false, data: hours.map(function (h) { return h + '时'; }) },
        yAxis: { type: 'value' },
        series: series.map(function (s) {
            return {
                name: s.name, type: 'line', stack: 'total', smooth: true, showSymbol: false,
                areaStyle: { opacity: 0.55 }, emphasis: { focus: 'series' },
                data: asList(s.counts)
            };
        })
    }));
}

// 成员雷达：五个维度都来自本地精确统计（发言量/被回复/被@/互动广度/活跃天数）
function renderMemberRadar(domId, totals, maxN) {
    var chart = mountChart(domId);
    if (!chart) return;
    var rows = asList(totals).slice(0, maxN || 6);
    if (!rows.length) return;
    function maxOf(key) {
        return Math.max(1, rows.reduce(function (m, r) { return Math.max(m, r[key] || 0); }, 0));
    }
    var scales = {
        received: maxOf('replied_by'), sent: maxOf('replies_to'),
        mentionIn: maxOf('mentions_received'), mentionOut: maxOf('mentions_sent')
    };
    chart.setOption(applyChartTheme({
        tooltip: {},
        legend: { type: 'scroll', bottom: 0, textStyle: { color: T.text } },
        radar: {
            indicator: [
                { name: '被接话', max: scales.received },
                { name: '接别人话', max: scales.sent },
                { name: '被@', max: scales.mentionIn },
                { name: '@别人', max: scales.mentionOut },
                { name: '精确回复', max: Math.max(1, maxOf('explicit_replied_by')) }
            ],
            axisName: { color: T.axis, fontSize: 11 },
            splitLine: { lineStyle: { color: T.split } },
            splitArea: { areaStyle: { color: ['transparent'] } }
        },
        series: [{
            type: 'radar',
            data: rows.map(function (r) {
                return {
                    name: r.name,
                    value: [r.replied_by || 0, r.replies_to || 0, r.mentions_received || 0, r.mentions_sent || 0, r.explicit_replied_by || 0]
                };
            })
        }]
    }));
}

// ---------------------------------------------------------------- AI 结果渲染

function section(title) {
    return '<h6 class="text-muted mt-3 mb-2">' + esc(title) + '</h6>';
}

function kvTable(rows) {
    var html = '<table class="ai-detail">';
    rows.forEach(function (r) {
        if (r[1] === undefined || r[1] === null || r[1] === '') return;
        html += '<tr><th>' + esc(r[0]) + '</th><td>' + r[1] + '</td></tr>';
    });
    return html + '</table>';
}

// 群聊动态（逐月）
function renderGroupDynamics(container, data) {
    var months = monthsOf(data);
    if (!months.length) return false;
    var html = '';
    months.forEach(function (m) {
        var d = data[m] || {};
        html += '<div class="card mb-3"><div class="card-body">';
        html += '<div class="d-flex justify-content-between align-items-baseline"><h6 class="mb-2">' + esc(m) +
            '</h6><span class="badge bg-secondary">置信度 ' + esc(d.confidence || '未知') + '</span></div>';
        if (d.group_vibe) html += '<p class="mb-2"><strong>' + esc(d.group_vibe) + '</strong></p>';
        var core = asList(d.core_members).map(function (c) {
            return '<span class="badge bg-primary me-1 mb-1">' + esc(c.name) + ' · ' + esc(c.role) + '</span>';
        }).join('');
        if (core) html += section('核心成员') + '<div>' + core + '</div>';
        var evidence = asList(d.core_members).map(function (c) {
            return c.evidence ? '<div class="small text-muted">' + esc(c.name) + '：' + esc(c.evidence) + '</div>' : '';
        }).join('');
        if (evidence) html += evidence;
        html += kvTable([
            ['群节奏', esc(d.pace)],
            ['权力结构', esc(d.power_structure)],
            ['潜水比例', d.lurker_ratio === undefined ? '' : pct(d.lurker_ratio)],
            ['游离的人', esc(d.newcomer_or_outsider)],
            ['我的角色', esc(d.self_role)]
        ]);
        var subs = asList(d.sub_groups);
        if (subs.length) {
            html += section('小圈子');
            html += subs.map(function (s) {
                return '<div class="small mb-1">' + joinEsc(s.members) + '　<span class="text-muted">' + esc(s.evidence) + '</span></div>';
            }).join('');
        }
        var conflicts = asList(d.conflict_moments);
        if (conflicts.length) {
            html += section('分歧/尴尬时刻') + '<ul class="small mb-0">' +
                conflicts.map(function (c) { return '<li>' + esc(c) + '</li>'; }).join('') + '</ul>';
        }
        html += '</div></div>';
    });
    container.innerHTML = html;
    return true;
}

// 群聊话题（逐月）
function renderGroupTopics(container, data) {
    var months = monthsOf(data);
    if (!months.length) return false;
    var html = '';
    months.forEach(function (m) {
        var d = data[m] || {};
        html += '<div class="card mb-3"><div class="card-body">';
        html += '<h6 class="mb-2">' + esc(d.month_title || m) + ' <span class="text-muted small">' + esc(m) + '</span></h6>';
        var topics = asList(d.topics);
        if (topics.length) {
            html += '<table class="ai-detail"><thead><tr><th>话题</th><th>比重</th><th>谁在聊</th><th>注解</th></tr></thead><tbody>';
            topics.forEach(function (t) {
                html += '<tr><td>' + esc(t.name) + '</td><td>' + pct(t.weight) + '</td><td>' +
                    (joinEsc(t.key_members) || '<span class="text-muted">—</span>') + '</td><td>' +
                    esc(t.one_liner) + '</td></tr>';
            });
            html += '</tbody></table>';
        }
        if (d.summary) html += '<p class="small mb-1">' + esc(d.summary) + '</p>';
        if (d.topic_shift_detected && d.shift_description) {
            html += '<p class="small text-muted mb-0">话题迁移：' + esc(d.shift_description) + '</p>';
        }
        html += '</div></div>';
    });
    container.innerHTML = html;
    return true;
}

// 群聊情绪（逐月）
function renderGroupEmotion(container, data) {
    var months = monthsOf(data);
    if (!months.length) return false;
    var html = '';
    months.forEach(function (m) {
        var d = data[m] || {};
        html += '<div class="card mb-3"><div class="card-body">';
        html += '<div class="d-flex justify-content-between align-items-baseline"><h6 class="mb-2">' + esc(m) + '</h6>' +
            '<span class="badge bg-info">' + esc(d.group_emotion || '数据不足') + ' ' +
            (d.group_intensity ? esc(d.group_intensity) + '/10' : '') + '</span></div>';
        if (d.group_evidence) html += '<p class="small mb-2">' + esc(d.group_evidence) + '</p>';
        html += kvTable([
            ['情绪走势', esc(d.emotion_flow)],
            ['转折点', esc(d.turning_point)],
            ['气氛担当', esc(d.atmosphere_maker)],
            ['冷场王', esc(d.atmosphere_killer)]
        ]);
        var members = asList(d.member_emotions);
        if (members.length) {
            html += section('成员情绪');
            html += members.map(function (x) {
                return '<div class="small mb-1"><strong>' + esc(x.name) + '</strong> ' + esc(x.emotion) +
                    (x.intensity ? '（' + esc(x.intensity) + '/10）' : '') +
                    (x.evidence ? '　<span class="text-muted">' + esc(x.evidence) + '</span>' : '') + '</div>';
            }).join('');
        }
        html += '</div></div>';
    });
    container.innerHTML = html;
    return true;
}

// 成员画像卡片网格
function renderMemberProfiles(container, data, people) {
    var profiles = Object.keys(data || {});
    if (!profiles.length) return false;
    var order = {};
    asList(people).forEach(function (p, i) { order[p.uid] = i; });
    profiles.sort(function (a, b) { return (order[a] === undefined ? 999 : order[a]) - (order[b] === undefined ? 999 : order[b]); });
    var html = '<div class="row">';
    profiles.forEach(function (uid) {
        var p = data[uid] || {};
        var g = p.group_specific || {};
        var pa = p.personality_analysis || {};
        var cs = p.chat_style_analysis || {};
        html += '<div class="col-md-6 mb-3"><div class="card h-100"><div class="card-body">';
        html += '<div class="d-flex justify-content-between align-items-baseline mb-1"><h6 class="mb-0">' + esc(p.name || uid) +
            (p.is_self ? ' <span class="badge bg-warning">我</span>' : '') + '</h6>' +
            '<span class="badge bg-secondary">' + esc(p.confidence || '未知') + '</span></div>';
        if (g.group_role) html += '<div class="mb-1"><span class="badge bg-primary">' + esc(g.group_role) + '</span></div>';
        if (p.overall_impression) html += '<p class="small mb-1"><strong>' + esc(p.overall_impression) + '</strong></p>';
        if (p.verdict) html += '<p class="small mb-2">' + esc(p.verdict) + '</p>';
        html += kvTable([
            ['互动模式', esc(g.reply_pattern)],
            ['出现方式', esc(g.presence)],
            ['思维风格', esc(pa.thinking_style)],
            ['幽默风格', esc(pa.humor_style)],
            ['口头禅', joinEsc(cs.signature_phrases)],
            ['标点习惯', esc(cs.punctuation_style)]
        ]);
        var facts = asList(p.fun_facts).slice(0, 2);
        if (facts.length) {
            html += '<ul class="small mb-1">' + facts.map(function (f) { return '<li>' + esc(f) + '</li>'; }).join('') + '</ul>';
        }
        if (p.roast_note) html += '<div class="small text-muted">' + esc(p.roast_note) + '</div>';
        html += '</div></div></div>';
    });
    html += '</div>';
    container.innerHTML = html;
    return true;
}

// 统一的"加载某个维度并渲染"入口（各页面只关心 container 与渲染函数）
function loadGroupAnalysis(dim, container, renderer) {
    if (!container) return;
    loadAnalysis(dim, function (data) {
        if (!data) return;
        var ok = false;
        try {
            ok = renderer(container, data);
        } catch (e) {
            container.innerHTML = '<div class="alert alert-warning small mb-0">结果渲染失败：' + esc(e.message) + '</div>';
            return;
        }
        if (ok === false) container.innerHTML = '<div class="text-muted small">暂无可用结果</div>';
    });
}

// 进度单位：群级维度按月、成员画像按人（后端 jobs.dimension_unit 的同一口径）
function groupDimensionUnit(dim) {
    return dim === 'member_profiles' ? '人' : '月';
}

// 单维度分析按钮的统一接线：发起 → 进度 → 完成后就地把结果渲染进 container
// （群聊维度不再跳转页面：结果直接显示在当前页的卡片里，用户不必来回切）
function analyzeGroupDimension(dim, btn, containerId, renderer, onFinish) {
    var status = $('#analyzeStatus');
    var refresh = $('#forceRefresh').is(':checked');
    var orig = btn.data('orig') || btn.text();
    var unit = groupDimensionUnit(dim);
    var opts = {
        refresh: refresh,
        onProgress: function (done, total) {
            if (done < 0) { $('#aText').text('正在取消…'); return; }
            if (total > 0) {
                $('#aProg').css('width', Math.round(done / total * 100) + '%');
                $('#aText').text('已完成 ' + done + '/' + total + '（' + unit + '）');
            } else { $('#aText').text('准备中…'); }
        },
        onDone: function (result) {
            try { sessionStorage.setItem('ai_' + dim, JSON.stringify(result)); } catch (e) { /* 配额满忽略 */ }
            if (containerId) {
                var el = document.getElementById(containerId);
                if (el && renderer) {
                    try { renderer(el, result); } catch (e) {
                        el.innerHTML = '<div class="alert alert-warning small mb-0">结果渲染失败：' + esc(e.message) + '</div>';
                    }
                }
            }
            status.html('<div class="alert alert-success mb-0">分析完成，结果已显示。</div>');
            btn.prop('disabled', false).text(orig);
            if (onFinish) onFinish();
        },
        onError: function (msg) {
            status.html('<div class="alert alert-danger mb-0">' + esc(msg) + '</div>');
            btn.prop('disabled', false).text(orig);
            if (onFinish) onFinish();
        }
    };
    btn.prop('disabled', true).text('分析中…');
    status.html(
        '<div class="spinner-border spinner-border-sm text-primary" role="status"></div>' +
        '<div class="progress mx-auto mt-2" style="max-width:320px;height:8px;">' +
        '<div id="aProg" class="progress-bar progress-bar-striped progress-bar-animated" style="width:0%"></div></div>' +
        '<div id="aText" class="small text-muted mt-1">准备中…</div>' +
        '<button id="aCancel" class="btn btn-sm btn-outline-secondary mt-2">取消</button>'
    );
    $('#aCancel').on('click', function () {
        $(this).prop('disabled', true).text('取消中…');
        cancelAnalyze(opts);
    });
    startAnalyze(dim, opts);
}

// 各群聊页面共用的按钮接线：#groupAnalyze 上的 data-dim / data-target 决定行为
// data-target = 渲染容器 id，渲染函数按维度名从 RENDERERS 里取
var GROUP_RENDERERS = {
    group_dynamics: renderGroupDynamics,
    group_topics: renderGroupTopics,
    group_emotion: renderGroupEmotion,
    member_profiles: renderMemberProfiles
};

function wireGroupAnalyzeButtons(members) {
    $('.btn-analyze').on('click', function () {
        var btn = $(this);
        var dim = btn.data('dim');
        var target = btn.data('target');
        var renderer = GROUP_RENDERERS[dim];
        if (dim === 'member_profiles' && members) {
            renderer = function (el, data) { return renderMemberProfiles(el, data, members); };
        }
        analyzeGroupDimension(dim, btn, target, renderer);
    });
}
