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

// HTML 转义：AI 分析结果 / 聊天内容可能包含 HTML，插入 DOM 前必须转义，防止 XSS
function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------- 主题
// AList 同款设计语言：主色与深浅变化由 CSS 主题变量提供。
// canvas 不认 var()，所以统一在这里读取一次并注入每个图表的 option。
function themeTokens() {
    var cs = getComputedStyle(document.documentElement);
    function v(name, fallback) {
        var x = (cs.getPropertyValue(name) || '').trim();
        return x || fallback;
    }
    function soft(hex, alpha) {              // #1890ff -> rgba(24,144,255,alpha)
        var m = /^#?([0-9a-f]{6})$/i.exec(hex);
        if (!m) return hex;
        var n = parseInt(m[1], 16);
        return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + alpha + ')';
    }
    var primary = v('--primary', '#1890ff');
    var accent2 = v('--chart-2', '#fa8c16');
    return {
        primary: primary,
        accent2: accent2,
        primarySoft: soft(primary, 0.12),
        accent2Soft: soft(accent2, 0.12),
        heat: [v('--chart-heat-low', '#f0f5ff'), v('--chart-heat-mid', '#91d5ff'), v('--chart-heat-high', '#1890ff')],
        // 饼图 / 词云：antd 分类色，围绕主色取邻近色相
        palette: [primary, accent2, '#13c2c2', '#722ed1', '#52c41a', '#eb2f96'],
        wordCloud: ['#1890ff', '#096dd9', '#40a9ff', '#13c2c2', '#5cdbd3', '#69c0ff', '#91d5ff'],
        axis: v('--chart-axis', 'rgba(0,0,0,.45)'),
        split: v('--chart-split', '#f0f0f0'),
        tipBg: v('--chart-tooltip-bg', '#fff'),
        tipBorder: v('--chart-tooltip-border', '#e8e8e8'),
        tipText: v('--chart-tooltip-text', 'rgba(0,0,0,.85)'),
        surface: v('--surface', '#fff'),
        text: v('--text-secondary', 'rgba(0,0,0,.45)')
    };
}
var T = themeTokens();

