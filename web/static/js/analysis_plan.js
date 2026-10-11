// Read-only preview and explicit unit selection for analysis runs.
function openAnalysisPlan(dimensions, opts) {
    var query = [];
    (dimensions || []).forEach(function (dim) {
        query.push('dimensions=' + encodeURIComponent(dim));
    });
    if (opts.refresh) query.push('refresh=1');
    var url = '/api/analysis-plan' + (query.length ? '?' + query.join('&') : '');
    var dialog = document.getElementById('analysisPlanDialog');
    if (dialog) dialog.remove();
    dialog = document.createElement('dialog');
    dialog.id = 'analysisPlanDialog';
    dialog.className = 'analysis-plan-dialog';
    document.body.appendChild(dialog);

    var settled = false;
    var selected = null;
    var currentPlan = null;

    function closeWithError(message) {
        if (settled) return;
        settled = true;
        if (dialog.open) dialog.close();
        dialog.remove();
        opts.onError(message);
    }

    dialog.addEventListener('cancel', function (event) {
        event.preventDefault();
        closeWithError('分析已取消');
    });
    dialog.addEventListener('close', function () { closeWithError('分析已取消'); });

    function formatRange(minimum, maximum) {
        return minimum === maximum ? String(minimum) : minimum + '–' + maximum;
    }

    function render(plan, warning) {
        if (settled) return;
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
        title.textContent = '分析计划预览';
        header.appendChild(title);
        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close';
        close.setAttribute('aria-label', '关闭预览');
        close.addEventListener('click', function () { closeWithError('已取消分析计划'); });
        header.appendChild(close);
        dialog.appendChild(header);

        var summary = plan.summary || {};
        var recap = document.createElement('div');
        recap.className = 'analysis-plan-summary';
        function updateSelectionSummary() {
            var chosen = runnable.filter(function (unit) { return selected.has(unit.key); });
            var uncached = chosen.filter(function (unit) { return !unit.cached; });
            var inputMin = uncached.reduce(function (sum, unit) {
                return sum + (unit.text_calls_min > 0 ? unit.input_tokens_min : 0);
            }, 0);
            var inputMax = uncached.reduce(function (sum, unit) {
                return sum + (unit.text_calls_max > 0 && !unit.vision_uncertain ? unit.input_tokens_max : 0);
            }, 0);
            var textMin = chosen.reduce(function (sum, unit) { return sum + unit.text_calls_min; }, 0);
            var textMax = chosen.reduce(function (sum, unit) { return sum + unit.text_calls_max; }, 0);
            var visionMax = chosen.reduce(function (sum, unit) { return sum + unit.vision_calls_max; }, 0);
            var visionMin = !!plan.refresh && visionMax ? 1 : 0;
            var outputMax = uncached.reduce(function (sum, unit) { return sum + unit.max_output_tokens; }, 0);
            var costText = '未配置模型价格，不估算费用';
            if (summary.cost) {
                var uncertain = uncached.some(function (unit) { return unit.vision_uncertain; });
                var priceIn = Number(summary.price_input_per_million || 0);
                var priceOut = Number(summary.price_output_per_million || 0);
                var costMin = inputMin / 1000000 * priceIn;
                var costMax = (inputMax * priceIn + outputMax * priceOut) / 1000000;
                costText = uncertain
                    ? '已知文本费用至少 ¥' + costMin.toFixed(4) + '，图像摘要输入未知'
                    : '预计费用 ¥' + costMin.toFixed(4) + '–¥' + costMax.toFixed(4);
            }
            var cacheHits = chosen.filter(function (unit) { return unit.cached; }).length;
            recap.textContent = plan.model + ' · 当前选中 ' + chosen.length + ' 个单元，其中 ' +
                cacheHits + ' 个命中缓存 · 文本调用 ' + formatRange(textMin, textMax) +
                ' 次 · 图片摘要 ' + formatRange(visionMin, visionMax) + ' 次 · 输入 token 约 ' +
                formatRange(inputMin, inputMax) + ' · ' + costText +
                ' · 范围：' + (plan.scope_summary || '全量对话');
        }
        updateSelectionSummary();
        dialog.appendChild(recap);

        if (warning) {
            var warningBox = document.createElement('div');
            warningBox.className = 'alert alert-warning py-2 small';
            warningBox.textContent = warning;
            dialog.appendChild(warningBox);
        }

        var notes = document.createElement('p');
        notes.className = 'small text-muted analysis-plan-notes';
        notes.textContent = summary.token_note + ' ' + summary.vision_note + ' ' + summary.cost_note;
        dialog.appendChild(notes);

        var controls = document.createElement('div');
        controls.className = 'analysis-plan-controls';
        var selectAll = document.createElement('button');
        selectAll.type = 'button';
        selectAll.className = 'btn btn-sm btn-outline-secondary';
        selectAll.textContent = '全选可运行项';
        selectAll.addEventListener('click', function () {
            selected = new Set(runnable.map(function (unit) { return unit.key; }));
            render(plan, warning);
        });
        controls.appendChild(selectAll);
        var clearAll = document.createElement('button');
        clearAll.type = 'button';
        clearAll.className = 'btn btn-sm btn-outline-secondary';
        clearAll.textContent = '清空选择';
        clearAll.addEventListener('click', function () { selected = new Set(); render(plan, warning); });
        controls.appendChild(clearAll);
        dialog.appendChild(controls);

        var wrap = document.createElement('div');
        wrap.className = 'analysis-plan-table-wrap';
        var table = document.createElement('table');
        table.className = 'table table-sm align-middle analysis-plan-table';
        var thead = document.createElement('thead');
        thead.innerHTML = '<tr><th scope="col">选择</th><th scope="col">维度 / 单元</th>' +
            '<th scope="col">缓存状态</th><th scope="col">输入字符</th>' +
            '<th scope="col">token 估算</th><th scope="col">调用估算</th></tr>';
        table.appendChild(thead);
        var tbody = document.createElement('tbody');
        units.forEach(function (unit) {
            var row = document.createElement('tr');
            var selectCell = document.createElement('td');
            var checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.className = 'form-check-input';
            checkbox.checked = selected.has(unit.key);
            checkbox.disabled = unit.status === '无可分析消息';
            checkbox.addEventListener('change', function () {
                if (checkbox.checked) selected.add(unit.key); else selected.delete(unit.key);
                updateSelectionSummary();
                updateRunButton();
            });
            selectCell.appendChild(checkbox);
            row.appendChild(selectCell);

            var labelCell = document.createElement('td');
            var label = document.createElement('span');
            label.textContent = unit.dimension + ' · ' + unit.label;
            labelCell.appendChild(label);
            if (unit.thinking) {
                var thinking = document.createElement('small');
                thinking.className = 'd-block text-muted';
                thinking.textContent = '思考模式';
                labelCell.appendChild(thinking);
            }
            row.appendChild(labelCell);

            [unit.status, unit.input_chars.toLocaleString(),
                formatRange(unit.input_tokens_min.toLocaleString(), unit.input_tokens_max.toLocaleString()),
                '文本 ' + formatRange(unit.text_calls_min, unit.text_calls_max) +
                    ' · 图片 ' + formatRange(unit.vision_calls_min, unit.vision_calls_max)
            ].forEach(function (value) {
                var cell = document.createElement('td');
                cell.textContent = value;
                row.appendChild(cell);
            });
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        dialog.appendChild(wrap);

        var footer = document.createElement('div');
        footer.className = 'analysis-plan-footer';
        var cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn-outline-secondary';
        cancel.textContent = '取消';
        cancel.addEventListener('click', function () { closeWithError('已取消分析计划'); });
        footer.appendChild(cancel);
        var run = document.createElement('button');
        run.type = 'button';
        run.className = 'btn btn-primary';
        run.textContent = opts.refresh ? '忽略缓存并开始' : '按所选单元开始';
        run.addEventListener('click', submit);
        footer.appendChild(run);
        dialog.appendChild(footer);
        function updateRunButton() { run.disabled = selected.size === 0; }
        updateRunButton();
        if (!dialog.open) dialog.showModal();
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
    $.get(url).done(render).fail(function (xhr) {
        var body = xhr.responseJSON || {};
        closeWithError(body.error || '读取分析计划失败');
    });
}
