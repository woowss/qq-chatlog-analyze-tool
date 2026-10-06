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

// 互动热力矩阵：X（列）= 接话/点名的人，Y（行）= 先说/被@的人；对角线留空（自己不接自己的话）
// （读法与 tooltip、卡片标题、服务端矩阵定义四方一致；本行注释曾把 X/Y 写反，
//   而那个 bug 恰恰就是照着这句错注释养成的——留错注释比留 bug 更长寿）
function renderInteractionHeatmap(domId, interaction, opts) {
    var chart = mountChart(domId);
    if (!chart) return;
    // 同一张热力图被两种矩阵复用，两处的**读法必须不同**，所以模式要显式传：
    //   接话/回复矩阵（group_stats.calc_interaction_matrix）：directed[i][j] 的
    //     i = 先说的人（行）、j = 接话的人（列）；
    //   @点名矩阵：mention[i][j] 的 i = 被@的人（行）、j = 点名的人（列）。
    // 数据点压成 [列, 行, 值] 交给 ECharts，于是 x=列、y=行。
    var mention = !!(opts && opts.mode === 'mention');
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
                // p.value[0] = 列 = 动作的**发出方**，p.value[1] = 行 = 动作的**对象**。
                // 这里曾经写反（先说/接话两个角色互换），而卡片标题、坐标轴与数据都是
                // 对的 —— 于是鼠标停在"A 先说、B 接了 4 次"的格子上，气泡却说
                // "B 说完，A 接了 4 次"：一处与其余三处相反，用户信的是气泡。
                var row = esc(names[p.value[1]]);  // 行 = 先说的人 / 被@的人
                var col = esc(names[p.value[0]]);  // 列 = 接话的人 / 点名的人
                if (p.value[2] === null) {
                    return row + (mention ? '（自己不@自己）' : '（自己不接自己的话）');
                }
                return mention
                    ? col + ' @了 ' + row + ' ' + p.value[2] + ' 次'
                    : row + ' 说完，' + col + ' 接了 ' + p.value[2] + ' 次';
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

// ---------------------------------------------------------------- 互动关系图
//
// 这张图真正难的不是"画出来"，而是"画出来能看"。30 位成员的群，相邻接话边能到
// 270 条（接近完全图），按老画法得到的是画布正中一团灰网 + 一堆叠在一起的昵称，
// 而四周大片空白——信息量为零。所以下面每个默认值都只为可读性服务：
//
//   1. 事实与推断分层：精确回复/@ 走实线并向上弯，接话走虚线并向下弯。老画法两层
//      都是直线，同一对成员的两条边完全重叠，看起来只有一条；
//   2. 推断边默认只留最强的几十条——弱边满图都是，等于没有信息。图例旁如实报出
//      "画了多少 / 一共多少"，不许悄悄丢数据；
//   3. 布局钉死：力导向只负责算坐标（force.layoutAnimation=false 让它一次算到收敛），
//      算完换成 layout:'none' 把坐标写死。老画法整个交给力导向，节点能撑到画布外
//      （实测老画法能把节点撑到画布外，而画布只有几百像素高），两端节点与昵称被裁掉，左右却空着；
//      钉死之后位置完全可控，也顺带解决了"每次 setOption 力导向都重排一次"的抖动；
//   4. labelLayout.hideOverlap：ECharts 5 自带的标签防重叠，重叠的昵称自动不画；
//   5. 昵称按发言量发"常驻名额"（前 N 位），其余悬停才显示——标签总量才是乱的根源；
//   6. 老图例是错的：三个 category 没有任何节点真的挂上去，色块也与线型对不上。
//      改成 HTML 图例（见 relLegendHtml），线型、粗细、颜色都按真实画法呈现。

// 推断边的三档强度。用"最多画几条"而不是"次数 ≥ k"：不同群的消息密度能差一个
// 数量级，固定次数门槛在小群里把边全滤光、在大群里一条都滤不掉。
var REL_LEVELS = [
    { name: '强', maxEdges: 45, minValue: 2 },
    { name: '中', maxEdges: 90, minValue: 2 },
    { name: '全', maxEdges: 0, minValue: 1 }        // 0 = 不限
];

var REL_DEFAULTS = {
    controls: true,      // 报告导出页传 false：导出的 HTML 会摘掉所有 button，留着只有残骸
    exact: true,         // 事实层：精确回复 / @点名（实线）
    infer: true,         // 推断层：相邻接话（虚线）
    level: 1,            // REL_LEVELS 下标
    memberLimit: 0,      // 0 = 全部进图的成员
    maxExactEdges: 120,  // 事实边同样封顶：极端群里回复也能到几百条
    maxLabels: 18,       // 常驻昵称名额（按发言量取前 N）
    labelMaxChars: 11    // 昵称截断长度（从中间截，见 label.formatter）
};

//: 成员数量档位（0 = 不限）。比成员总数还大的档位会自动跳过：5 人群不该出现"前 12"。
var REL_MEMBER_LIMITS = [0, 20, 12];

//: 布局拉伸上限（见 relStretchLayout）。力导向各向同性，宽卡片上要铺满得横向拉很多；
//: 拉到 2.6 倍还在"看得出是个关系网"的范围里，再大就明显像被擀面杖擀过。
var REL_STRETCH_MAX = 2.6;

//: 每个画布的重绘状态。工具条改的是它，不必重新回模板取数。
var _REL_STATE = {};

function relClampIndex(i, n) {
    i = Number(i) || 0;
    return Math.min(Math.max(i, 0), Math.max(0, n - 1));
}

function relNum(v) {
    return Number(v) || 0;
}

function relOpts(opts) {
    var o = {}, k;
    for (k in REL_DEFAULTS) {
        if (Object.prototype.hasOwnProperty.call(REL_DEFAULTS, k)) o[k] = REL_DEFAULTS[k];
    }
    opts = opts || {};
    for (k in opts) {
        if (Object.prototype.hasOwnProperty.call(opts, k) && opts[k] !== undefined) o[k] = opts[k];
    }
    return o;
}

// 矩阵取值：矩阵是二维数组，缺行/缺列都按 0 算（asList 兜住"某行不是数组"的脏数据）
function relAt(matrix, i, j) {
    return relNum(asList(matrix[i])[j]);
}

// 把服务端三个矩阵重组成"每对成员一份"的记录。
// 服务端按信号分了三个矩阵，前端却必须按"一对人"聚合：同一对成员各画一条线会互相
// 压住，看不出谁跟谁到底多铁。方向语义照服务端约定——X[i][j] = j 对 i 的动作。
//
// 两个退路都为了"别悄悄少画一层"：推断层缺 undirected 时用 edges 边表重建；
// 只有无向的回复矩阵时，边照画但不画箭头（不知道方向就别瞎指）。
function relPairs(interaction, opts) {
    interaction = interaction || {};
    var members = asList(interaction.members);
    if (opts.memberLimit > 0) members = members.slice(0, opts.memberLimit);
    var n = members.length;

    var inferM = asList(interaction.undirected);
    if (!inferM.length) {
        asList(interaction.edges).forEach(function (e) {
            var i = Number(e && e.source), j = Number(e && e.target), v = relNum(e && e.value);
            if (!(i >= 0) || !(j >= 0) || !v) return;
            if (!inferM[i]) inferM[i] = [];
            if (!inferM[j]) inferM[j] = [];
            inferM[i][j] = inferM[j][i] = v;
        });
    }
    var replyDir = asList(interaction.explicit_directed);
    var replyFlat = asList(interaction.explicit_undirected);
    var mentionM = asList(interaction.mention_directed);

    var pairs = [];
    for (var i = 0; i < n; i++) {
        for (var j = i + 1; j < n; j++) {
            var p = {
                source: members[i].uid, target: members[j].uid,
                infer: relAt(inferM, i, j),                      // 无向合计（服务端已算好）
                replyIJ: relAt(replyDir, j, i), replyJI: relAt(replyDir, i, j),
                mentionIJ: relAt(mentionM, j, i), mentionJI: relAt(mentionM, i, j)
            };
            p.reply = p.replyIJ + p.replyJI || relAt(replyFlat, i, j);
            p.mention = p.mentionIJ + p.mentionJI;
            p.exact = p.reply + p.mention;
            // 精确信号的方向：fwd = i→j，back = j→i。只有无向矩阵时两者都是 0，
            // 于是下面不会画箭头 —— 这正是"不知道方向就别指"想要的效果。
            p.fwd = p.replyIJ + p.mentionIJ;
            p.back = p.replyJI + p.mentionJI;
            if (p.exact || p.infer) pairs.push(p);
        }
    }
    return { members: members, pairs: pairs };
}

// 边的悬停明细：两层分开列，且只列非零项——罗列一堆 0 反而看不清重点
function relEdgeTip(nameOf, p) {
    var a = nameOf[p.source] || '', b = nameOf[p.target] || '';
    var rows = [];
    function dir(label, ij, ji) {
        var parts = [];
        if (ij) parts.push(esc(a) + ' → ' + esc(b) + ' ' + ij);
        if (ji) parts.push(esc(b) + ' → ' + esc(a) + ' ' + ji);
        if (parts.length) rows.push(label + '：' + parts.join(' · '));
    }
    dir('精确回复', p.replyIJ, p.replyJI);
    dir('@点名', p.mentionIJ, p.mentionJI);
    if (p.infer) rows.push('相邻接话 ' + p.infer + ' 次（推断）');
    return '<b>' + esc(a) + ' ↔ ' + esc(b) + '</b>' + (rows.length ? '<br/>' + rows.join('<br/>') : '');
}

// 节点的悬停明细：口径与页面下方"谁最常和谁互动"表一致，两处读数必须对得上
function relNodeTip(m, vol, act, t) {
    var rows = ['<b>' + esc(m.name) + '</b>' + (m.is_self ? '（我）' : '')];
    rows.push('发言 ' + vol + ' 条' + (act && act.share ? ' · 占 ' + pct(act.share) : ''));
    rows.push('精确回复 别人 ' + relNum(t.explicit_replies_to) + ' · 被回复 ' + relNum(t.explicit_replied_by));
    rows.push('@别人 ' + relNum(t.mentions_sent) + ' · 被@ ' + relNum(t.mentions_received));
    rows.push('接话 ' + relNum(t.replies_to) + ' · 被接话 ' + relNum(t.replied_by) + '（推断）');
    return rows.join('<br/>');
}

// HTML 图例：线样、粗细、颜色都按 canvas 里的真实画法画出来。
// ECharts 的 legend 只能画色块，表达不了"实线 vs 虚线"，而这张图的全部信息都在
// 线型与粗细里——所以这里必须用 DOM，不能用 legend。
function relLegendHtml() {
    function key(cls, label, style) {
        return '<span class="rel-key"><i class="' + cls + '" style="' + style + '"></i>' + label + '</span>';
    }
    return key('rel-swatch', '精确回复（事实）', 'border-color:' + T.primary) +
        key('rel-swatch', '@点名（事实）', 'border-color:' + T.palette[2]) +
        key('rel-swatch rel-swatch-dash', '相邻接话（推断）', 'border-color:' + T.axis) +
        key('rel-dot', '成员（大小 = 发言量）', 'background:' + T.primary) +
        key('rel-dot', '我', 'background:' + T.accent2) +
        '<span class="rel-key rel-hint">可拖拽节点 · 滚轮缩放 · 悬停看明细</span>';
}

// 组装力导向图的数据：节点（成员）+ 两层边（事实/推断）+ 图例旁的对账文字
function relSeriesData(interaction, activity, opts) {
    var built = relPairs(interaction, opts);
    var members = built.members;
    var nameOf = {}, totals = {}, acts = {}, vols = {}, maxVol = 1;
    members.forEach(function (m) { nameOf[m.uid] = m.name; });
    asList(interaction && interaction.totals).forEach(function (t) { totals[t.uid] = t; });
    asList(activity).forEach(function (a) { acts[a.uid] = a; });
    members.forEach(function (m) {
        vols[m.uid] = relNum(acts[m.uid] && acts[m.uid].msg_count);
        maxVol = Math.max(maxVol, vols[m.uid]);
    });

    var level = REL_LEVELS[relClampIndex(opts.level, REL_LEVELS.length)];
    function byValue(a, b) { return b.v - a.v; }

    // —— 事实层（实线）：精确回复 + @点名 ——
    var exactAll = built.pairs.filter(function (p) { return p.exact > 0; })
        .map(function (p) { return { p: p, v: p.exact }; }).sort(byValue);
    var exact = opts.exact ? exactAll.slice(0, Math.max(1, opts.maxExactEdges)) : [];

    // —— 推断层（虚线）：相邻接话 ——
    var inferAll = built.pairs.filter(function (p) { return p.infer > 0; })
        .map(function (p) { return { p: p, v: p.infer }; }).sort(byValue);
    var infer = opts.infer ? inferAll.filter(function (r) { return r.v >= level.minValue; }) : [];
    if (opts.infer && level.maxEdges > 0) infer = infer.slice(0, level.maxEdges);

    var maxExact = exact.length ? exact[0].v : 1;
    var maxInfer = infer.length ? infer[0].v : 1;
    var edges = [];
    var linked = {};

    exact.forEach(function (r) {
        var p = r.p;
        var t = Math.sqrt(r.v / maxExact);           // 开方压缩量级差，否则细边根本看不见
        var onlyMention = !p.reply && p.mention > 0;
        var e = {
            source: p.source, target: p.target,
            lineStyle: {
                color: onlyMention ? T.palette[2] : T.primary,
                width: 1 + 4 * t,
                opacity: 0.5 + 0.4 * t,
                curveness: 0.16,                     // 与推断层反向弯，两层不再叠成一条
                type: 'solid'
            },
            tip: relEdgeTip(nameOf, p)
        };
        // 方向只在"明显一边倒"时画箭头：双向对等的互动画箭头等于撒谎
        if (p.fwd >= 3 * p.back && p.fwd > 0) e.symbol = ['none', 'arrow'];
        else if (p.back >= 3 * p.fwd && p.back > 0) e.symbol = ['arrow', 'none'];
        edges.push(e);
        linked[p.source] = linked[p.target] = true;
    });

    infer.forEach(function (r) {
        var p = r.p;
        var t = Math.sqrt(r.v / maxInfer);
        edges.push({
            source: p.source, target: p.target,
            lineStyle: {
                color: T.axis, width: 0.8 + 2.4 * t, opacity: 0.16 + 0.34 * t,
                curveness: -0.16, type: 'dashed'
            },
            tip: relEdgeTip(nameOf, p)
        });
        linked[p.source] = linked[p.target] = true;
    });

    // —— 节点 ——
    // 常驻昵称按发言量发名额：糊成一团的根源是标签总量，不是字号不够小
    var labelled = {};
    members.slice().sort(function (a, b) { return vols[b.uid] - vols[a.uid]; })
        .slice(0, Math.max(0, opts.maxLabels))
        .forEach(function (m) { labelled[m.uid] = true; });

    var nodes = members.map(function (m) {
        var vol = vols[m.uid];
        var size = 13 + 38 * Math.sqrt(vol / maxVol);
        return {
            id: m.uid, name: m.name, value: vol,
            symbolSize: size,
            itemStyle: {
                color: m.is_self ? T.accent2 : T.primary,
                borderColor: T.surface, borderWidth: 2,
                shadowBlur: 8, shadowColor: 'rgba(0,0,0,.18)',
                // 被门槛滤光所有边的节点压暗：它还在矩阵里，但不该抢注意力
                opacity: linked[m.uid] ? 1 : 0.35
            },
            label: { show: !!labelled[m.uid] },
            tip: relNodeTip(m, vol, acts[m.uid], totals[m.uid] || {})
        };
    });

    var n = Math.max(1, nodes.length);
    var option = applyChartTheme({
        animation: false,
        tooltip: {
            trigger: 'item', confine: true,
            formatter: function (p) { return (p.data && p.data.tip) || ''; }
        },
        // 关掉 ECharts 图例：它只能画色块，画不出实线/虚线/粗细（详见 relLegendHtml）
        legend: { show: false },
        series: [{
            type: 'graph', layout: 'force', roam: true, draggable: true,
            force: {
                initLayout: 'circular',                    // 先落在圆周上，再让力导向收拢
                repulsion: Math.max(420, 30 * n),          // 人越多越要撑开，否则挤成一小块
                edgeLength: [55, 150],
                gravity: 0.05,
                friction: 0.55,
                layoutAnimation: false                     // 一次算到收敛再上屏（见文件头注释 3）
            },
            label: {
                position: 'right', color: T.text, fontSize: 11,
                // 昵称压在线和点上就读不出来了：垫一圈卡片底色的描边当底衬
                textBorderColor: T.surface, textBorderWidth: 3,
                // 群里 15+ 字的昵称很常见，整串画出来等于给邻居糊上一条色带。
                // 从中间截断而不是砍尾巴：群里大量昵称是"部门 编号 姓名"，
                // 真正能把人区分开的是尾巴（编辑部 25-13 张罩 / 编辑部 25-6 张乐水）。
                formatter: function (p) {
                    var nm = String((p.data && p.data.name) || '');
                    var max = Math.max(4, opts.labelMaxChars);
                    if (nm.length <= max) return nm;
                    var head = Math.ceil((max - 1) / 2), tail = max - 1 - head;
                    return nm.slice(0, head) + '…' + (tail > 0 ? nm.slice(-tail) : '');
                }
            },
            labelLayout: { hideOverlap: true },
            edgeSymbolSize: 6,
            emphasis: { focus: 'adjacency', scale: 1.12, label: { show: true, fontWeight: 'bold' } },
            blur: { itemStyle: { opacity: 0.12 }, lineStyle: { opacity: 0.05 }, label: { opacity: 0.2 } },
            data: nodes,
            edges: edges
        }]
    });

    var note = '已画 ' + (opts.exact ? '事实 ' + exact.length + '/' + exactAll.length + ' 条' : '事实已隐藏');
    note += opts.infer ? ' · 推断 ' + infer.length + '/' + inferAll.length + ' 条' : ' · 推断已隐藏';
    if (opts.memberLimit > 0) note += ' · 成员前 ' + members.length + ' 位';

    return { nodes: nodes, edges: edges, option: option, note: note };
}

// 可用的成员档位：比成员总数还大的档位没有意义（5 人群不该出现"前 12"）
function relMemberLimits(st) {
    var n = asList(st.interaction && st.interaction.members).length;
    var out = [0];
    REL_MEMBER_LIMITS.forEach(function (k) { if (k > 0 && k < n) out.push(k); });
    return out;
}

// 给画布套一层外壳（工具条 + 图例）。做成"包一层"而不是改四个模板：群概况/话题/
// 关系/报告共用同一个渲染函数，工具条在模板里各写一份迟早会漂移。
function relShell(domId, st) {
    var el = document.getElementById(domId);
    if (!el || !el.parentNode) return;
    var box = document.createElement('div');
    box.className = 'rel-shell';
    el.parentNode.insertBefore(box, el);
    box.appendChild(el);

    if (st.opts.controls !== false) {
        var bar = document.createElement('div');
        bar.className = 'rel-toolbar no-print';
        bar.id = domId + 'Toolbar';
        box.insertBefore(bar, el);
    }
    var lg = document.createElement('div');
    lg.className = 'rel-legend';
    lg.innerHTML = relLegendHtml() + '<span class="rel-count" id="' + domId + 'Note"></span>';
    box.appendChild(lg);
}

// 工具条：两个开关（事实 / 推断）+ 两个档位（推断强度 / 成员数量）。
// 全部用 button：报告导出会摘掉所有 button，所以报告页干脆传 controls:false 不生成。
function relToolbar(domId, st) {
    var bar = document.getElementById(domId + 'Toolbar');
    if (!bar) return;
    var o = st.opts;
    bar.innerHTML = '';

    function add(label, title, pressed, disabled, onClick) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'rel-btn';
        b.textContent = label;
        if (title) b.title = title;
        b.setAttribute('aria-pressed', pressed ? 'true' : 'false');
        if (disabled) b.disabled = true;
        else b.onclick = onClick;
        bar.appendChild(b);
    }

    add('精确回复/@', '导出器记录的回复与 @点名（事实），实线', o.exact, false, function () {
        o.exact = !o.exact;
        relRefresh(domId, st);
    });
    add('相邻接话', '30 分钟内的相邻换人发言（推断），虚线', o.infer, false, function () {
        o.infer = !o.infer;
        relRefresh(domId, st);
    });

    var lv = relClampIndex(o.level, REL_LEVELS.length);
    add('推断强度：' + REL_LEVELS[lv].name, '接话边只画最强的若干条：强 / 中 / 全（点一下换一档）',
        o.infer && lv > 0, !o.infer, function () {
            o.level = (relClampIndex(o.level, REL_LEVELS.length) + 1) % REL_LEVELS.length;
            relRefresh(domId, st);
        });

    var limits = relMemberLimits(st);
    if (limits.length > 1) {
        var at = Math.max(0, limits.indexOf(o.memberLimit));
        add(o.memberLimit > 0 ? '成员：前 ' + o.memberLimit + ' 位' : '成员：全部',
            '只画发言最多的这几位（点一下换一档）', o.memberLimit > 0, false, function () {
                o.memberLimit = limits[(at + 1) % limits.length];
                relRefresh(domId, st);
            });
    } else {
        o.memberLimit = 0;
    }
}