// 给 option 补默认主题（只填未显式指定的部分，不覆盖各图表自己的设置）
function applyChartTheme(option) {
    var o = $.extend(true, {}, option);
    if (!o.color) o.color = T.palette;

    // 活页图表统一补"下载为图片"：右上角一个图标，点了存当前渲染的 PNG。
    // 此前只有导出报告才会把图冻结成图片，页面上想看某一张图只能截图。
    // 已有 toolbox 的图（少数）不覆盖它的 feature。
    if (!o.toolbox) {
        o.toolbox = {
            feature: {
                saveAsImage: {
                    title: '下载图片',
                    pixelRatio: 2,
                    name: (document.title || 'chart').replace(/[\\/:*?"<>|]/g, '_')
                }
            }
        };
    } else if (o.toolbox.feature && !o.toolbox.feature.saveAsImage) {
        o.toolbox.feature.saveAsImage = { title: '下载图片', pixelRatio: 2 };
    }

    var tip = o.tooltip;
    if (tip) {
        var list = $.isArray(tip) ? tip : [tip];
        list.forEach(function (x) {
            if (!x) return;
            x.backgroundColor = x.backgroundColor || T.tipBg;
            x.borderColor = x.borderColor || T.tipBorder;
            x.borderWidth = x.borderWidth === undefined ? 1 : x.borderWidth;
            x.textStyle = $.extend({ color: T.tipText, fontSize: 12 }, x.textStyle || {});
            x.extraCssText = x.extraCssText || 'box-shadow: 0 2px 8px rgba(0,0,0,.16); border-radius: 6px;';
        });
    }
    ['xAxis', 'yAxis'].forEach(function (key) {
        var axes = o[key];
        if (!axes) return;
        ($.isArray(axes) ? axes : [axes]).forEach(function (ax) {
            if (!ax || ax.type === 'category' && ax.show === false) return;
            ax.axisLabel = $.extend({ color: T.axis }, ax.axisLabel || {});
            ax.nameTextStyle = $.extend({ color: T.axis }, ax.nameTextStyle || {});
            ax.axisLine = $.extend(true, { lineStyle: { color: T.split } }, ax.axisLine || {});
            ax.axisTick = $.extend(true, { lineStyle: { color: T.split } }, ax.axisTick || {});
            if (ax.splitLine !== false) {
                ax.splitLine = $.extend(true, { lineStyle: { color: T.split, type: 'dashed' } }, ax.splitLine || {});
            }
        });
    });
    if (o.legend) {
        ($.isArray(o.legend) ? o.legend : [o.legend]).forEach(function (lg) {
            if (lg) lg.textStyle = $.extend({ color: T.axis }, lg.textStyle || {});
        });
    }
    if (o.title) {
        ($.isArray(o.title) ? o.title : [o.title]).forEach(function (ti) {
            if (ti && ti.textStyle) ti.textStyle.color = ti.textStyle.color || T.text;
            else if (ti) ti.textStyle = { color: T.text };
        });
    }
    // 系列标签（饼图/漏斗等的数值标签）默认是浅色主题的深灰，深色下必须换色
    (o.series || []).forEach(function (s) {
        if (!s) return;
        var labels = $.isArray(s.label) ? s.label : [s.label];
        labels.forEach(function (lb) {
            if (lb && lb.show !== false) lb.color = lb.color || T.tipText;
        });
        if (s.labelLine) {
            s.labelLine = $.extend(true, { lineStyle: { color: T.split } }, s.labelLine);
        }
    });
    if (o.visualMap) {
        ($.isArray(o.visualMap) ? o.visualMap : [o.visualMap]).forEach(function (vm) {
            if (vm) vm.textStyle = $.extend({ color: T.axis }, vm.textStyle || {});
        });
    }
    return o;
}

// 所有图表统一走带主题的 setOption：各渲染函数不必重复主题代码
(function patchECharts() {
    if (!window.echarts || window.__alistThemePatched) return;
    window.__alistThemePatched = true;
    var init = echarts.init;
    echarts.init = function (dom, theme, opts) {
        var chart = init.call(echarts, dom, theme, opts);
        var setOption = chart.setOption.bind(chart);
        chart.setOption = function (option, notMerge, lazy) {
            return setOption(applyChartTheme(option), notMerge, lazy);
        };
        return chart;
    };
})();

// ---------------------------------------------------------------- 图表实例表
// 早先每个渲染函数都自己挂一个 window resize 监听，且从不移除：同一页面重绘 N 次
// 就留下 N 个监听器（每次 resize 触发 N 次）。同一容器上重复 echarts.init 还会
// 留下无法回收的旧实例。现在统一登记：同一容器重绘先 dispose 旧实例，resize 只挂一次。
var _CHARTS = {};

function mountChart(domId) {
    var el = document.getElementById(domId);
    if (!el) return null;
    var old = _CHARTS[domId];
    if (old) {
        try { old.dispose(); } catch (e) { /* 已释放/容器已换，忽略 */ }
        delete _CHARTS[domId];
    }
    var chart = echarts.init(el);
    _CHARTS[domId] = chart;
    return chart;
}

window.addEventListener('resize', function () {
    Object.keys(_CHARTS).forEach(function (key) {
        var chart = _CHARTS[key];
        if (chart && !chart.isDisposed()) chart.resize();
    });
});

function renderPieChart(domId, data, name) {
    const chart = mountChart(domId);
    if (!chart) return;
    const colors = T.palette;
    chart.setOption({
        tooltip: { trigger: 'item', formatter: function(p) { return esc(p.name) + ': ' + p.value + ' (' + p.percent + '%)'; } },
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
}

function renderComparisonChart(domId, overview) {
    var chart = mountChart(domId);
    if (!chart) return;
    var people = [overview.self_name || '我', overview.other_name || '对方'];
    var counts = [Number(overview.self_count) || 0, Number(overview.other_count) || 0];
    var chars = [Number(overview.self_chars) || 0, Number(overview.other_chars) || 0];
    function shares(values) {
        var total = values[0] + values[1];
        return total ? values.map(function (v) { return Math.round(v / total * 1000) / 10; }) : [0, 0];
    }
    var countShares = shares(counts), charShares = shares(chars);
    chart.setOption(applyChartTheme({
        color: [T.primary, T.accent2],
        tooltip: {
            trigger: 'axis', axisPointer: { type: 'shadow' },
            formatter: function (items) {
                var i = items[0].dataIndex;
                var values = i === 0 ? counts : chars;
                return esc(items[0].axisValue) + '<br>' +
                    esc(people[0]) + '：' + Number(values[0]).toLocaleString() + '（' + items[0].value + '%）<br>' +
                    esc(people[1]) + '：' + Number(values[1]).toLocaleString() + '（' + items[1].value + '%）';
            }
        },
        legend: { bottom: 0, data: people },
        grid: { left: 8, right: 22, top: 12, bottom: 35, containLabel: true },
        xAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%' }, splitLine: { show: false } },
        yAxis: { type: 'category', data: ['消息数量', '发言字数'] },
        series: [
            { name: people[0], type: 'bar', stack: 'people', barMaxWidth: 28, label: { show: true, formatter: '{c}%', color: '#fff' }, data: [countShares[0], charShares[0]] },
            { name: people[1], type: 'bar', stack: 'people', barMaxWidth: 28, label: { show: true, formatter: '{c}%', color: '#fff' }, data: [countShares[1], charShares[1]] }
        ]
    }));
}

// 日线聚合：消息只出现在少数日子时，类目轴会把空档压平（首末相隔 100 天可能只画 3 个点）。
// 服务端已补齐空档为 0；这里再按跨度自动聚合，避免 3 年 1000+ 个点挤成一团。
function aggregateDaily(data, maxPoints) {
    maxPoints = maxPoints || 200;
    if (!data || !data.length) return { points: [], note: '' };
    if (data.length <= maxPoints) {
        return { points: data.map(function (d) {
            return { label: d.date, self: d.self, other: d.other };
        }), note: '' };
    }
    var spanDays = data.length;
    var mode = spanDays > 1100 ? 'month' : 'week';
    var buckets = {}, order = [];
    function weekStart(iso) {                       // 该日期所在周的周一（UTC 计算，避免时区漂移）
        var d = new Date(iso + 'T00:00:00Z');
        var day = (d.getUTCDay() + 6) % 7;
        d.setUTCDate(d.getUTCDate() - day);
        return d.toISOString().slice(0, 10);
    }
    data.forEach(function (d) {
        var key = mode === 'month' ? d.date.slice(0, 7) : weekStart(d.date);
        if (!buckets[key]) { buckets[key] = { label: key, self: 0, other: 0 }; order.push(key); }
        buckets[key].self += d.self;
        buckets[key].other += d.other;
    });
    return {
        points: order.map(function (k) { return buckets[k]; }),
        note: mode === 'month' ? '按月聚合' : '按周聚合'
    };
}

function renderLineChart(domId, data, yName) {
    const chart = mountChart(domId);
    if (!chart) return;
    var view = aggregateDaily(data);
    var titleEl = document.getElementById(domId + 'Title');
    if (titleEl && view.note) titleEl.textContent = '消息量（' + view.note + '）';
    chart.setOption({
        title: view.note ? { text: view.note + '，共 ' + view.points.length + ' 个点',
                             left: 'center', top: 0,
                             textStyle: { fontSize: 11, fontWeight: 'normal', color: T.axis } } : undefined,
        tooltip: { trigger: 'axis' },
        legend: { data: ['对方', '自己'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '15%', top: view.note ? 28 : 10, containLabel: true },
        xAxis: {
            type: 'category',
            data: view.points.map(function(p) { return p.label; }),
            axisLabel: { rotate: 45, fontSize: 10 }
        },
        yAxis: { type: 'value', name: yName },
        dataZoom: [{ type: 'inside', start: 0, end: 100 }],
        series: [
            {
                name: '对方', type: 'line',
                data: view.points.map(function(p) { return p.other; }),
                smooth: true,
                lineStyle: { color: T.accent2, width: 2 },
                itemStyle: { color: T.accent2 },
                areaStyle: { color: T.accent2Soft }
            },
            {
                name: '自己', type: 'line',
                data: view.points.map(function(p) { return p.self; }),
                smooth: true,
                lineStyle: { color: T.primary, width: 2 },
                itemStyle: { color: T.primary },
                areaStyle: { color: T.primarySoft }
            }
        ]
    });
}

function renderBarChart(domId, data, yName) {
    const chart = mountChart(domId);
    if (!chart) return;
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
                itemStyle: { color: T.accent2, borderRadius: [3,3,0,0] }
            },
            {
                name: '自己', type: 'bar',
                data: data.map(function(d) { return d.self; }),
                itemStyle: { color: T.primary, borderRadius: [3,3,0,0] }
            }
        ]
    });
}

// 月度趋势：柱=当月双方条数（堆叠看总量、对比看谁主动），线=当月平均句长（右轴）。
// 数据来自本地统计 trends.months，服务端已按月排好序。
function renderMonthlyTrendChart(domId, months, selfName, otherName) {
    const chart = mountChart(domId);
    if (!chart || !months || !months.length) return;
    var names = [selfName + '条数', otherName + '条数', selfName + '平均句长', otherName + '平均句长'];
    chart.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'cross', crossStyle: { color: T.axis } } },
        legend: { data: names, bottom: 0, textStyle: { fontSize: 11 } },
        grid: { left: '3%', right: '4%', bottom: '16%', top: 30, containLabel: true },
        xAxis: { type: 'category', data: months.map(function (m) { return m.month; }),
                 axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: [
            { type: 'value', name: '消息条数' },
            { type: 'value', name: '平均句长(字)', splitLine: { show: false } }
        ],
        series: [
            {
                name: selfName + '条数', type: 'bar', stack: 'total',
                data: months.map(function (m) { return m.self; }),
                itemStyle: { color: T.primary }
            },
            {
                name: otherName + '条数', type: 'bar', stack: 'total',
                data: months.map(function (m) { return m.other; }),
                itemStyle: { color: T.accent2 }
            },
            {
                name: selfName + '平均句长', type: 'line', yAxisIndex: 1, smooth: true,
                data: months.map(function (m) { return m.avg_len_self; }),
                lineStyle: { color: T.primary, type: 'dashed' }, itemStyle: { color: T.primary }
            },
            {
                name: otherName + '平均句长', type: 'line', yAxisIndex: 1, smooth: true,
                data: months.map(function (m) { return m.avg_len_other; }),
                lineStyle: { color: T.accent2, type: 'dashed' }, itemStyle: { color: T.accent2 }
            }
        ]
    });
}

