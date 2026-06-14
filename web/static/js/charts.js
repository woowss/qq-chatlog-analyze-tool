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
// QQ 聊天记录分析 — ECharts 图表渲染
// ====================================

function renderPieChart(domId, data, name) {
    const el = document.getElementById(domId);
    if (!el) return;
    const chart = echarts.init(el);
    const colors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de'];
    chart.setOption({
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        legend: { bottom: 0 },
        series: [{
            type: 'pie',
            radius: ['40%', '65%'],
            center: ['50%', '45%'],
            data: data.map(function(d, i) {
                return $.extend({}, d, { itemStyle: { color: colors[i % colors.length] } });
            }),
            label: { show: true, formatter: '{b}\n{d}%' },
            emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } }
        }]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

function renderLineChart(domId, data, yName) {
    const el = document.getElementById(domId);
    if (!el) return;
    const chart = echarts.init(el);
    chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['对方', '自己'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '15%', containLabel: true },
        xAxis: {
            type: 'category',
            data: data.map(function(d) { return d.date; }),
            axisLabel: { rotate: 45, fontSize: 10 }
        },
        yAxis: { type: 'value', name: yName },
        dataZoom: [{ type: 'inside', start: 0, end: 100 }],
        series: [
            {
                name: '对方', type: 'line',
                data: data.map(function(d) { return d.other; }),
                smooth: true,
                lineStyle: { color: '#91cc75' },
                itemStyle: { color: '#91cc75' },
                areaStyle: { color: 'rgba(145,204,117,0.15)' }
            },
            {
                name: '自己', type: 'line',
                data: data.map(function(d) { return d.self; }),
                smooth: true,
                lineStyle: { color: '#5470c6' },
                itemStyle: { color: '#5470c6' },
                areaStyle: { color: 'rgba(84,112,198,0.15)' }
            }
        ]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

function renderBarChart(domId, data, yName) {
    const el = document.getElementById(domId);
    if (!el) return;
    const chart = echarts.init(el);
    chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['对方', '自己'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '15%', containLabel: true },
        xAxis: { type: 'category', data: data.map(function(d) { return d.hour + '时'; }) },
        yAxis: { type: 'value', name: yName },
        series: [
            {
                name: '对方', type: 'bar',
                data: data.map(function(d) { return d.other; }),
                itemStyle: { color: '#91cc75', borderRadius: [4,4,0,0] }
            },
            {
                name: '自己', type: 'bar',
                data: data.map(function(d) { return d.self; }),
                itemStyle: { color: '#5470c6', borderRadius: [4,4,0,0] }
            }
        ]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

function renderWeeklyChart(domId, data) {
    const el = document.getElementById(domId);
    if (!el) return;
    const chart = echarts.init(el);
    chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['对方', '自己'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '15%', containLabel: true },
        xAxis: { type: 'category', data: data.map(function(d) { return d.weekday_name; }) },
        yAxis: { type: 'value', name: '消息数' },
        series: [
            {
                name: '对方', type: 'bar',
                data: data.map(function(d) { return d.other; }),
                itemStyle: { color: '#91cc75', borderRadius: [4,4,0,0] }
            },
            {
                name: '自己', type: 'bar',
                data: data.map(function(d) { return d.self; }),
                itemStyle: { color: '#5470c6', borderRadius: [4,4,0,0] }
            }
        ]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

function renderResponseChart(domId, data) {
    const el = document.getElementById(domId);
    if (!el) return;
    const chart = echarts.init(el);
    chart.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: '10%', right: '10%', containLabel: true },
        xAxis: { type: 'category', data: [data.selfName, data.otherName] },
        yAxis: { type: 'value', name: '秒' },
        series: [{
            type: 'bar',
            data: [
                { value: data.self, itemStyle: { color: '#5470c6' } },
                { value: data.other, itemStyle: { color: '#91cc75' } }
            ],
            barWidth: '40%',
            label: { show: true, formatter: '{c}s', position: 'top' }
        }]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

function renderWordCloud(domId, data, title) {
    const el = document.getElementById(domId);
    if (!el) return;
    if (!data || !data.length) {
        el.innerHTML = '<div class="text-muted text-center py-4">暂无数据</div>';
        return;
    }
    const chart = echarts.init(el);
    var maxCount = data[0].count;
    var minCount = data[data.length - 1].count || 1;

    var colors = ['#1a237e','#2e7d32','#bf360c','#4a148c','#01579b','#e65100','#004d40','#b71c1c','#3e2723','#283593','#00695c','#37474f','#0d47a1','#33691e','#5d4037'];

    chart.setOption({
        title: { text: title, left: 'center', textStyle: { fontSize: 14 } },
        tooltip: { formatter: function(p) { return p.name + ': ' + p.value + ' 次'; } },
        series: [{
            type: 'wordCloud',
            shape: 'circle',
            left: 'center',
            top: 'center',
            width: '90%',
            height: '85%',
            sizeRange: [14, 48],
            rotationRange: [-20, 20],
            rotationStep: 20,
            gridSize: 10,
            drawOutOfBound: false,
            layoutAnimation: true,
            textStyle: {
                fontFamily: 'Microsoft YaHei, sans-serif',
                fontWeight: 'bold'
            },
            data: data.map(function(d) {
                var fontSize = 14 + 34 * (d.count - minCount) / (maxCount - minCount || 1);
                return {
                    name: d.word,
                    value: d.count,
                    textStyle: {
                        fontSize: fontSize,
                        color: colors[Math.floor(Math.random() * colors.length)]
                    }
                };
            })
        }]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

function renderFaceBarChart(domId, data, personName) {
    const el = document.getElementById(domId);
    if (!el) return;
    const entries = Object.entries(data).slice(0, 10);
    if (!entries.length) {
        el.innerHTML = '<div class="text-muted text-center py-4">表情数据不足</div>';
        return;
    }
    const chart = echarts.init(el);
    chart.setOption({
        tooltip: { trigger: 'axis', formatter: function(p) { return p.name + ': ' + p.value + ' 次'; } },
        grid: { left: '5%', right: '10%', containLabel: true },
        xAxis: { type: 'value', name: '次数' },
        yAxis: {
            type: 'category',
            data: entries.map(function(e) { return e[0]; }),
            axisLabel: { fontSize: 13, fontWeight: 'bold' }
        },
        series: [{
            type: 'bar',
            data: entries.map(function(e) { return e[1]; }),
            itemStyle: { color: '#5470c6', borderRadius: [0,4,4,0] },
            label: { show: true, position: 'right', fontWeight: 'bold' }
        }]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

function renderHeatmapChart(domId, data) {
    const el = document.getElementById(domId);
    if (!el) return;
    const chart = echarts.init(el);
    const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    var maxVal = 1;
    data.forEach(function(d) { if (d.count > maxVal) maxVal = d.count; });
    const heatData = data.map(function(d) {
        return [d.hour, d.weekday, d.count];
    });
    chart.setOption({
        tooltip: {
            formatter: function(params) {
                return weekdays[params.value[1]] + ' ' + params.value[0] + '时: ' + params.value[2] + '条';
            }
        },
        grid: { left: '5%', right: '5%', bottom: '10%', containLabel: true },
        xAxis: { type: 'category', data: Array.from({length:24}, function(_,i) { return i+'时'; }), splitArea: { show: true } },
        yAxis: { type: 'category', data: weekdays, splitArea: { show: true } },
        visualMap: { min: 0, max: maxVal, calculable: true, orient: 'horizontal', left: 'center', bottom: 0 },
        series: [{
            type: 'heatmap',
            data: heatData,
            label: { show: false },
            emphasis: { itemStyle: { shadowBlur: 10 } }
        }]
    });
    window.addEventListener('resize', function() { chart.resize(); });
}

// ========== AI 分析图表 ==========

function renderEmotionCharts(data) {
    const months = Object.keys(data).sort();
    var selfIntensity = months.map(function(m) { return data[m].self_intensity; });
    var otherIntensity = months.map(function(m) { return data[m].other_intensity; });
    var selfEmotions = months.map(function(m) { return data[m].self_emotion; });
    var otherEmotions = months.map(function(m) { return data[m].other_emotion; });

    // 情绪强度折线图
    var lineChart = echarts.init(document.getElementById('emotionLineChart'));
    lineChart.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                var idx = params[0].dataIndex;
                var m = months[idx];
                var html = '<strong>' + m + '</strong><br>';
                params.forEach(function(p) {
                    html += p.marker + ' ' + p.seriesName + ': ' + p.value + '<br>';
                });
                html += '😊 自己: ' + selfEmotions[idx] + '<br>';
                html += '😊 对方: ' + otherEmotions[idx];
                return html;
            }
        },
        legend: { data: ['自己情绪强度', '对方情绪强度'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '15%', containLabel: true },
        xAxis: { type: 'category', data: months },
        yAxis: { type: 'value', name: '情绪强度', min: 1, max: 10 },
        series: [
            {
                name: '自己情绪强度', type: 'line',
                data: selfIntensity, smooth: true,
                lineStyle: { color: '#5470c6', width: 3 },
                itemStyle: { color: '#5470c6' },
                areaStyle: { color: 'rgba(84,112,198,0.15)' }
            },
            {
                name: '对方情绪强度', type: 'line',
                data: otherIntensity, smooth: true,
                lineStyle: { color: '#91cc75', width: 3 },
                itemStyle: { color: '#91cc75' },
                areaStyle: { color: 'rgba(145,204,117,0.15)' }
            }
        ]
    });

    // 情绪分布饼图
    var selfEmoCount = {}, otherEmoCount = {};
    selfEmotions.forEach(function(e) { selfEmoCount[e] = (selfEmoCount[e] || 0) + 1; });
    otherEmotions.forEach(function(e) { otherEmoCount[e] = (otherEmoCount[e] || 0) + 1; });

    renderPieChart('selfEmotionPie',
        Object.keys(selfEmoCount).map(function(k) { return {name: k, value: selfEmoCount[k]}; }),
        '月份数');
    renderPieChart('otherEmotionPie',
        Object.keys(otherEmoCount).map(function(k) { return {name: k, value: otherEmoCount[k]}; }),
        '月份数');

    // 逐月详情
    var html = '';
    months.forEach(function(m) {
        var d = data[m];
        if (!d) return;
        html += '<div class="card mb-2"><div class="card-body py-2">' +
            '<strong>' + m + '</strong>' +
            '<span class="badge bg-primary ms-2">自己: ' + d.self_emotion + '(' + d.self_intensity + ')</span>' +
            '<span class="badge bg-success ms-1">对方: ' + d.other_emotion + '(' + d.other_intensity + ')</span>' +
            '<span class="badge bg-info ms-1">基调: ' + d.overall_tone + '</span>' +
            '<div class="mt-1 small text-muted">' +
            '自己关键词: ' + (d.self_keywords || []).join('、') + '<br>' +
            '对方关键词: ' + (d.other_keywords || []).join('、') +
            '</div></div></div>';
    });
    $('#emotionDetails').html(html);
}

function renderRelationshipInsight(data) {
    var months = Object.keys(data).sort();
    var html = '';
    months.forEach(function(m) {
        var d = data[m];
        if (!d) return;
        html += '<div class="card mb-2"><div class="card-body py-2">' +
            '<strong>' + m + '</strong>' +
            '<span class="badge bg-info ms-2">亲密: ' + d.closeness_score + '/10</span>' +
            '<span class="badge bg-secondary ms-1">趋势: ' + d.closeness_trend + '</span>' +
            '<span class="badge bg-warning ms-1">风格: ' + d.interaction_style + '</span>' +
            '<div class="mt-1 small text-muted">' +
            '自己角色: ' + d.self_role + ' · 对方角色: ' + d.other_role + '<br>' +
            (d.relationship_summary || '') +
            '</div></div></div>';
    });
    if (html) $('#relationshipInsight').html(html);
}

function renderHabitsInsight(data) {
    var html = '';
    ['self', 'other'].forEach(function(key) {
        var d = data[key];
        if (!d) return;
        html += '<div class="card mb-3">' +
            '<div class="card-header">' + d.name + '</div>' +
            '<div class="card-body">' +
            '<div class="row"><div class="col-md-6">' +
            '<p><strong>性格标签:</strong> ' + (d.personality_tags || []).join('、') + '</p>' +
            '<p><strong>口头禅:</strong> ' + (d.common_phrases || []).join('、') + '</p>' +
            '<p><strong>表情风格:</strong> ' + d.emoji_style + '</p>' +
            '</div><div class="col-md-6">' +
            '<p><strong>句子长度:</strong> ' + d.sentence_length + '</p>' +
            '<p><strong>回复速度:</strong> ' + d.reply_speed + '</p>' +
            '<p><strong>话题跳跃:</strong> ' + d.topic_jumping + '</p>' +
            '</div></div>' +
            '<p><strong>独特习惯:</strong> ' + (d.unique_traits || []).join('、') + '</p>' +
            '</div></div>';
    });
    if (html) $('#habitsInsight').html(html);
}

function renderTopicsCharts(data) {
    var topicMap = {};
    var months = Object.keys(data).sort();

    months.forEach(function(m) {
        var d = data[m];
        if (!d || !d.topics) return;
        d.topics.forEach(function(t) {
            topicMap[t.name] = (topicMap[t.name] || 0) + t.weight;
        });
    });

    var sorted = Object.keys(topicMap)
        .map(function(k) { return {name: k, value: Math.round(topicMap[k] * 100)}; })
        .sort(function(a, b) { return b.value - a.value; });

    renderPieChart('topicsChart', sorted, '话题比重');

    // 逐月详情
    var html = '';
    months.forEach(function(m) {
        var d = data[m];
        if (!d) return;
        var tags = (d.topics || []).map(function(t) {
            return '<span class="badge bg-secondary me-1">' + t.name + ' (' + Math.round(t.weight * 100) + '%)</span>';
        }).join('');
        html += '<div class="card mb-2"><div class="card-body py-2">' +
            '<strong>' + m + '</strong>' +
            '<div class="mt-1">' + tags + '</div>' +
            '<div class="small text-muted mt-1">' + (d.summary || '') + '</div>' +
            '</div></div>';
    });
    if (html) $('#topicsDetails').html(html);
}
