(function () {
    'use strict';

    const API_OK = document.body.getAttribute('data-api-key') === '1';
    const root = document.querySelector('[data-overlay-text-agent="main2"]');
    if (!root) return;

    const els = {
        model: document.getElementById('ot2-model'),
        systemToggle: root.querySelector('[data-ot2-system-toggle]'),
        systemWrap: root.querySelector('[data-ot2-system-wrap]'),
        systemTa: root.querySelector('[data-ot2-system-prompt]'),
        systemBadge: root.querySelector('[data-ot2-system-badge]'),
        userToggle: root.querySelector('[data-ot2-user-toggle]'),
        userWrap: root.querySelector('[data-ot2-user-wrap]'),
        userTa: root.querySelector('[data-ot2-user-prompt]'),
        userBadge: root.querySelector('[data-ot2-user-badge]'),
        textToggle: root.querySelector('[data-ot2-text-toggle]'),
        textTa: root.querySelector('[data-ot2-text]'),
        textBadge: root.querySelector('[data-ot2-text-badge]'),
        styleToggle: root.querySelector('[data-ot2-style-toggle]'),
        styleTa: root.querySelector('[data-ot2-style]'),
        styleBadge: root.querySelector('[data-ot2-style-badge]'),
        durationInput: root.querySelector('[data-ot2-duration]'),
        resultToggle: root.querySelector('[data-ot2-result-toggle]'),
        resultWrap: root.querySelector('[data-ot2-result-wrap]'),
        resultTa: root.querySelector('[data-ot2-result]'),
        resultBadge: root.querySelector('[data-ot2-result-badge]'),
        resultCounts: root.querySelector('[data-ot2-result-counts]'),
        resultCopy: root.querySelector('[data-ot2-result-copy]'),
        exportBtn: root.querySelector('[data-ot2-export]'),
        runBtn: root.querySelector('[data-ot2-run]'),
        statusRow: root.querySelector('[data-ot2-status-row]'),
        statusText: root.querySelector('[data-ot2-status-text]'),
        cancelBtn: root.querySelector('[data-ot2-cancel-btn]'),
        photoPreview: root.querySelector('[data-ot2-photo-preview]'),
        photoBadge: root.querySelector('[data-ot2-photo-badge]'),
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

    function updateTextareaCounts(ta, countsEl) {
        if (!countsEl || !ta) return;
        const text = ta.value || '';
        const words = text.trim() ? text.trim().split(/\s+/).length : 0;
        countsEl.textContent = text.trim()
            ? formatNumRu(String(text.length)) + ' симв. · ' + formatNumRu(String(words)) + ' сл.'
            : '';
    }

    function updateResultCounts() {
        updateTextareaCounts(els.resultTa, els.resultCounts);
    }

    function getRpImageUrl() {
        const rpImg = document.querySelector('[data-rp-photo-preview]');
        const src = rpImg ? (rpImg.getAttribute('src') || rpImg.src || '') : '';
        return String(src || '').trim();
    }

    function refreshPreview2() {
        if (window.OverlayTextPreview2 && typeof window.OverlayTextPreview2.refresh === 'function') {
            window.OverlayTextPreview2.refresh();
        }
    }

    function syncRpPhotoPreview() {
        const url = getRpImageUrl();
        if (els.photoPreview) {
            if (url) {
                els.photoPreview.src = url;
                els.photoPreview.classList.remove('is-hidden');
            } else {
                els.photoPreview.classList.add('is-hidden');
                els.photoPreview.removeAttribute('src');
            }
        }
        setBadgeYesNo(els.photoBadge, url, 'Photo');
        refreshPreview2();
    }

    function collectPayload(extra) {
        return Object.assign({
            ot2_model: els.model ? els.model.value : '',
            ot2_system_prompt: els.systemTa ? els.systemTa.value : '',
            ot2_user_prompt: els.userTa ? els.userTa.value : '',
            ot2_text: els.textTa ? els.textTa.value : '',
            ot2_style: els.styleTa ? els.styleTa.value : '',
            ot2_duration_sec: els.durationInput ? els.durationInput.value : '',
            ot2_result: els.resultTa ? els.resultTa.value : '',
        }, extra || {});
    }

    async function savePrefs(extra) {
        const r = await fetch('/overlay-text/api/agent2/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(collectPayload(extra)),
        });
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            throw new Error((data && data.error) || 'Ошибка сохранения');
        }
        return data;
    }

    function scheduleSave(delayMs) {
        if (!API_OK) return;
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
            saveTimer = null;
            savePrefs().catch(function () { /* ignore */ });
        }, delayMs == null ? 400 : delayMs);
    }

    function syncResultWrapIdleStatus() {
        if (!els.resultWrap || !els.resultTa) return;
        els.resultWrap.setAttribute('data-status', (els.resultTa.value || '').trim() ? 'done' : 'pending');
    }

    function clearStageStatus() {
        if (statusTimer) {
            clearInterval(statusTimer);
            statusTimer = null;
        }
        runStartedAt = 0;
        abortController = null;
        generating = false;
        root.classList.remove('rewrite-stage-card--running', 'rewrite-stage-card--error');
        if (els.statusRow) {
            els.statusRow.classList.add('hidden');
            els.statusRow.hidden = true;
        }
        if (els.statusText) els.statusText.textContent = '';
        if (els.cancelBtn) {
            els.cancelBtn.classList.add('hidden');
            els.cancelBtn.disabled = true;
        }
        if (els.resultTa) {
            els.resultTa.classList.remove('rewrite-stage-result--busy', 'rewrite-stage-result--error');
        }
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
            if (running && !isError) els.resultWrap.setAttribute('data-status', 'generating');
            else if (isError) els.resultWrap.setAttribute('data-status', 'error');
            else syncResultWrapIdleStatus();
        }
        if (els.resultTa) {
            els.resultTa.classList.toggle('rewrite-stage-result--busy', running && !isError);
            els.resultTa.classList.toggle('rewrite-stage-result--error', isError);
        }
        generating = running;
        if (els.runBtn) els.runBtn.disabled = running || !API_OK;
    }

    function showStageError(text, rawResponse) {
        clearStageStatus();
        root.classList.add('rewrite-stage-card--error');
        if (els.resultWrap) els.resultWrap.setAttribute('data-status', 'error');
        if (els.resultTa) {
            const err = String(text || '').trim();
            const raw = String(rawResponse || '').trim();
            els.resultTa.value = raw ? raw + '\n\n--- Ошибка ---\n' + err : 'Ошибка: ' + err;
            els.resultTa.classList.add('rewrite-stage-result--error');
        }
        updateResultCounts();
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
            if (collapseClass) root.classList.toggle(collapseClass, locked);
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

    function closeAllModePanels() {
        root.querySelectorAll('.rewrite-anim-dropdown__panel').forEach(function (p) {
            p.hidden = true;
            const btn = p.parentElement && p.parentElement.querySelector('.rewrite-anim-dropdown__btn');
            if (btn) btn.setAttribute('aria-expanded', 'false');
        });
    }

    function bindModeDropdown(field) {
        if (!field) return;
        const select = field.querySelector('.rewrite-mode-dropdown-select-hidden');
        const btn = field.querySelector('.rewrite-anim-dropdown__btn');
        const panel = field.querySelector('.rewrite-anim-dropdown__panel');
        const label = field.querySelector('[data-rewrite-mode-label]');
        const radioName = field.querySelector('.rewrite-mode-dropdown-radio')?.name;
        if (!select || !btn || !panel || !radioName) return;

        function syncFromSelect() {
            const val = select.value;
            field.querySelectorAll('.rewrite-mode-dropdown-radio').forEach(function (rb) {
                rb.checked = rb.value === val;
            });
            const opt = field.querySelector('.rewrite-mode-dropdown-radio[value="' + CSS.escape(val) + '"]');
            const title = opt && opt.closest('.rewrite-anim-option')?.querySelector('.rewrite-anim-option__title');
            if (label && title) label.textContent = title.textContent.trim();
        }

        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (btn.disabled) return;
            const open = panel.hidden;
            closeAllModePanels();
            panel.hidden = !open;
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        });

        panel.addEventListener('click', function (e) {
            e.stopPropagation();
            const rb = e.target.closest('.rewrite-mode-dropdown-radio');
            if (!rb || rb.disabled) return;
            select.value = rb.value;
            syncFromSelect();
            panel.hidden = true;
            btn.setAttribute('aria-expanded', 'false');
            scheduleSave(0);
        });

        select.addEventListener('change', syncFromSelect);
        syncFromSelect();
    }

    async function downloadExport(btn) {
        if (btn) btn.disabled = true;
        try {
            await savePrefs();
            const r = await fetch('/overlay-text/api/agent2/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(collectPayload()),
            });
            if (!r.ok) {
                const data = await r.json().catch(function () { return {}; });
                throw new Error((data && data.error) || 'export_failed');
            }
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'overlay_text_agent2_request.json';
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (e) {
            alert(String(e.message || e));
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    async function runAgent() {
        if (!API_OK || generating) return;
        syncRpPhotoPreview();
        if (!getRpImageUrl()) {
            alert('Загрузите фото в Remotion Preview Agent.');
            return;
        }
        try {
            await savePrefs();
        } catch (e) {
            alert(String(e.message || e));
            return;
        }
        abortController = new AbortController();
        runStartedAt = Date.now();
        setStageStatus('Запрос к модели…', { running: true });
        statusTimer = window.setInterval(function () {
            const elapsed = Math.max(0, Math.floor((Date.now() - runStartedAt) / 1000));
            setStageStatus('Запрос к модели… (' + elapsed + 's)', { running: true });
        }, 1000);

        try {
            const r = await fetch('/overlay-text/api/agent2/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(collectPayload()),
                signal: abortController.signal,
            });
            const data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok) {
                showStageError((data && data.error) || 'generation_failed', data && data.raw);
                return;
            }
            if (els.resultTa) els.resultTa.value = data.result || data.ot2_result || '';
            setBadgeYesNo(els.resultBadge, els.resultTa && els.resultTa.value, 'Result');
            updateResultCounts();
            clearStageStatus();
            refreshPreview2();
        } catch (e) {
            if (e && e.name === 'AbortError') {
                clearStageStatus();
                return;
            }
            showStageError(String(e.message || e), '');
        }
    }

    document.addEventListener('click', closeAllModePanels);

    bindModeDropdown(root.querySelector('[data-ot2-model-field]'));

    bindLockToggle(els.systemToggle, els.systemWrap, els.systemTa, function () {
        setBadgeYesNo(els.systemBadge, els.systemTa.value, 'System Prompt');
    }, { collapseClass: 'rewrite-stage-card--prompt-collapsed' });

    bindLockToggle(els.userToggle, els.userWrap, els.userTa, function () {
        setBadgeYesNo(els.userBadge, els.userTa.value, 'User Prompt');
    }, { collapseClass: 'rewrite-stage-card--stage-user-prompt-collapsed' });

    bindLockToggle(els.textToggle, null, els.textTa, function () {
        setBadgeYesNo(els.textBadge, els.textTa.value, 'Text');
    }, { alwaysVisible: true });

    bindLockToggle(els.styleToggle, null, els.styleTa, function () {
        setBadgeYesNo(els.styleBadge, els.styleTa.value, 'Style');
    }, { alwaysVisible: true });

    bindLockToggle(els.resultToggle, null, els.resultTa, function () {
        setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
        refreshPreview2();
    }, { alwaysVisible: true });

    [els.textTa, els.styleTa, els.durationInput].forEach(function (el) {
        if (!el) return;
        el.addEventListener('input', function () { scheduleSave(); });
        el.addEventListener('change', function () { scheduleSave(0); });
        el.addEventListener('blur', function () { scheduleSave(0); });
    });

    if (els.resultTa) {
        els.resultTa.addEventListener('input', refreshPreview2);
        els.resultTa.addEventListener('change', refreshPreview2);
    }

    els.runBtn?.addEventListener('click', runAgent);
    els.exportBtn?.addEventListener('click', function () { downloadExport(els.exportBtn); });
    els.cancelBtn?.addEventListener('click', function () {
        if (abortController) abortController.abort();
        clearStageStatus();
    });
    els.resultCopy?.addEventListener('click', function () {
        if (!els.resultTa || !els.resultTa.value.trim()) return;
        navigator.clipboard.writeText(els.resultTa.value).catch(function () { /* ignore */ });
    });

    window.addEventListener('overlay-rp-photo-updated', syncRpPhotoPreview);

    syncRpPhotoPreview();
    setBadgeYesNo(els.systemBadge, els.systemTa && els.systemTa.value, 'System Prompt');
    setBadgeYesNo(els.userBadge, els.userTa && els.userTa.value, 'User Prompt');
    setBadgeYesNo(els.textBadge, els.textTa && els.textTa.value, 'Text');
    setBadgeYesNo(els.styleBadge, els.styleTa && els.styleTa.value, 'Style');
    setBadgeYesNo(els.resultBadge, els.resultTa && els.resultTa.value, 'Result');
    updateResultCounts();
    clearStageStatus();
    refreshPreview2();
})();