function renderWeeklyChart(domId, data) {
    const chart = mountChart(domId);
    if (!chart) return;
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
                itemStyle: { color: T.accent2, borderRadius: [3,3,0,0] }
            },
            {
                name: '自己', type: 'bar',
                data: data.map(function(d) { return d.self; }),
                itemStyle: { color: T.primary, borderRadius: [3,3,0,0] }
            }
        ]
    });
}

function renderResponseChart(domId, data) {
    const chart = mountChart(domId);
    if (!chart) return;
    chart.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: '10%', right: '10%', containLabel: true },
        xAxis: { type: 'category', data: [data.selfName, data.otherName] },
        yAxis: { type: 'value', name: '秒' },
        series: [{
            type: 'bar',
            data: [
                { value: data.self, itemStyle: { color: T.primary } },
                { value: data.other, itemStyle: { color: T.accent2 } }
            ],
            barWidth: '40%',
            label: { show: true, formatter: '{c}s', position: 'top' }
        }]
    });
}

function renderWordCloud(domId, data, title) {
    const el = document.getElementById(domId);
    if (!el) return;
    if (!data || !data.length) {
        el.innerHTML = '<div class="text-muted text-center py-4">暂无数据</div>';
        return;
    }
    const chart = mountChart(domId);
    if (!chart) return;
    var maxCount = data[0].count;
    var minCount = data[data.length - 1].count || 1;

    var colors = T.wordCloud;

    chart.setOption({
        // 标题渲染在 canvas 上（非 DOM），不需要 esc——转义反而会显示字面实体
        title: { text: title, left: 'center', textStyle: { fontSize: 14 } },
        tooltip: { formatter: function(p) { return esc(p.name) + ': ' + p.value + '次'; } },
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
            data: data.map(function(d, i) {
                var fontSize = 14 + 34 * (d.count - minCount) / (maxCount - minCount || 1);
                return {
                    name: d.word,
                    value: d.count,
                    textStyle: {
                        fontSize: fontSize,
                        // 按出现顺序固定配色，刷新时颜色稳定
                        color: colors[i % colors.length]
                    }
                };
            })
        }]
    });
}