function relRefresh(domId, st) {
    relToolbar(domId, st);
    relDraw(domId, st);
}

// 力导向只用来"算位置"，算完就把坐标钉死，改用 layout:'none' 重画一遍。
// 两个理由都是实测出来的：
//   1. 力导向不会自己收边：30 个节点在 460px 高的画布里能撑到 y ∈ [-80, 565]，
//      两端的节点与昵称被直接裁掉，而左右两半是空的；
//   2. 每次 setOption 都会重跑一遍力导向（没有已保存坐标时用 Math.random 撒初始点），
//      位置每次都变——"先画一遍、再补一个 zoom 去适配"必然落空：补的那一版立刻被重排，
//      而且 ECharts 会把 zoom 归一化，算好的比例根本落不到实处。
//
// layout:'none' 的摆法在 ECharts 源码里是确定的：把数据包围盒**等比**装进一个
// "画布四周各缩进 10%" 的框并居中（createCoordinateSystem：setBoundingRect = 数据
// 包围盒，setViewRect = 按 aspect 求出的框）。于是：
//   · 裁切问题自动消失——给什么范围就缩放到框里；
//   · 能控制的只有包围盒的宽高比：想让它铺满宽卡片，就得先把坐标横向拉开；
//   · 那 10% 的缩进正好是给昵称留的余量（正是它让右端节点的标签不再被画布切掉）。
// 拉伸上限 REL_STRETCH_MAX 是必要的：力导向本身各向同性，宽卡片上要铺满得横向拉
// 2 倍以上，再大节点群就明显"被擀扁"了。
function relStretchLayout(chart, el, nodes) {
    var s = chart.getModel().getSeriesByIndex(0);
    var data = s.getData();
    var cs = s.coordinateSystem;
    if (!cs || !cs.dataToPoint) return null;

    var w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return null;

    // dataToPoint 给出的已经是像素坐标，节点半径也是像素，两者单位一致
    var pts = [], x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, i, p, r;
    for (i = 0; i < data.count() && i < nodes.length; i++) {
        p = cs.dataToPoint(data.getItemLayout(i));
        if (!p || !isFinite(p[0]) || !isFinite(p[1])) return null;
        r = (relNum(nodes[i].symbolSize) / 2) || 6;
        pts.push(p);
        x0 = Math.min(x0, p[0] - r); x1 = Math.max(x1, p[0] + r);
        y0 = Math.min(y0, p[1] - r); y1 = Math.max(y1, p[1] + r);
    }
    if (!pts.length || !isFinite(x0) || x1 <= x0 || y1 <= y0) return null;

    // 目标宽高比 = 画布宽高比（那个框是四周等比缩进 10%，比例与画布相同）
    var k = (w / h) / ((x1 - x0) / (y1 - y0));
    k = Math.min(Math.max(k, 1 / REL_STRETCH_MAX), REL_STRETCH_MAX);
    var sx = k > 1 ? k : 1, sy = k < 1 ? 1 / k : 1;
    var bcx = (x0 + x1) / 2, bcy = (y0 + y1) / 2;
    return pts.map(function (q) {
        return [bcx + (q[0] - bcx) * sx, bcy + (q[1] - bcy) * sy];
    });
}

