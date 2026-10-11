// Read-only preview and explicit unit selection for analysis runs.
var lastAnalysisPlanOpener = null;
document.addEventListener('click', function (event) {
    if (!event.target.closest) return;
    var trigger = event.target.closest('.btn-analyze, #btn-analyze-all, #btn-recap, #inPageAnalyze');
    if (trigger) lastAnalysisPlanOpener = trigger;
}, true);

function openAnalysisPlan(dimensions, opts) {
    var query = [];
    (dimensions || []).forEach(function (dim) {
        query.push('dimensions=' + encodeURIComponent(dim));
    });
    if (opts.refresh) query.push('refresh=1');
    var url = '/api/analysis-plan' + (query.length ? '?' + query.join('&') : '');
    var opener = document.activeElement;
    if ((!opener || opener === document.body || opener.disabled) && lastAnalysisPlanOpener) {
        opener = lastAnalysisPlanOpener;
    }
    var dialog = document.getElementById('analysisPlanDialog');
    if (dialog) dialog.remove();
    dialog = document.createElement('dialog');
    dialog.id = 'analysisPlanDialog';
    dialog.className = 'analysis-plan-dialog';
    document.body.appendChild(dialog);

    var settled = false;
    var selected = null;
    var currentPlan = null;

    function restoreOpenerFocus() {
        if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    }

    function closeWithError(message) {
        if (settled) return;
        settled = true;
        if (dialog.open) dialog.close();
        dialog.remove();
        opts.onError(message);
        restoreOpenerFocus();
        if (lastAnalysisPlanOpener === opener) lastAnalysisPlanOpener = null;
    }

    dialog.addEventListener('cancel', function (event) {
        event.preventDefault();
        closeWithError('分析已取消');
    });
    dialog.addEventListener('close', function () { closeWithError('分析已取消'); });

    function formatRange(minimum, maximum) {
        return minimum === maximum ? String(minimum) : minimum + '–' + maximum;
    }

    function number(value) {
        var parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function dimensionLabel(dimension) {
        return {
            emotion: '情绪分析',
            topics: '话题趋势',
            relationship: '人际关系',
            habits: '个人习惯',
            profile: '人物画像',
            group_dynamics: '群聊动态',
            group_topics: '群聊话题',
            group_emotion: '群聊情绪',
            member_profiles: '成员画像',
            recap: '整体总括'
        }[dimension] || dimension;
    }

    function render(plan, warning) {
        if (settled) return;
        var wasOpen = dialog.open;
        currentPlan = plan;
        var units = plan.units || [];
        var runnable = units.filter(function (unit) { return unit.status !== '无可分析消息'; });
        if (selected === null) {
            selected = new Set(runnable.map(function (unit) { return unit.key; }));
        } else {
            var valid = new Set(runnable.map(function (unit) { return unit.key; }));
            selected = new Set(Array.from(selected).filter(function (key) { return valid.has(key); }));
        }
        dialog.replaceChildren();

        var header = document.createElement('div');
        header.className = 'analysis-plan-header';
        var title = document.createElement('h3');
        title.id = 'analysisPlanTitle';
        title.textContent = '分析计划预览';
        header.appendChild(title);
        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close';
        close.setAttribute('aria-label', '关闭预览');
        close.addEventListener('click', function () { closeWithError('分析已取消'); });
        header.appendChild(close);
        dialog.setAttribute('aria-labelledby', title.id);
        dialog.appendChild(header);

        var summary = plan.summary || {};
        var recap = document.createElement('section');
        recap.className = 'analysis-plan-summary';
        recap.setAttribute('aria-label', '计划估算摘要');
        var summaryHeading = document.createElement('div');
        summaryHeading.className = 'analysis-plan-summary-heading';
        var model = document.createElement('span');
        model.className = 'analysis-plan-model';
        model.textContent = '模型：' + (plan.model || '未指定');
        summaryHeading.appendChild(model);
        var scope = document.createElement('span');
        scope.className = 'analysis-plan-scope';
        scope.textContent = '范围：' + (plan.scope_summary || '全量对话');
        summaryHeading.appendChild(scope);
        recap.appendChild(summaryHeading);

        var summaryGrid = document.createElement('dl');
        summaryGrid.className = 'analysis-plan-summary-grid';
        function summaryItem(label, className, live) {
            var item = document.createElement('div');
            item.className = 'analysis-plan-summary-item' + (className ? ' ' + className : '');
            var term = document.createElement('dt');
            term.textContent = label;
            item.appendChild(term);
            var value = document.createElement('dd');
            if (live) {
                value.setAttribute('role', 'status');
                value.setAttribute('aria-live', 'polite');
                value.className = 'analysis-plan-selection-count';
            }
            item.appendChild(value);
            summaryGrid.appendChild(item);
            return value;
        }
        var chosenCount = summaryItem('已选单元', 'analysis-plan-summary-selection', true);
        var cacheCount = summaryItem('缓存命中', '', false);
        var textCallsValue = summaryItem('文本调用', '', false);
        var visionCallsValue = summaryItem('图片摘要调用', '', false);
        var inputTokensValue = summaryItem('输入 token 估算', '', false);
        var outputTokensValue = summaryItem('输出 token 上限', '', false);
        var costValue = summaryItem('费用估算', 'analysis-plan-summary-cost', false);
        recap.appendChild(summaryGrid);
        dialog.appendChild(recap);

        var body = document.createElement('div');
        body.className = 'analysis-plan-body';
        if (warning) {
            var warningBox = document.createElement('div');
            warningBox.className = 'alert alert-warning py-2 small analysis-plan-revalidation';
            warningBox.setAttribute('role', 'alert');
            warningBox.textContent = warning;
            body.appendChild(warningBox);
        }

        var notices = document.createElement('div');
        notices.className = 'analysis-plan-notices';
        var staticNoticeCount = 0;
        var incompleteCostNotice = null;
        function addNotice(message, dynamic) {
            var notice = document.createElement('div');
            notice.className = 'alert alert-warning py-2 small analysis-plan-notice';
            notice.textContent = message;
            if (dynamic) notice.hidden = true;
            else staticNoticeCount += 1;
            notices.appendChild(notice);
            return notice;
        }
        if (plan.refresh) {
            addNotice('已启用强制刷新：本次会忽略缓存，可能产生新的模型调用和费用。');
        }
        var uncertainImages = units.some(function (unit) { return unit.vision_uncertain && !unit.cached; });
        if (uncertainImages) {
            addNotice('部分单元含图片，摘要调用与文本调用分开估算；实际图片 token 不计入文本 token 区间。');
        }
        if (!summary.cost) {
            addNotice('未配置可识别的模型价格，无法估算费用。');
        } else if (uncertainImages) {
            incompleteCostNotice = addNotice('图片摘要输入暂时未知，费用估算只显示已知文本的下限。', true);
        }
        if (notices.childElementCount) body.appendChild(notices);

        var notes = document.createElement('details');
        notes.className = 'analysis-plan-notes';
        var notesSummary = document.createElement('summary');
        notesSummary.textContent = '查看估算说明';
        notes.appendChild(notesSummary);
        var notesContent = document.createElement('div');
        notesContent.className = 'analysis-plan-notes-content';
        [summary.token_note, summary.vision_note, summary.cost_note].filter(Boolean).forEach(function (text) {
            var paragraph = document.createElement('p');
            paragraph.textContent = text;
            notesContent.appendChild(paragraph);
        });
        notes.appendChild(notesContent);
        body.appendChild(notes);

        var controls = document.createElement('div');
        controls.className = 'analysis-plan-controls';
        var selectAll = document.createElement('button');
        selectAll.type = 'button';
        selectAll.className = 'btn btn-sm btn-outline-secondary';
        selectAll.textContent = '全选可运行项';
        controls.appendChild(selectAll);
        var clearAll = document.createElement('button');
        clearAll.type = 'button';
        clearAll.className = 'btn btn-sm btn-outline-secondary';
        clearAll.textContent = '清空选择';
        controls.appendChild(clearAll);
        body.appendChild(controls);

        var wrap = document.createElement('div');
        wrap.className = 'analysis-plan-table-wrap';
        var table = document.createElement('table');
        table.className = 'table table-sm align-middle analysis-plan-table';
        table.setAttribute('aria-label', '分析单元计划');
        var thead = document.createElement('thead');
        var headRow = document.createElement('tr');
        ['选择', '维度 / 单元', '缓存状态', '输入字符', 'token 估算', '调用估算'].forEach(function (text) {
            var cell = document.createElement('th');
            cell.scope = 'col';
            cell.textContent = text;
            headRow.appendChild(cell);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);
        var tbody = document.createElement('tbody');
        units.forEach(function (unit) {
            var row = document.createElement('tr');
            var name = dimensionLabel(unit.dimension) + '：' + unit.label;

            var selectCell = document.createElement('td');
            selectCell.dataset.label = '选择';
            var selectLabel = document.createElement('label');
            selectLabel.className = 'analysis-plan-select';
            var checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.className = 'form-check-input';
            checkbox.dataset.unitKey = unit.key;
            checkbox.checked = selected.has(unit.key);
            checkbox.disabled = unit.status === '无可分析消息';
            checkbox.setAttribute('aria-label', (checkbox.disabled ? '不可选择：' : '选择') + name);
            checkbox.addEventListener('change', function () {
                if (checkbox.checked) selected.add(unit.key); else selected.delete(unit.key);
                updateSelectionSummary();
                updateRunButton();
            });
            selectLabel.appendChild(checkbox);
            var selectText = document.createElement('span');
            selectText.textContent = '选择此单元';
            selectLabel.appendChild(selectText);
            selectCell.appendChild(selectLabel);
            row.appendChild(selectCell);

            var labelCell = document.createElement('td');
            labelCell.dataset.label = '维度 / 单元';
            var label = document.createElement('span');
            label.className = 'analysis-plan-unit-name';
            label.textContent = dimensionLabel(unit.dimension) + ' · ' + unit.label;
            labelCell.appendChild(label);
            if (unit.thinking) {
                var thinking = document.createElement('small');
                thinking.className = 'd-block text-muted';
                thinking.textContent = '思考模式';
                labelCell.appendChild(thinking);
            }
            row.appendChild(labelCell);

            [
                ['缓存状态', unit.status || '待调用'],
                ['输入字符', number(unit.input_chars).toLocaleString()],
                ['token 估算', formatRange(
                    number(unit.input_tokens_min).toLocaleString(),
                    number(unit.input_tokens_max).toLocaleString()
                )],
                ['调用估算', '文本 ' + formatRange(number(unit.text_calls_min), number(unit.text_calls_max)) +
                    ' · 图片 ' + formatRange(number(unit.vision_calls_min), number(unit.vision_calls_max))]
            ].forEach(function (item) {
                var cell = document.createElement('td');
                cell.dataset.label = item[0];
                cell.textContent = item[1];
                row.appendChild(cell);
            });
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        body.appendChild(wrap);
        dialog.appendChild(body);

        var footer = document.createElement('div');
        footer.className = 'analysis-plan-footer';
        var cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn-outline-secondary';
        cancel.textContent = '取消';
        cancel.addEventListener('click', function () { closeWithError('分析已取消'); });
        footer.appendChild(cancel);
        var run = document.createElement('button');
        run.type = 'button';
        run.className = 'btn btn-primary';
        run.textContent = opts.refresh ? '忽略缓存并开始' : '按所选单元开始';
        run.addEventListener('click', submit);
        footer.appendChild(run);
        dialog.appendChild(footer);

        function updateSelectionSummary() {
            var chosen = runnable.filter(function (unit) { return selected.has(unit.key); });
            var uncached = chosen.filter(function (unit) { return !unit.cached; });
            var inputMin = uncached.reduce(function (sum, unit) {
                return sum + (unit.text_calls_min > 0 ? number(unit.input_tokens_min) : 0);
            }, 0);
            var inputMax = uncached.reduce(function (sum, unit) {
                return sum + (unit.text_calls_max > 0 && !unit.vision_uncertain ? number(unit.input_tokens_max) : 0);
            }, 0);
            var textMin = chosen.reduce(function (sum, unit) { return sum + number(unit.text_calls_min); }, 0);
            var textMax = chosen.reduce(function (sum, unit) { return sum + number(unit.text_calls_max); }, 0);
            var visionMax = chosen.reduce(function (sum, unit) { return sum + number(unit.vision_calls_max); }, 0);
            var visionMin = !!plan.refresh && visionMax ? 1 : 0;
            var outputMax = uncached.reduce(function (sum, unit) { return sum + number(unit.max_output_tokens); }, 0);
            var cacheHits = chosen.filter(function (unit) { return unit.cached; }).length;
            var uncertain = uncached.some(function (unit) { return unit.vision_uncertain; });
            chosenCount.textContent = chosen.length + ' / ' + runnable.length + ' 个';
            cacheCount.textContent = cacheHits + ' 个';
            textCallsValue.textContent = formatRange(textMin, textMax) + ' 次';
            visionCallsValue.textContent = formatRange(visionMin, visionMax) + ' 次';
            inputTokensValue.textContent = formatRange(inputMin.toLocaleString(), inputMax.toLocaleString());
            outputTokensValue.textContent = '最多 ' + outputMax.toLocaleString();
            if (incompleteCostNotice) {
                incompleteCostNotice.hidden = !uncertain;
                notices.hidden = staticNoticeCount === 0 && !uncertain;
            }

            if (!summary.cost) {
                costValue.textContent = '未配置价格';
            } else {
                var priceIn = number(summary.price_input_per_million);
                var priceOut = number(summary.price_output_per_million);
                var costMin = inputMin / 1000000 * priceIn;
                var costMax = (inputMax * priceIn + outputMax * priceOut) / 1000000;
                costValue.textContent = uncertain
                    ? '已知文本费用至少 ¥' + costMin.toFixed(4)
                    : '预计费用 ¥' + costMin.toFixed(4) + '–¥' + costMax.toFixed(4);
            }
        }

        function updateRunButton() { run.disabled = selected.size === 0; }
        function syncCheckboxes() {
            wrap.querySelectorAll('input[data-unit-key]').forEach(function (checkbox) {
                checkbox.checked = selected.has(checkbox.dataset.unitKey);
            });
        }
        selectAll.addEventListener('click', function () {
            selected = new Set(runnable.map(function (unit) { return unit.key; }));
            syncCheckboxes();
            updateSelectionSummary();
            updateRunButton();
        });
        clearAll.addEventListener('click', function () {
            selected = new Set();
            syncCheckboxes();
            updateSelectionSummary();
            updateRunButton();
        });
        updateSelectionSummary();
        updateRunButton();
        if (!dialog.open) {
            dialog.showModal();
            close.focus();
        } else if (warning) {
            (run.disabled ? close : run).focus();
        }
    }

    function submit() {
        var button = dialog.querySelector('.analysis-plan-footer .btn-primary');
        if (!selected || !selected.size || !currentPlan) return;
        button.disabled = true;
        button.textContent = '重新核对计划…';
        $.ajax({
            url: '/api/analyze-plan',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({
                plan_id: currentPlan.plan_id,
                dimensions: currentPlan.dimensions,
                refresh: !!opts.refresh,
                selected_units: Array.from(selected)
            })
        }).done(function (data) {
            if (!data.job) { closeWithError('未知响应格式'); return; }
            settled = true;
            dialog.close();
            dialog.remove();
            opts.dim = dimensions && dimensions.length === 1 ? dimensions[0] : null;
            pollAnalyzeJob(data.job, opts);
        }).fail(function (xhr) {
            var body = xhr.responseJSON || {};
            if (xhr.status === 409 && body.changed && body.plan) {
                render(body.plan, body.error || '缓存或配置已变化，请检查更新后的计划，再次点击开始。');
                return;
            }
            closeWithError(body.error || '启动分析失败');
        });
    }

    if (!dialog.showModal) {
        dialog.remove();
        opts.onError('当前浏览器不支持计划预览对话框');
        return;
    }
    $.get(url).done(function (plan) { render(plan); }).fail(function (xhr) {
        var body = xhr.responseJSON || {};
        closeWithError(body.error || '读取分析计划失败');
    });
}