function renderFaceBarChart(domId, data, personName, emojiMap, imageMap) {
    const el = document.getElementById(domId);
    if (!el) return;
    // 兼容两种入参：[[名字, 次数], ...]（保序，模板用 items()|list 传来）
    // 或 {名字: 次数}（旧格式）。顺序即排行，绝不能按名字重排。
    let entries = Array.isArray(data) ? data.map(function (p) { return [p[0], p[1]]; })
                                      : Object.entries(data || {});
    entries = entries.slice(0, 10);
    if (!entries.length) {
        el.innerHTML = '<div class="text-muted text-center py-4">表情数据不足</div>';
        return;
    }
    emojiMap = emojiMap || {};
    imageMap = imageMap || {};
    // 三级回退：有原始表情图就画图（ECharts rich text 的 backgroundImage），
    // 否则用 Unicode emoji，再否则显示表情名——QQ 专属表情抓不到图时不硬凑。
    const rich = {};
    const labels = entries.map(function (e, i) {
        const name = e[0];
        if (imageMap[name]) {
            const style = 'face' + i;
            rich[style] = { backgroundColor: { image: imageMap[name] },
                            width: 20, height: 20, align: 'center' };
            return '{' + style + '|}';
        }
        return emojiMap[name] ? emojiMap[name] : name.replace(/^\//, '');
    });
    const chart = mountChart(domId);
    if (!chart) return;
    chart.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function (p) {
                const name = entries[p[0].dataIndex][0];
                const em = emojiMap[name];
                return esc(em ? em + ' ' + name.replace(/^\//, '') : name) + ': ' + p[0].value + '次';
            }
        },
        grid: { left: '5%', right: '10%', containLabel: true },
        xAxis: { type: 'value', name: '次数' },
        yAxis: {
            type: 'category',
            // 排行要"第一名在最上面"：ECharts 类目轴默认自下而上，这里翻转
            inverse: true,
            data: labels,
            axisTick: { alignWithLabel: true },
            axisLabel: {
                fontSize: 18,
                // 关键：emoji 字形（彩色 emoji 字体）与中文字形的度量不同，不指定 lineHeight
                // 时轴标签会与柱条错位半个行高；固定行高让两者对齐。
                lineHeight: 18,
                verticalAlign: 'middle',
                margin: 10,
                rich: rich,
                fontFamily: '"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",'
                            + '"Microsoft YaHei",sans-serif'
            }
        },
        series: [{
            type: 'bar',
            data: entries.map(function (e) { return e[1]; }),
            itemStyle: { color: T.primary, borderRadius: [0,3,3,0] },
            label: { show: true, position: 'right', fontWeight: 'bold' }
        }]
    });
}