function relDraw(domId, st) {
    var el = document.getElementById(domId);
    if (!el) return;
    var built = relSeriesData(st.interaction, st.activity, st.opts);
    var note = document.getElementById(domId + 'Note');
    // 复用已有实例：换档位只是重画数据，dispose + init 会让整块画布闪一下
    var chart = _CHARTS[domId];
    if (!chart || chart.isDisposed() || chart.getDom() !== el) chart = mountChart(domId);
    if (!chart) return;
    // clear 之后再画：不 clear 的话力导向会拿上一轮的坐标当起点，同一个档位每次
    // 点出来的布局都不一样（"为什么我点一下接话，人就全跑位了"）
    chart.clear();
    if (!built.nodes.length) {
        if (note) note.textContent = '没有可画的成员（群里还没有能归属到人的发言）';
        return;
    }
    chart.setOption(built.option, true);

    // 第二遍：把算好的坐标钉进数据里，让位置完全由我们决定。
    // 取不到坐标就退回纯力导向——宁可不好看，也不能让图整块消失。
    var pinned = relStretchLayout(chart, el, built.nodes);
    if (pinned) {
        chart.setOption({
            series: [{
                layout: 'none',
                data: built.nodes.map(function (nd, k) {
                    var p = $.extend({}, nd);
                    p.x = pinned[k][0];
                    p.y = pinned[k][1];
                    return p;
                })
            }]
        });
    }
    if (note) note.textContent = built.note;
}

