(function () {
    'use strict';

    const API_OK = document.body.getAttribute('data-api-key') === '1';
    const root = document.querySelector('[data-scenes-map-agent="scenemap"]');
    if (!root) return;

    const els = {
        model: document.getElementById('ssm-model'),
        videoDynamics: document.getElementById('ssm-video-dynamics'),
        elementsLabel: root.querySelector('[data-ssm-elements-label]'),
        elementsPanel: root.querySelector('.scenes-map-elements-panel'),
        elementsBtn: root.querySelector('[data-ssm-elements-dropdown] .rewrite-anim-dropdown__btn'),
        elementsChecks: root.querySelectorAll('[data-ssm-elements-checkbox]'),
        systemToggle: root.querySelector('[data-ssm-system-toggle]'),
        systemWrap: root.querySelector('[data-ssm-system-wrap]'),
        systemTa: root.querySelector('[data-ssm-system-prompt]'),
        systemBadge: root.querySelector('[data-ssm-system-badge]'),
        userToggle: root.querySelector('[data-ssm-user-toggle]'),
        userWrap: root.querySelector('[data-ssm-user-wrap]'),
        userTa: root.querySelector('[data-ssm-user-prompt]'),
        userBadge: root.querySelector('[data-ssm-user-badge]'),
        inputResultTa: root.querySelector('[data-ssm-input-result]'),
        inputResultBadge: root.querySelector('[data-ssm-input-result-badge]'),
        resultToggle: root.querySelector('[data-ssm-result-toggle]'),
        resultWrap: root.querySelector('[data-ssm-result-wrap]'),
        resultTa: root.querySelector('[data-ssm-result]'),
        resultBadge: root.querySelector('[data-ssm-result-badge]'),
        resultCounts: root.querySelector('[data-ssm-result-counts]'),
        resultCopy: root.querySelector('[data-ssm-result-copy]'),
        runBtn: root.querySelector('[data-ssm-run]'),
        statusRow: root.querySelector('[data-ssm-status-row]'),
        statusText: root.querySelector('[data-ssm-status-text]'),
        cancelBtn: root.querySelector('[data-ssm-cancel-btn]'),
        progressWrap: root.querySelector('[data-ssm-progress]'),
    };

    let saveTimer = null;
    let generating = false;
    let statusTimer = null;
    let runStartedAt = 0;
    let abortController = null;

    function formatNumRu(n) {
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }

    function setBadgeYesNo(badge, text, prefix) {
        if (!badge) return;
        const yes = !!(String(text || '').trim());
        badge.classList.toggle('badge-yes', yes);
        badge.classList.toggle('badge-no', !yes);
        if (prefix) badge.textContent = prefix + ': ' + (yes ? 'YES' : 'NO');
    }

    function setInputResultBadge() {
        if (!els.inputResultBadge || !els.inputResultTa) return;
        const yes = !!(els.inputResultTa.value || '').trim();
        els.inputResultBadge.classList.toggle('badge-yes', yes);
        els.inputResultBadge.classList.toggle('badge-no', !yes);
    }

    function updateResultCounts() {
        if (!els.resultCounts || !els.resultTa) return;
        const text = els.resultTa.value || '';
        els.resultCounts.textContent = text.trim()
            ? formatNumRu(text.length) + ' симв.'
            : '';
    }

    function collectPayload(extra) {
        const elements = [];
        els.elementsChecks.forEach(function (cb) {
            if (cb.checked) elements.push(cb.value);
        });
        return Object.assign({
            scenemap_model: els.model ? els.model.value : '',
            scenemap_video_dynamics_mode: els.videoDynamics ? els.videoDynamics.value : '',
            scenemap_elements_used: elements,
            scenemap_system_prompt: els.systemTa ? els.systemTa.value : '',
            scenemap_user_prompt: els.userTa ? els.userTa.value : '',
            scenemap_result: els.resultTa ? els.resultTa.value : '',
            result_as: els.inputResultTa ? els.inputResultTa.value : '',
        }, extra || {});
    }

    function scheduleSave(delayMs) {
        if (!API_OK) return;
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
            saveTimer = null;
            savePrefs().catch(function () { /* ignore */ });
        }, delayMs == null ? 400 : delayMs);
    }

    async function savePrefs(extra) {
        const r = await fetch('/scenes-map/api/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(collectPayload(extra)),
        });
        const rawText = await r.text();
        let data = {};
        try {
            data = rawText ? JSON.parse(rawText) : {};
        } catch (_e) {
            data = {};
        }
        if (!r.ok || !data.ok) {
            const msg = (data && (data.error || data.message)) || ('HTTP ' + r.status);
            const err = new Error(msg || 'Ошибка сохранения');
            err.rawResponse = rawText && rawText.trim() ? rawText : JSON.stringify(data, null, 2);
            throw err;
        }
        return data;
    }

    function syncResultWrapIdleStatus() {
        if (!els.resultWrap || !els.resultTa) return;
        if ((els.resultTa.value || '').trim()) {
            els.resultWrap.setAttribute('data-status', 'done');
        } else {
            els.resultWrap.setAttribute('data-status', 'pending');
        }
    }

    function clearStageStatus() {
        if (statusTimer) {
            clearInterval(statusTimer);
            statusTimer = null;
        }
        runStartedAt = 0;
        root.classList.remove('rewrite-stage-card--running', 'rewrite-stage-card--error');
        if (els.statusRow) {
            els.statusRow.classList.add('hidden');
            els.statusRow.hidden = true;
        }
        if (els.statusText) {
            els.statusText.textContent = '';
            els.statusText.classList.remove('slot-status-with-spinner');
        }
        if (els.cancelBtn) {
            els.cancelBtn.classList.add('hidden');
            els.cancelBtn.disabled = true;
        }
        if (els.resultTa) {
            els.resultTa.classList.remove('rewrite-stage-result--busy', 'rewrite-stage-result--error');
        }
        generating = false;
        if (els.runBtn) els.runBtn.disabled = !API_OK;
        syncResultWrapIdleStatus();
    }

    function setStageStatus(text, opts) {
        opts = opts || {};
        const visible = !!text;
        const running = !!opts.running;
        const isError = !!opts.error;

        if (els.statusRow) {
            els.statusRow.classList.toggle('hidden', !visible);
            els.statusRow.hidden = !visible;
        }
        if (els.statusText) {
            els.statusText.textContent = text || '';
            els.statusText.classList.toggle('slot-status-with-spinner', running && !isError);
        }
        if (els.cancelBtn) {
            els.cancelBtn.classList.toggle('hidden', !(running && !isError));
            els.cancelBtn.disabled = !(running && !isError);
        }

        root.classList.toggle('rewrite-stage-card--running', running && !isError);
        root.classList.toggle('rewrite-stage-card--error', visible && isError);

        if (els.resultWrap) {
            if (running && !isError) {
                els.resultWrap.setAttribute('data-status', 'generating');
            } else if (isError) {
                els.resultWrap.setAttribute('data-status', 'error');
            } else {
                syncResultWrapIdleStatus();
            }
        }
        if (els.resultTa) {
            els.resultTa.classList.toggle('rewrite-stage-result--busy', running && !isError);
            els.resultTa.classList.toggle('rewrite-stage-result--error', isError);
        }

        generating = running;
        if (els.runBtn) els.runBtn.disabled = running || !API_OK;
    }

    function pushStageStatus(msg) {
        const elapsed = runStartedAt > 0
            ? Math.max(0, Math.floor((Date.now() - runStartedAt) / 1000))
            : 0;
        setStageStatus((msg || 'Выполнение…') + ' (' + elapsed + 's)', { running: true });
        if (els.resultTa) {
            els.resultTa.classList.add('rewrite-stage-result--busy');
        }
    }

    function showStageError(text, rawResponse) {
        if (statusTimer) {
            clearInterval(statusTimer);
            statusTimer = null;
        }
        runStartedAt = 0;
        abortController = null;
        generating = false;
        if (els.runBtn) els.runBtn.disabled = !API_OK;

        root.classList.remove('rewrite-stage-card--running');
        root.classList.add('rewrite-stage-card--error');
        if (els.statusRow) {
            els.statusRow.classList.add('hidden');
            els.statusRow.hidden = true;
        }
        if (els.statusText) {
            els.statusText.textContent = '';
            els.statusText.classList.remove('slot-status-with-spinner');
        }
        if (els.cancelBtn) {
            els.cancelBtn.classList.add('hidden');
            els.cancelBtn.disabled = true;
        }
        if (els.resultWrap) {
            els.resultWrap.setAttribute('data-status', 'error');
        }
        if (els.resultTa) {
            const err = String(text || '').trim();
            const raw = String(rawResponse || '').trim();
            if (raw) {
                els.resultTa.value = raw + '\n\n--- Ошибка ---\n' + err;
            } else {
                els.resultTa.value = 'Ошибка: ' + err;
            }
            els.resultTa.classList.remove('rewrite-stage-result--busy');
            els.resultTa.classList.add('rewrite-stage-result--error');
        }
        updateResultCounts();
    }

    function resolveStageErrorRaw(data) {
        if (!data || typeof data !== 'object') return '';
        if (data.raw && String(data.raw).trim()) return String(data.raw);
        if (Array.isArray(data.scenes) && data.scenes.length) {
            return data.scenes.map(function (s) { return JSON.stringify(s); }).join('\n');
        }
        return '';
    }

    function bindLockToggle(toggle, wrap, ta, onSave, opts) {
        if (!toggle || !ta) return;
        opts = opts || {};
        const alwaysVisible = !!opts.alwaysVisible;
        const collapseClass = opts.collapseClass || '';

        function setLocked(locked) {
            toggle.classList.toggle('rewrite-lock-toggle--locked', locked);
            ta.readOnly = locked;
            ta.classList.toggle('rewrite-source-textarea--locked', locked);
            toggle.title = locked ? 'Редактировать' : 'Сохранить';
            if (wrap && !alwaysVisible) {
                wrap.hidden = locked;
                wrap.classList.toggle('hidden', locked);
                wrap.style.display = locked ? 'none' : '';
            }
            if (collapseClass) {
                root.classList.toggle(collapseClass, locked);
            }
        }

        setLocked(true);

        toggle.addEventListener('click', function (ev) {
            ev.stopPropagation();
            if (!API_OK) return;
            const wasLocked = toggle.classList.contains('rewrite-lock-toggle--locked');
            if (wasLocked) {
                setLocked(false);
                ta.focus();
                return;
            }
            setLocked(true);
            if (typeof onSave === 'function') onSave();
            scheduleSave(0);
        });
    }

    function updateElementsLabel() {
        if (!els.elementsLabel) return;
        const labels = [];
        els.elementsChecks.forEach(function (cb) {
            if (!cb.checked) return;
            const opt = cb.closest('.scenes-map-elements-option');
            const title = opt && opt.querySelector('.rewrite-anim-option__title');
            labels.push(title ? title.textContent.trim() : cb.value);
        });
        els.elementsLabel.textContent = labels.length ? labels.join(', ') : '—';
    }

    function closeAllModePanels() {
        root.querySelectorAll('.rewrite-anim-dropdown__panel').forEach(function (p) {
            p.hidden = true;
            const btn = p.parentElement && p.parentElement.querySelector('.rewrite-anim-dropdown__btn');
            if (btn) btn.setAttribute('aria-expanded', 'false');
        });
    }

    function bindElementsDropdown() {
        if (!els.elementsBtn || !els.elementsPanel) return;

        els.elementsBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (els.elementsBtn.disabled) return;
            const open = els.elementsPanel.hidden;
            closeAllModePanels();
            els.elementsPanel.hidden = !open;
            els.elementsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        });

        els.elementsPanel.addEventListener('click', function (e) {
            e.stopPropagation();
        });

        root.querySelector('[data-ssm-elements-select-all]')?.addEventListener('click', function (e) {
            e.preventDefault();
            els.elementsChecks.forEach(function (cb) { cb.checked = true; });
            updateElementsLabel();
            scheduleSave(0);
        });

        root.querySelector('[data-ssm-elements-clear-all]')?.addEventListener('click', function (e) {
            e.preventDefault();
            els.elementsChecks.forEach(function (cb) { cb.checked = false; });
            updateElementsLabel();
            scheduleSave(0);
        });

        els.elementsChecks.forEach(function (cb) {
            cb.addEventListener('change', function () {
                updateElementsLabel();
                scheduleSave(0);
            });
        });

        updateElementsLabel();
    }

    function bindModeDropdown(field) {
        if (!field) return;
        const select = field.querySelector('.rewrite-mode-dropdown-select-hidden');
        const btn = field.querySelector('.rewrite-anim-dropdown__btn');
        const panel = field.querySelector('.rewrite-anim-dropdown__panel');
        const label = field.querySelector('[data-rewrite-mode-label]');
        const radioName = field.querySelector('.rewrite-mode-dropdown-radio')?.name;
        if (!select || !btn || !panel || !radioName) return;

        function syncLabel() {
            if (!label) return;
            const opt = select.options[select.selectedIndex];
            label.textContent = opt ? opt.textContent.trim() : '—';
        }

        function closePanel() {
            panel.hidden = true;
            btn.setAttribute('aria-expanded', 'false');
        }

        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (btn.disabled) return;
            if (!panel.hidden) {
                closePanel();
                return;
            }
            closeAllModePanels();
            panel.hidden = false;
            btn.setAttribute('aria-expanded', 'true');
        });

        panel.addEventListener('click', function (e) { e.stopPropagation(); });

        panel.addEventListener('change', function (e) {
            const rb = e.target;
            if (!rb || rb.name !== radioName) return;
            select.value = rb.value;
            syncLabel();
            closePanel();
            scheduleSave(0);
        });

        select.addEventListener('change', function () {
            panel.querySelectorAll('input[name="' + radioName + '"]').forEach(function (r) {
                r.checked = r.value === select.value;
            });
            syncLabel();
        });

        syncLabel();
    }

    function formatVisualCounts(counts) {
        if (!counts || typeof counts !== 'object') return '';
        return Object.keys(counts).map(function (k) {
            return k + '×' + counts[k];
        }).join(', ');
    }

    function formatCoverageBadge(coverage) {
        if (!coverage || !Object.keys(coverage).length) return '';
        const ok = !!coverage.ok;
        const cls = ok ? 'scenes-map-block-coverage--ok' : 'scenes-map-block-coverage--miss';
        let label = ok ? 'текст OK' : 'текст NO';
        const miss = Number(coverage.missing_fragment_chars || 0);
        if (!ok && miss > 0) {
            label += ' −' + formatNumRu(miss);
        }
        return ' <span class="scenes-map-block-coverage ' + cls + '">' + label + '</span>';
    }

    function renderProgressSummary(summary) {
        if (!summary || !summary.total_scenes) return '';
        const avgSec = summary.avg_duration_sec != null ? summary.avg_duration_sec : 0;
        return '<div class="scenes-map-scenemap-summary">' +
            'Всего сцен: <strong>' + formatNumRu(summary.total_scenes) + '</strong>' +
            ' · средняя длина: <strong>' + formatNumRu(summary.avg_chars || 0) + '</strong> символов' +
            ' (~<strong>' + String(avgSec).replace('.', ',') + '</strong> сек/сцена)' +
            '</div>';
    }

    function renderCoverageFooter(summary) {
        if (!summary || !summary.total_scenes) return '';
        const ok = !!summary.coverage_ok;
        const cls = ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no';
        let html = '<div class="scenes-map-scenemap-coverage">';
        html += '<div class="rewrite-scene-writer-check__head"><strong>Проверка текста (склейка сцен)</strong> ';
        html += '<span class="' + cls + '">' + (ok ? 'OK' : 'NO') + '</span></div>';
        html += '<div class="rewrite-scene-writer-check__hint rewrite-editor-check-line">';
        html += 'IN (block.text): <strong>' + formatNumRu(summary.source_chars || 0) + '</strong>';
        html += ' · пропуски: <strong class="' + (summary.missing_fragment_chars ? 'rewrite-scene-writer-check__no' : 'rewrite-scene-writer-check__ok') + '">' + formatNumRu(summary.missing_fragment_chars || 0) + '</strong> символов';
        html += ' · пробелы на стыках: <strong>' + formatNumRu(summary.boundary_whitespace_chars || 0) + '</strong> символов';
        html += '</div></div>';
        return html;
    }

    function renderProgress(blocks, doneMap, summary) {
        if (!els.progressWrap) return;
        if (!blocks || !blocks.length) {
            els.progressWrap.innerHTML = '';
            return;
        }
        const rows = blocks.map(function (b) {
            const entry = doneMap[b.block_index] || {};
            const status = entry.status || '';
            let mark = '…';
            let cls = 'scenes-map-block--pending';
            if (status === 'ok') { mark = '✓'; cls = 'scenes-map-block--ok'; }
            else if (status === 'err') { mark = '✗'; cls = 'scenes-map-block--err'; }
            else if (status === 'run') { mark = '↻'; cls = 'scenes-map-block--run'; }
            const stats = entry.stats || {};
            const coverage = entry.coverage || {};
            const sceneCount = entry.scene_count || stats.scene_count || 0;
            const minChars = stats.min_chars != null ? stats.min_chars : null;
            const maxChars = stats.max_chars != null ? stats.max_chars : null;
            const visualLine = formatVisualCounts(stats.visual_source_counts);
            let meta = '';
            if (sceneCount && minChars != null && maxChars != null) {
                meta += ' <span class="scenes-map-block-lens">min ' + formatNumRu(minChars) + ' · max ' + formatNumRu(maxChars) + ' символов</span>';
            }
            if (visualLine) {
                meta += ' <span class="scenes-map-block-visuals">' + visualLine.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</span>';
            }
            meta += formatCoverageBadge(coverage);
            return '<div class="scenes-map-block-row ' + cls + '">' +
                '<span class="scenes-map-block-mark">' + mark + '</span> ' +
                '<span class="scenes-map-block-id">' + (b.block_id || '') + '</span> ' +
                '<span class="scenes-map-block-type">' + (b.macro_block_type || '') + '</span>' +
                (sceneCount ? ' <span class="scenes-map-block-scenes">' + sceneCount + ' сцен</span>' : '') +
                meta +
                '</div>';
        }).join('');
        els.progressWrap.innerHTML = renderProgressSummary(summary) +
            '<div class="scenes-map-block-progress">' + rows + '</div>' +
            renderCoverageFooter(summary);
    }

    function applyProgressReport(report) {
        if (!report || !report.ok) return;
        const doneMap = {};
        (report.block_results || []).forEach(function (row) {
            doneMap[row.block_index] = {
                status: row.ok === false ? 'err' : 'ok',
                scene_count: row.scene_count || (row.stats && row.stats.scene_count) || 0,
                stats: row.stats || {},
                coverage: row.coverage || {},
            };
        });
        fetch('/scenes-map/api/scenemap/prepare')
            .then(function (r) { return r.json(); })
            .then(function (prepData) {
                renderProgress((prepData && prepData.blocks) || [], doneMap, report.summary || null);
            })
            .catch(function () { /* ignore */ });
    }

    function loadSavedProgress() {
        fetch('/scenes-map/api/scenemap/progress')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok || !data.blocks_done) return;
                applyProgressReport(data);
            })
            .catch(function () { /* ignore */ });
    }

    function syncMacromapResultAs(text) {
        if (!els.inputResultTa) return;
        els.inputResultTa.value = text || '';
        setInputResultBadge();
    }

    document.addEventListener('click', function () {
        closeAllModePanels();
    });

    bindLockToggle(els.systemToggle, els.systemWrap, els.systemTa, function () {
        setBadgeYesNo(els.systemBadge, els.systemTa.value, 'System Prompt');
    }, { collapseClass: 'rewrite-stage-card--prompt-collapsed' });

    bindLockToggle(els.userToggle, els.userWrap, els.userTa, function () {
        setBadgeYesNo(els.userBadge, els.userTa.value, 'User Prompt');
    }, { collapseClass: 'rewrite-stage-card--stage-user-prompt-collapsed' });

    bindLockToggle(els.resultToggle, null, els.resultTa, function () {
        setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
        updateResultCounts();
    }, { alwaysVisible: true });

    const modelField = root.querySelector('[data-ssm-model-field]');
    if (modelField) bindModeDropdown(modelField);

    root.querySelectorAll('.rewrite-mode-dropdown-field').forEach(function (field) {
        if (field === modelField) return;
        bindModeDropdown(field);
    });
    bindElementsDropdown();

    if (els.model) {
        els.model.addEventListener('change', function () { scheduleSave(0); });
    }

    if (els.resultCopy && els.resultTa) {
        els.resultCopy.addEventListener('click', function () {
            const text = els.resultTa.value || '';
            if (!text.trim()) return;
            navigator.clipboard.writeText(text).catch(function () { /* ignore */ });
        });
    }

    if (els.resultTa) {
        els.resultTa.addEventListener('input', function () {
            updateResultCounts();
        });
        updateResultCounts();
    }

    document.addEventListener('scenes-map-macromap-result-as', function (e) {
        const detail = e && e.detail;
        syncMacromapResultAs(detail && detail.result_as != null ? detail.result_as : '');
    });

    const macroResultAsTa = document.querySelector('[data-sm-result-as]');
    if (macroResultAsTa) {
        syncMacromapResultAs(macroResultAsTa.value || '');
        macroResultAsTa.addEventListener('input', function () {
            syncMacromapResultAs(macroResultAsTa.value || '');
        });
    }

    if (els.runBtn) {
        els.runBtn.addEventListener('click', async function () {
            if (!API_OK || generating) return;
            let hadError = false;
            runStartedAt = Date.now();
            abortController = new AbortController();
            pushStageStatus('SceneMap Agent…');
            statusTimer = setInterval(function () {
                pushStageStatus('SceneMap Agent…');
            }, 1000);
            try {
                const macroResultAsTaLocal = document.querySelector('[data-sm-result-as]');
                const extra = {};
                if (macroResultAsTaLocal) extra.result_as = macroResultAsTaLocal.value || '';
                await savePrefs(extra);

                const prep = await fetch('/scenes-map/api/scenemap/prepare', { signal: abortController.signal });
                const prepData = await prep.json().catch(function () { return {}; });
                if (!prep.ok || !prepData.ok) {
                    throw new Error((prepData && prepData.error) || 'Не удалось подготовить macro_map');
                }

                const blocks = prepData.blocks || [];
                const doneMap = {};
                let progressSummary = null;
                renderProgress(blocks, doneMap, null);

                let finalResult = '';
                for (let i = 0; i < blocks.length; i++) {
                    if (abortController && abortController.signal.aborted) {
                        throw new DOMException('Aborted', 'AbortError');
                    }
                    const b = blocks[i];
                    doneMap[b.block_index] = { status: 'run' };
                    renderProgress(blocks, doneMap, progressSummary);
                    pushStageStatus('SceneMap: ' + (b.block_id || ('block ' + (i + 1))) + '…');

                    const r = await fetch('/scenes-map/api/generate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            agent: 'scenemap',
                            block_index: b.block_index,
                            reset: i === 0,
                        }),
                        signal: abortController.signal,
                    });
                    const data = await r.json().catch(function () { return {}; });
                    if (!r.ok || !data.ok) {
                        doneMap[b.block_index] = { status: 'err' };
                        renderProgress(blocks, doneMap, progressSummary);
                        showStageError(
                            (data && data.error) || ('Ошибка блока ' + (b.block_id || i)),
                            resolveStageErrorRaw(data)
                        );
                        hadError = true;
                        return;
                    }
                    doneMap[b.block_index] = {
                        status: 'ok',
                        scene_count: data.scene_count || 0,
                        stats: data.stats || {},
                        coverage: data.coverage || {},
                    };
                    if (data.progress) progressSummary = data.progress;
                    renderProgress(blocks, doneMap, progressSummary);
                    if (data.result) finalResult = data.result;
                }

                if (els.resultTa) {
                    els.resultTa.value = finalResult || els.resultTa.value || '';
                    setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
                    updateResultCounts();
                }
                if (API_OK) scheduleSave(0);
                clearStageStatus();
            } catch (e) {
                if (e && e.name === 'AbortError') {
                    clearStageStatus();
                    return;
                }
                hadError = true;
                showStageError(String(e.message || e), e && e.rawResponse ? e.rawResponse : '');
            } finally {
                if (statusTimer) {
                    clearInterval(statusTimer);
                    statusTimer = null;
                }
                abortController = null;
                if (!hadError && generating) {
                    clearStageStatus();
                }
            }
        });
    }

    if (els.cancelBtn) {
        els.cancelBtn.addEventListener('click', function () {
            if (abortController) abortController.abort();
        });
    }

    if (els.runBtn && typeof window.scenesMapDownloadExport === 'function') {
        const exportBtn = root.querySelector('[data-ssm-export]');
        if (exportBtn) {
            exportBtn.addEventListener('click', function () {
                window.scenesMapDownloadExport('scenemap', collectPayload, exportBtn);
            });
        }
    }

    setInputResultBadge();
    if (els.systemTa) {
        setBadgeYesNo(els.systemBadge, els.systemTa.value, 'System Prompt');
    }
    if (els.userTa) {
        setBadgeYesNo(els.userBadge, els.userTa.value, 'User Prompt');
    }
    loadSavedProgress();
    syncResultWrapIdleStatus();
})();