function renderHeatmapChart(domId, data) {
    const chart = mountChart(domId);
    if (!chart) return;
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
        grid: { left: 8, right: 16, top: 10, bottom: 56, containLabel: true },
        xAxis: {
            type: 'category',
            data: Array.from({length:24}, function(_,i) { return i; }),
            splitArea: { show: false },
            axisLabel: { interval: 1, fontSize: 10, color: T.axis },
            axisTick: { show: false },
            axisLine: { lineStyle: { color: T.split } }
        },
        yAxis: {
            type: 'category',
            data: weekdays,
            splitArea: { show: false },
            axisLabel: { fontSize: 11, color: T.axis },
            axisTick: { show: false },
            axisLine: { lineStyle: { color: T.split } }
        },
        visualMap: {
            min: 0, max: maxVal, calculable: true, orient: 'horizontal',
            left: 'center', bottom: 0, itemWidth: 12, itemHeight: 90,
            inRange: { color: T.heat },
            textStyle: { color: T.axis, fontSize: 11 }
        },
        series: [{
            type: 'heatmap',
            data: heatData,
            label: { show: false },
            // 格子描边用主题色：写死 #ffffff 会让深色主题下出现一圈白线
            itemStyle: { borderColor: T.surface, borderWidth: 2, borderRadius: 2 },
            emphasis: { itemStyle: { borderColor: T.axis, borderWidth: 1 } }
        }]
    });
}