// 互动关系图：节点大小 = 发言量；实线 = 精确回复/@（事实），虚线 = 相邻接话（推断）
// opts 见 REL_DEFAULTS；报告页传 { controls: false } 关掉交互控件
function renderRelationGraph(domId, interaction, activity, opts) {
    var el = document.getElementById(domId);
    if (!el) return;
    var st = _REL_STATE[domId];
    if (!st) {
        st = _REL_STATE[domId] = { opts: relOpts(opts) };
        relShell(domId, st);
    } else {
        // 重复渲染时只覆盖显式传进来的项，别把控件调好的档位重置回默认值
        st.opts = relOpts(opts ? $.extend({}, st.opts, opts) : st.opts);
    }
    st.interaction = interaction || {};
    st.activity = activity;
    relRefresh(domId, st);
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
    renderGroupEmotionTrend(data);
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

// 群聊情绪走势只出现在情绪页；仪表盘复用同一渲染函数时没有该容器，直接跳过。
function renderGroupEmotionTrend(data) {
    var el = document.getElementById('emotionTrend');
    if (!el) return;
    var months = monthsOf(data);
    if (!months.length) return;
    var chart = mountChart('emotionTrend');
    if (!chart) return;
    el.classList.remove('d-none');
    var empty = document.getElementById('emotionTrendEmpty');
    if (empty) empty.classList.add('d-none');
    chart.setOption(applyChartTheme({
        tooltip: { trigger: 'axis' },
        grid: { left: 8, right: 16, top: 24, bottom: 24, containLabel: true },
        xAxis: { type: 'category', data: months },
        yAxis: { type: 'value', min: 0, max: 10, name: '强度' },
        series: [{
            type: 'line', smooth: true, symbolSize: 8, areaStyle: { opacity: 0.18 },
            label: { show: true, color: T.text, fontSize: 11,
                     formatter: function (p) { return (data[months[p.dataIndex]] || {}).group_emotion || ''; } },
            data: months.map(function (m) { return (data[m] || {}).group_intensity || 0; })
        }]
    }));
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
        if (!data) {
            setAnalysisEmptyState(dim, 'empty');
            return;
        }
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
            setAnalysisEmptyState(dim, 'ready');
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
            var cancelled = msg === '分析已取消';
            setAnalysisEmptyState(dim, cancelled ? 'empty' : 'error', cancelled ? null : msg);
            status.html('<div class="alert alert-' + (cancelled ? 'secondary' : 'danger')
                + ' mb-0">' + esc(msg) + '</div>');
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
