(function () {
    'use strict';

    const API_OK = document.body.getAttribute('data-api-key') === '1';
    const root = document.querySelector('[data-overlay-text-agent="remotion-preview"]');
    if (!root) return;

    const els = {
        model: document.getElementById('rp-model'),
        systemToggle: root.querySelector('[data-rp-system-toggle]'),
        systemWrap: root.querySelector('[data-rp-system-wrap]'),
        systemTa: root.querySelector('[data-rp-system-prompt]'),
        systemBadge: root.querySelector('[data-rp-system-badge]'),
        userToggle: root.querySelector('[data-rp-user-toggle]'),
        userWrap: root.querySelector('[data-rp-user-wrap]'),
        userTa: root.querySelector('[data-rp-user-prompt]'),
        userBadge: root.querySelector('[data-rp-user-badge]'),
        resultToggle: root.querySelector('[data-rp-result-toggle]'),
        resultWrap: root.querySelector('[data-rp-result-wrap]'),
        resultTa: root.querySelector('[data-rp-result]'),
        resultBadge: root.querySelector('[data-rp-result-badge]'),
        resultCounts: root.querySelector('[data-rp-result-counts]'),
        resultCopy: root.querySelector('[data-rp-result-copy]'),
        exportBtn: root.querySelector('[data-rp-export]'),
        runBtn: root.querySelector('[data-rp-run]'),
        statusRow: root.querySelector('[data-rp-status-row]'),
        statusText: root.querySelector('[data-rp-status-text]'),
        cancelBtn: root.querySelector('[data-rp-cancel-btn]'),
        photoFile: root.querySelector('[data-rp-photo-file]'),
        photoPreview: root.querySelector('[data-rp-photo-preview]'),
        photoBadge: root.querySelector('[data-rp-photo-badge]'),
        photoDims: root.querySelector('[data-rp-photo-dims]'),
        dinoRunBtn: root.querySelector('[data-rp-dino-run]'),
        dinoLog: root.querySelector('[data-rp-dino-log]'),
        dinoResultToggle: root.querySelector('[data-rp-dino-result-toggle]'),
        dinoResultWrap: root.querySelector('[data-rp-dino-result-wrap]'),
        dinoResultTa: root.querySelector('[data-rp-dino-result]'),
        dinoBadge: root.querySelector('[data-rp-dino-badge]'),
        dinoResultCounts: root.querySelector('[data-rp-dino-result-counts]'),
        dinoResultCopy: root.querySelector('[data-rp-dino-result-copy]'),
        dinoDrawBtn: root.querySelector('[data-rp-dino-draw]'),
        dinoAnnotatedWrap: root.querySelector('[data-rp-dino-annotated-wrap]'),
        dinoAnnotatedPreview: root.querySelector('[data-rp-dino-annotated-preview]'),
        dinoCheckWrap: root.querySelector('[data-rp-dino-check]'),
    };

    let imageUrl = (els.photoPreview && els.photoPreview.getAttribute('src')) || '';
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

    function updateDinoResultCounts() {
        updateTextareaCounts(els.dinoResultTa, els.dinoResultCounts);
    }

    function syncDinoResultWrapStatus() {
        if (!els.dinoResultWrap || !els.dinoResultTa) return;
        els.dinoResultWrap.setAttribute('data-status', (els.dinoResultTa.value || '').trim() ? 'done' : 'pending');
    }

    function syncImageUrlFromPreview() {
        const src = (els.photoPreview && els.photoPreview.getAttribute('src')) || '';
        if (src && src.trim() && !/^data:/i.test(src.trim())) {
            imageUrl = src.trim();
        }
    }

    function formatPhotoDimsText(w, h) {
        const wi = Number(w);
        const hi = Number(h);
        if (!wi || !hi || wi < 1 || hi < 1) return '';
        return '"image_width": ' + Math.round(wi) + ',\n"image_height": ' + Math.round(hi);
    }

    function setPhotoDims(w, h) {
        if (!els.photoDims) return;
        const text = formatPhotoDimsText(w, h);
        if (!text) {
            els.photoDims.hidden = true;
            els.photoDims.textContent = '"image_width": —,\n"image_height": —';
            return;
        }
        els.photoDims.textContent = text;
        els.photoDims.hidden = false;
    }

    function readPhotoDimsFromPreview() {
        const img = els.photoPreview;
        if (!img || img.classList.contains('is-hidden')) {
            setPhotoDims(0, 0);
            return;
        }
        const src = (img.getAttribute('src') || '').trim();
        if (!src) {
            setPhotoDims(0, 0);
            return;
        }
        if (img.complete && img.naturalWidth > 0) {
            setPhotoDims(img.naturalWidth, img.naturalHeight);
            return;
        }
        img.addEventListener('load', function onLoad() {
            img.removeEventListener('load', onLoad);
            setPhotoDims(img.naturalWidth, img.naturalHeight);
        }, { once: true });
    }

    function readPhotoDimsFromDinoResult() {
        if (!els.dinoResultTa) return false;
        const data = parseResultJson(els.dinoResultTa.value);
        if (!data || typeof data !== 'object') return false;
        const w = data.image_width;
        const h = data.image_height;
        if (w && h) {
            setPhotoDims(w, h);
            return true;
        }
        return false;
    }

    function collectPayload(extra) {
        syncImageUrlFromPreview();
        const payload = {
            rp_model: els.model ? els.model.value : '',
            rp_system_prompt: els.systemTa ? els.systemTa.value : '',
            rp_user_prompt: els.userTa ? els.userTa.value : '',
            rp_result: els.resultTa ? els.resultTa.value : '',
            rp_image_url: imageUrl,
            rp_image_preview_url: imageUrl,
        };
        /* rp_dino_* только явно — иначе scheduleSave затирает Result from DINO пустой строкой */
        if (extra && typeof extra === 'object') {
            if (Object.prototype.hasOwnProperty.call(extra, 'rp_dino_result')) {
                payload.rp_dino_result = extra.rp_dino_result;
            }
            if (Object.prototype.hasOwnProperty.call(extra, 'rp_dino_annotated_url')) {
                payload.rp_dino_annotated_url = extra.rp_dino_annotated_url;
            }
            return Object.assign(payload, extra);
        }
        return payload;
    }

    function applyPrefsFromServer(prefs) {
        if (!prefs || typeof prefs !== 'object') return;
        if (els.model && prefs.rp_model) els.model.value = prefs.rp_model;
        if (els.systemTa && prefs.rp_system_prompt != null) els.systemTa.value = prefs.rp_system_prompt;
        if (els.userTa && prefs.rp_user_prompt != null) els.userTa.value = prefs.rp_user_prompt;
        if (els.resultTa && prefs.rp_result != null) els.resultTa.value = prefs.rp_result;
        if (prefs.rp_image_url || prefs.rp_image_preview_url) {
            imageUrl = prefs.rp_image_url || prefs.rp_image_preview_url || imageUrl;
            if (els.photoPreview && imageUrl) {
                els.photoPreview.src = imageUrl;
                els.photoPreview.classList.remove('is-hidden');
            }
        }
        if (els.dinoResultTa && prefs.rp_dino_result != null) {
            els.dinoResultTa.value = prefs.rp_dino_result;
            setBadgeYesNo(els.dinoBadge, prefs.rp_dino_result, 'Result from DINO');
            updateDinoResultCounts();
            syncDinoResultWrapStatus();
        }
        if (prefs.rp_dino_annotated_url) {
            showAnnotatedPreview(prefs.rp_dino_annotated_url);
        }
        if (!readPhotoDimsFromDinoResult()) readPhotoDimsFromPreview();
        renderDinoKeywordCheck();
    }

    function setDinoLog(lines) {
        if (!els.dinoLog) return;
        const text = Array.isArray(lines) ? lines.join('\n') : String(lines || '');
        els.dinoLog.textContent = text || '—';
        els.dinoLog.classList.toggle('overlay-text-dino__log--busy', /работает|Запрос/i.test(text));
    }

    function parseResultJson(raw) {
        const text = String(raw || '').trim();
        if (!text) return null;
        try {
            return JSON.parse(text);
        } catch (e1) {
            const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
            if (fence) {
                try {
                    return JSON.parse(fence[1].trim());
                } catch (e2) {
                    return null;
                }
            }
            const start = text.indexOf('{');
            const end = text.lastIndexOf('}');
            if (start >= 0 && end > start) {
                try {
                    return JSON.parse(text.slice(start, end + 1));
                } catch (e3) {
                    return null;
                }
            }
            return null;
        }
    }

    function parseDinoPromptFromResult() {
        const raw = els.resultTa ? (els.resultTa.value || '').trim() : '';
        if (!raw) return { prompt: '', error: 'Result пуст — сначала ↻ (должен быть JSON с dino_prompt).' };
        const data = parseResultJson(raw);
        if (!data || typeof data !== 'object') {
            return {
                prompt: '',
                error: 'Result должен быть JSON вида {"dino_prompt":"person. face. ..."}. Сейчас там обычный текст — обновите промты и нажмите ↻.',
            };
        }
        const p = data.dino_prompt || data.prompt;
        if (!p) return { prompt: '', error: 'В Result нет ключа dino_prompt.' };
        return { prompt: String(p), error: '' };
    }

    function normTerm(s) {
        return String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
    }

    function splitPromptTerms(prompt) {
        const raw = normTerm(prompt);
        if (!raw) return [];
        const parts = raw.split(/[.\n;,]+/).map(function (p) { return p.trim(); }).filter(Boolean);
        const seen = {};
        const out = [];
        parts.forEach(function (p) {
            const key = normTerm(p);
            if (key && !seen[key]) {
                seen[key] = true;
                out.push(key);
            }
        });
        return out;
    }

    function uniqueDinoLabels(detections) {
        const seen = {};
        const out = [];
        (detections || []).forEach(function (d) {
            if (!d || typeof d !== 'object') return;
            const lab = normTerm(d.label);
            if (!lab || seen[lab]) return;
            seen[lab] = true;
            out.push(lab);
        });
        return out;
    }

    function termMatchesLabel(term, label) {
        term = normTerm(term);
        label = normTerm(label);
        if (!term || !label) return false;
        if (term === label) return true;
        if (term.indexOf(label) >= 0 || label.indexOf(term) >= 0) return true;
        const termWords = term.split(/\s+/);
        const labelWords = label.split(/\s+/);
        if (termWords.length && termWords.every(function (w) { return label.indexOf(w) >= 0 || labelWords.indexOf(w) >= 0; })) {
            return true;
        }
        if (labelWords.length && labelWords.every(function (w) { return term.indexOf(w) >= 0 || termWords.indexOf(w) >= 0; })) {
            return true;
        }
        if (term.length > 2 && term.slice(-1) === 's' && term.slice(0, -1) === label) return true;
        if (label.length > 2 && label.slice(-1) === 's' && label.slice(0, -1) === term) return true;
        return false;
    }

    function computeDinoKeywordCheck() {
        const parsed = parseDinoPromptFromResult();
        if (parsed.error) {
            return { error: parsed.error };
        }
        const dinoData = parseResultJson(els.dinoResultTa ? els.dinoResultTa.value : '');
        if (!dinoData || typeof dinoData !== 'object') {
            return { error: 'Сначала получите Result from DINO (↻).' };
        }
        const detections = Array.isArray(dinoData.detections) ? dinoData.detections : [];
        const llmTerms = splitPromptTerms(parsed.prompt);
        const labels = uniqueDinoLabels(detections);
        const matched = [];
        const llmOnly = [];
        llmTerms.forEach(function (term) {
            if (labels.some(function (lab) { return termMatchesLabel(term, lab); })) {
                matched.push(term);
            } else {
                llmOnly.push(term);
            }
        });
        const dinoOnly = [];
        labels.forEach(function (lab) {
            if (!llmTerms.some(function (term) { return termMatchesLabel(term, lab); })) {
                dinoOnly.push(lab);
            }
        });
        return {
            ok: llmOnly.length === 0 && llmTerms.length > 0,
            dino_prompt: parsed.prompt,
            llm_term_count: llmTerms.length,
            dino_label_count: labels.length,
            matched: matched,
            llm_only: llmOnly,
            dino_only: dinoOnly,
        };
    }

    function escCheckText(s) {
        return String(s || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function renderCheckChips(items, cls) {
        if (!items || !items.length) {
            return '<span class="rewrite-scene-writer-check__wait">—</span>';
        }
        return items.map(function (t) {
            return '<span class="overlay-text-dino-check__chip ' + cls + '">' + escCheckText(t) + '</span>';
        }).join('');
    }

    function renderDinoKeywordCheck(external) {
        if (!els.dinoCheckWrap) return;
        const check = external || computeDinoKeywordCheck();
        if (check.error) {
            els.dinoCheckWrap.innerHTML =
                '<div class="rewrite-scene-writer-check__head"><strong>Проверка</strong> ' +
                '<span class="rewrite-scene-writer-check__wait">ожидание данных</span></div>' +
                '<div class="rewrite-scene-writer-check__hint">' + escCheckText(check.error) + '</div>';
            return;
        }
        const okClass = check.ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no';
        let html = '';
        html += '<div class="rewrite-scene-writer-check__head"><strong>Проверка</strong> ';
        html += '<span class="' + okClass + '">' + (check.ok ? 'OK' : 'NO') + '</span></div>';
        html += '<div class="rewrite-scene-writer-check__hint">Сверка <code>dino_prompt</code> (Result LLM) с <code>label</code> (DINO): ';
        html += formatNumRu(String(check.llm_term_count || 0)) + ' терм. · ';
        html += formatNumRu(String(check.dino_label_count || 0)) + ' label</div>';

        html += '<div class="overlay-text-dino-check__section">';
        html += '<div class="rewrite-scene-writer-check__hint"><span class="rewrite-scene-writer-check__ok">в LLM и DINO</span> (' +
            formatNumRu(String((check.matched || []).length)) + ')</div>';
        html += '<div class="overlay-text-dino-check__chips">' +
            renderCheckChips(check.matched, 'overlay-text-dino-check__chip--matched') + '</div></div>';

        html += '<div class="overlay-text-dino-check__section">';
        html += '<div class="rewrite-scene-writer-check__hint"><span class="rewrite-scene-writer-check__no">в LLM, DINO не нашёл</span> (' +
            formatNumRu(String((check.llm_only || []).length)) + ')</div>';
        html += '<div class="overlay-text-dino-check__chips">' +
            renderCheckChips(check.llm_only, 'overlay-text-dino-check__chip--llm-only') + '</div></div>';

        html += '<div class="overlay-text-dino-check__section">';
        html += '<div class="rewrite-scene-writer-check__hint"><span class="rewrite-scene-writer-check__wait">только в DINO</span> (' +
            formatNumRu(String((check.dino_only || []).length)) + ')</div>';
        html += '<div class="overlay-text-dino-check__chips">' +
            renderCheckChips(check.dino_only, 'overlay-text-dino-check__chip--dino-only') + '</div></div>';

        els.dinoCheckWrap.innerHTML = html;
    }

    function showAnnotatedPreview(url) {
        if (!els.dinoAnnotatedPreview || !url) return;
        els.dinoAnnotatedPreview.src = url;
        if (els.dinoAnnotatedWrap) els.dinoAnnotatedWrap.classList.remove('is-hidden');
    }

    async function runDinoDraw() {
        if (!API_OK || !els.dinoDrawBtn) return;
        syncImageUrlFromPreview();
        const dinoText = els.dinoResultTa ? (els.dinoResultTa.value || '').trim() : '';
        if (!dinoText) {
            alert('Сначала получите Result from DINO (↻).');
            return;
        }
        if (!imageUrl) {
            alert('Загрузите фото в Remotion Preview Agent.');
            return;
        }

        els.dinoDrawBtn.disabled = true;
        setDinoLog(['Обрисовка элементов…', 'image: ' + imageUrl]);

        try {
            const r = await fetch('/overlay-text/api/remotion-preview/dino-draw', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    image_url: imageUrl,
                    rp_dino_result: dinoText,
                }),
            });
            let data = {};
            try {
                data = await r.json();
            } catch (parseErr) {
                data = {};
            }
            if (!r.ok || !data.ok) {
                const errMsg = (data && data.error)
                    || (r.status === 404 ? 'Маршрут dino-draw не найден — перезапустите Flask (run_server.py).' : '')
                    || ('HTTP ' + r.status);
                setDinoLog((data.log || []).concat([errMsg]));
                alert(errMsg);
                return;
            }
            const url = data.image_url || data.image_preview_url || '';
            showAnnotatedPreview(url);
            setDinoLog(data.log || ['Готово — превью ниже.']);
            if (data.prefs) applyPrefsFromServer(data.prefs);
            else await savePrefs({ rp_dino_annotated_url: url, rp_dino_result: dinoText });
        } catch (e) {
            setDinoLog(['Ошибка: ' + String(e.message || e)]);
            alert(String(e.message || e));
        } finally {
            els.dinoDrawBtn.disabled = !API_OK;
        }
    }

    async function runGroundingDino() {
        if (!API_OK || !els.dinoRunBtn) return;
        syncImageUrlFromPreview();
        const parsed = parseDinoPromptFromResult();
        if (parsed.error) {
            alert(parsed.error);
            return;
        }
        if (!imageUrl) {
            alert('Загрузите фото в Remotion Preview Agent.');
            return;
        }

        if (els.dinoRunBtn) els.dinoRunBtn.disabled = true;
        if (els.dinoResultTa) els.dinoResultTa.classList.add('rewrite-stage-result--busy');
        if (els.dinoResultWrap) els.dinoResultWrap.setAttribute('data-status', 'generating');
        setDinoLog([
            'Grounding DINO работает…',
            'источник: Result → ключ dino_prompt',
            'dino_prompt: ' + parsed.prompt,
            'image: ' + imageUrl,
        ]);

        try {
            const r = await fetch('/overlay-text/api/remotion-preview/grounding-dino', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    image_url: imageUrl,
                    rp_result: els.resultTa ? els.resultTa.value : '',
                    box_threshold: 0.25,
                    text_threshold: 0.25,
                }),
            });
            const data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok) {
                setDinoLog((data.log || []).concat([data.error || 'HTTP ' + r.status]));
                alert((data && data.error) || 'Grounding DINO failed');
                return;
            }
            const text = data.result_text || JSON.stringify(data.result, null, 2);
            if (els.dinoResultTa) els.dinoResultTa.value = text;
            setBadgeYesNo(els.dinoBadge, text, 'Result from DINO');
            updateDinoResultCounts();
            syncDinoResultWrapStatus();
            setDinoLog(data.log || ['Готово.']);
            if (data.result && data.result.image_width && data.result.image_height) {
                setPhotoDims(data.result.image_width, data.result.image_height);
            }
            if (data.prefs) applyPrefsFromServer(data.prefs);
            else await savePrefs({ rp_dino_result: text });
            renderDinoKeywordCheck(data.keyword_check || null);
        } catch (e) {
            setDinoLog(['Ошибка: ' + String(e.message || e)]);
            alert(String(e.message || e));
        } finally {
            if (els.dinoRunBtn) els.dinoRunBtn.disabled = !API_OK;
            if (els.dinoResultTa) els.dinoResultTa.classList.remove('rewrite-stage-result--busy');
            syncDinoResultWrapStatus();
        }
    }

    function scheduleSave(delayMs) {
        if (!API_OK) return;
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
            saveTimer = null;
            savePrefs().catch(function () { /* ignore */ });
        }, delayMs == null ? 400 : delayMs);
    }

    async function postRemotionPrefs(body) {
        let r = await fetch('/overlay-text/api/remotion-preview/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (r.status === 404) {
            r = await fetch('/overlay-text/api/prefs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        }
        return r;
    }

    async function savePrefs(extra) {
        const r = await postRemotionPrefs(collectPayload(extra));
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            throw new Error((data && data.error) || 'Ошибка сохранения (HTTP ' + r.status + ')');
        }
        if (data.prefs) applyPrefsFromServer(data.prefs);
        return data;
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

    function pushStageStatus(msg) {
        const elapsed = runStartedAt > 0
            ? Math.max(0, Math.floor((Date.now() - runStartedAt) / 1000))
            : 0;
        setStageStatus((msg || 'Выполнение…') + ' (' + elapsed + 's)', { running: true });
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
        if (els.resultWrap) els.resultWrap.setAttribute('data-status', 'error');
        if (els.resultTa) {
            const err = String(text || '').trim();
            const raw = String(rawResponse || '').trim();
            els.resultTa.value = raw ? raw + '\n\n--- Ошибка ---\n' + err : 'Ошибка: ' + err;
            els.resultTa.classList.remove('rewrite-stage-result--busy');
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
        document.querySelectorAll('[data-overlay-text-agent="remotion-preview"] .rewrite-anim-dropdown__panel').forEach(function (p) {
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

    async function uploadPhoto(file) {
        const fd = new FormData();
        fd.append('image', file);
        const r = await fetch('/overlay-text/api/upload', { method: 'POST', body: fd });
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok || !data.image_url) {
            throw new Error((data && data.error) || 'Ошибка загрузки');
        }
        imageUrl = data.image_url;
        if (els.photoPreview) {
            els.photoPreview.src = data.image_url;
            els.photoPreview.classList.remove('is-hidden');
        }
        setBadgeYesNo(els.photoBadge, imageUrl, 'Photo');
        await savePrefs({ rp_image_url: imageUrl, rp_image_preview_url: imageUrl });
        readPhotoDimsFromPreview();
    }

    async function downloadExport(btn) {
        if (btn) btn.disabled = true;
        try {
            if (saveTimer) {
                clearTimeout(saveTimer);
                saveTimer = null;
            }
            await savePrefs();
            const r = await fetch('/overlay-text/api/remotion-preview/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(collectPayload()),
            });
            if (!r.ok) {
                const raw = await r.text();
                let data = {};
                try {
                    data = raw ? JSON.parse(raw) : {};
                } catch (_e) {
                    data = {};
                }
                throw new Error(
                    (data && data.error)
                    || (raw && raw.length < 400 ? raw : '')
                    || ('HTTP ' + r.status)
                );
            }
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'overlay_text_remotion_preview_request.json';
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
        if (saveTimer) {
            clearTimeout(saveTimer);
            saveTimer = null;
        }
        try {
            await savePrefs();
        } catch (e) {
            /* prefs всё равно уходят в POST /generate; не блокируем запуск */
            console.warn('Remotion prefs save:', e);
        }
        abortController = new AbortController();
        runStartedAt = Date.now();
        setStageStatus('Запрос к модели…', { running: true });
        statusTimer = window.setInterval(function () {
            pushStageStatus('Запрос к модели…');
        }, 1000);

        try {
            const r = await fetch('/overlay-text/api/remotion-preview/generate', {
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
            if (data.agent && data.agent !== 'remotion_preview') {
                showStageError('Ответ не от Remotion Preview Agent (agent=' + data.agent + ')', '');
                return;
            }
            if (els.resultTa) els.resultTa.value = data.rp_result || data.result || '';
            setBadgeYesNo(els.resultBadge, els.resultTa && els.resultTa.value, 'Result');
            updateResultCounts();
            renderDinoKeywordCheck();
            clearStageStatus();
            syncResultWrapIdleStatus();
        } catch (e) {
            if (e && e.name === 'AbortError') {
                clearStageStatus();
                return;
            }
            showStageError(String(e.message || e), '');
        }
    }

    document.addEventListener('click', closeAllModePanels);

    bindModeDropdown(root.querySelector('[data-rp-model-field]'));

    bindLockToggle(els.systemToggle, els.systemWrap, els.systemTa, function () {
        setBadgeYesNo(els.systemBadge, els.systemTa.value, 'System Prompt');
    }, { alwaysVisible: true });

    bindLockToggle(els.userToggle, els.userWrap, els.userTa, function () {
        setBadgeYesNo(els.userBadge, els.userTa.value, 'User Prompt');
    }, { alwaysVisible: true });

    bindLockToggle(els.resultToggle, null, els.resultTa, function () {
        setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
        updateResultCounts();
    }, { alwaysVisible: true });

    bindLockToggle(els.dinoResultToggle, null, els.dinoResultTa, function () {
        setBadgeYesNo(els.dinoBadge, els.dinoResultTa.value, 'Result from DINO');
        updateDinoResultCounts();
        syncDinoResultWrapStatus();
        renderDinoKeywordCheck();
        savePrefs({
            rp_dino_result: els.dinoResultTa ? els.dinoResultTa.value : '',
        }).catch(function () { /* ignore */ });
    }, { alwaysVisible: true });

    els.photoFile?.addEventListener('change', function () {
        const file = els.photoFile.files && els.photoFile.files[0];
        if (!file) return;
        uploadPhoto(file).catch(function (e) {
            alert(String(e.message || e));
        });
    });

    els.exportBtn?.addEventListener('click', function () {
        downloadExport(els.exportBtn);
    });

    els.runBtn?.addEventListener('click', function () {
        runAgent();
    });

    els.cancelBtn?.addEventListener('click', function () {
        if (abortController) abortController.abort();
        clearStageStatus();
    });

    els.resultCopy?.addEventListener('click', function () {
        if (!els.resultTa) return;
        const text = els.resultTa.value || '';
        if (!text.trim()) return;
        navigator.clipboard.writeText(text).catch(function () { /* ignore */ });
    });

    els.dinoRunBtn?.addEventListener('click', function () {
        runGroundingDino();
    });

    els.dinoDrawBtn?.addEventListener('click', function () {
        runDinoDraw();
    });

    els.dinoResultCopy?.addEventListener('click', function () {
        if (!els.dinoResultTa) return;
        const text = els.dinoResultTa.value || '';
        if (!text.trim()) return;
        navigator.clipboard.writeText(text).catch(function () { /* ignore */ });
    });

    setBadgeYesNo(els.photoBadge, imageUrl, 'Photo');
    if (!readPhotoDimsFromDinoResult()) readPhotoDimsFromPreview();
    if (els.photoPreview) {
        els.photoPreview.addEventListener('load', readPhotoDimsFromPreview);
    }
    setBadgeYesNo(els.systemBadge, els.systemTa && els.systemTa.value, 'System Prompt');
    setBadgeYesNo(els.userBadge, els.userTa && els.userTa.value, 'User Prompt');
    setBadgeYesNo(els.resultBadge, els.resultTa && els.resultTa.value, 'Result');
    if (els.dinoResultTa) setBadgeYesNo(els.dinoBadge, els.dinoResultTa.value, 'Result from DINO');
    updateDinoResultCounts();
    syncDinoResultWrapStatus();
    updateResultCounts();
    renderDinoKeywordCheck();
    clearStageStatus();
})();