// ========== AI 分析图表 ==========

function renderEmotionCharts(data) {
    if (!data) return;
    const months = Object.keys(data).sort();
    // 模型偶尔会漏字段：取值统一兜底，别让一个 undefined 把整块图表打断
    var val = function (m, key, fallback) {
        var d = data[m] || {};
        return d[key] === undefined || d[key] === null ? fallback : d[key];
    };
    var selfIntensity = months.map(function(m) { return val(m, 'self_intensity', 0); });
    var otherIntensity = months.map(function(m) { return val(m, 'other_intensity', 0); });
    var selfEmotions = months.map(function(m) { return val(m, 'self_emotion', '数据不足'); });
    var otherEmotions = months.map(function(m) { return val(m, 'other_emotion', '数据不足'); });

    // 情绪强度折线图
    var lineChart = mountChart('emotionLineChart');
    if (!lineChart) return;
    lineChart.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                var idx = params[0].dataIndex;
                var m = months[idx];
                // 情绪标签来自模型输出（受聊天内容影响），tooltip 按 HTML 渲染，必须转义
                var html = '<strong>' + esc(m) + '</strong><br>';
                params.forEach(function(p) {
                    html += p.marker + ' ' + esc(p.seriesName) + ': ' + p.value + '<br>';
                });
                html += '自己: ' + esc(selfEmotions[idx]) + '<br>';
                html += '对方: ' + esc(otherEmotions[idx]);
                return html;
            }
        },
        legend: { data: ['自己情绪强度', '对方情绪强度'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '15%', containLabel: true },
        xAxis: { type: 'category', data: months },
        yAxis: { type: 'value', name: '情绪强度', min: 0, max: 10 },
        series: [
            {
                name: '自己情绪强度', type: 'line',
                data: selfIntensity, smooth: true,
                lineStyle: { color: T.primary, width: 2 },
                itemStyle: { color: T.primary },
                areaStyle: { color: T.primarySoft }
            },
            {
                name: '对方情绪强度', type: 'line',
                data: otherIntensity, smooth: true,
                lineStyle: { color: T.accent2, width: 2 },
                itemStyle: { color: T.accent2 },
                areaStyle: { color: T.accent2Soft }
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
            '<strong>' + esc(m) + '</strong>' +
            '<span class="badge bg-primary ms-2">自己: ' + esc(d.self_emotion) + '(' + esc(d.self_intensity) + ')</span>' +
            '<span class="badge bg-success ms-1">对方: ' + esc(d.other_emotion) + '(' + esc(d.other_intensity) + ')</span>' +
            '<span class="badge bg-info ms-1">基调: ' + esc(d.overall_tone) + '</span>' +
            '<div class="mt-1 small text-muted">' +
            '自己关键词: ' + (d.self_keywords || []).map(esc).join('、') + '<br>' +
            '对方关键词: ' + (d.other_keywords || []).map(esc).join('、') +
            '</div>' +
            (d.month_vibe ? '<div class="mt-1 small">' + esc(d.month_vibe) + '</div>' : '') +
            (d.turning_point ? '<div class="mt-1 small text-warning">' + esc(d.turning_point) + '</div>' : '') +
            '</div></div></div>';
    });
    $('#emotionDetails').html(html);
}

function renderRelationshipInsight(data) {
    if (!data) return;
    var months = Object.keys(data).sort();
    var html = '';
    months.forEach(function(m) {
        var d = data[m];
        if (!d) return;
        html += '<div class="card mb-2"><div class="card-body py-2">' +
            '<strong>' + esc(m) + '</strong>' +
            '<span class="badge bg-info ms-2">亲密: ' + esc(d.closeness_score) + '/10</span>' +
            '<span class="badge bg-secondary ms-1">趋势: ' + esc(d.closeness_trend) + '</span>' +
            '<span class="badge bg-warning ms-1">风格: ' + esc(d.interaction_style) + '</span>' +
            '<div class="mt-1 small text-muted">' +
            '自己角色: ' + esc(d.self_role) + ' · 对方角色: ' + esc(d.other_role) + '<br>' +
            esc(d.relationship_summary) +
            '</div></div></div>';
    });
    if (html) $('#relationshipInsight').html(html);
}

function renderHabitsInsight(data) {
    if (!data) return;
    var html = '';
    ['self', 'other'].forEach(function(key) {
        var d = data[key];
        if (!d) return;
        html += '<div class="card mb-3">' +
            '<div class="card-header">' + esc(d.name) + '</div>' +
            '<div class="card-body">' +
            '<div class="row"><div class="col-md-6">' +
            '<p><strong>性格标签:</strong> ' + (d.personality_tags || []).map(esc).join('、') + '</p>' +
            '<p><strong>口头禅:</strong> ' + (d.common_phrases || []).map(esc).join('、') + '</p>' +
            '<p><strong>表情风格:</strong> ' + esc(d.emoji_style) + '</p>' +
            '</div><div class="col-md-6">' +
            '<p><strong>句子长度:</strong> ' + esc(d.sentence_length) + '</p>' +
            '<p><strong>回复速度:</strong> ' + esc(d.reply_speed) + '</p>' +
            '<p><strong>话题跳跃:</strong> ' + esc(d.topic_jumping) + '</p>' +
            '</div></div>' +
            '<p><strong>独特习惯:</strong> ' + (d.unique_traits || []).map(esc).join('、') + '</p>' +
            '</div></div>';
    });
    if (html) $('#habitsInsight').html(html);
}

function renderTopicsCharts(data) {
    if (!data) return;
    var topicMap = {};
    var months = Object.keys(data).sort();

    months.forEach(function(m) {
        var d = data[m];
        if (!d || !Array.isArray(d.topics)) return;
        d.topics.forEach(function(t) {
            var w = Number(t && t.weight);
            topicMap[t.name] = (topicMap[t.name] || 0) + (isFinite(w) ? w : 0);
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
            return '<span class="badge bg-secondary me-1">' + esc(t.name) + ' (' + Math.round(t.weight * 100) + '%)</span>';
        }).join('');
        html += '<div class="card mb-2"><div class="card-body py-2">' +
            '<strong>' + esc(m) + '</strong>' +
            (d.month_title ? ' <span class="text-muted small">' + esc(d.month_title) + '</span>' : '') +
            '<div class="mt-1">' + tags + '</div>' +
            '<div class="small text-muted mt-1">' + esc(d.summary) + '</div>' +
            '</div></div>';
    });
    if (html) $('#topicsDetails').html(html);
}
