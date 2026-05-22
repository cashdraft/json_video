(function () {
    'use strict';

    const KEY_ANY = document.body.getAttribute('data-claude-key') === '1';
    const KEY_KIE = document.body.getAttribute('data-kie-key') === '1';
    const KEY_OPENAI = document.body.getAttribute('data-openai-key') === '1';
    const GPT_MODEL_ID = 'gpt-5.4';

    function isOpenaiLaterModel(modelId) {
        return (modelId || '').trim() === GPT_MODEL_ID;
    }

    function modelKeyOk(modelId) {
        if (isOpenaiLaterModel(modelId)) return KEY_OPENAI;
        return KEY_KIE;
    }

    function updateLaterKeyHints(wrap) {
        const modelEl = wrap.querySelector('[data-later-model]');
        const mid = modelEl ? modelEl.value : '';
        const hintKie = wrap.querySelector('[data-later-hint-kie]');
        const hintOpenai = wrap.querySelector('[data-later-hint-openai]');
        if (hintKie) {
            hintKie.classList.toggle('is-hidden', KEY_KIE || isOpenaiLaterModel(mid));
        }
        if (hintOpenai) {
            hintOpenai.classList.toggle('is-hidden', KEY_OPENAI || !isOpenaiLaterModel(mid));
        }
    }

    function syncLaterPanelEnabled(wrap) {
        const panel = wrap.querySelector('.later-lab__panel');
        const modelEl = wrap.querySelector('[data-later-model]');
        if (!panel) return;
        const mid = modelEl ? modelEl.value : '';
        const ok = KEY_ANY && modelKeyOk(mid);
        panel.classList.toggle('later-lab--disabled', !ok);
        updateLaterKeyHints(wrap);
    }

    const statusTimers = new WeakMap();

    function stopStatusTimer(wrap) {
        const t = statusTimers.get(wrap);
        if (t) {
            clearInterval(t.id);
            statusTimers.delete(wrap);
        }
    }

    function startStatusTimer(wrap, detailText) {
        stopStatusTimer(wrap);
        const timerEl = wrap.querySelector('[data-later-status-timer]');
        const detailEl = wrap.querySelector('[data-later-status-detail]');
        const started = Date.now();
        if (detailEl) {
            detailEl.textContent = detailText || '';
            detailEl.classList.toggle('is-hidden', !detailText);
        }
        if (timerEl) {
            timerEl.classList.remove('is-hidden');
            timerEl.textContent = '0 с';
        }
        const id = setInterval(function () {
            const sec = Math.floor((Date.now() - started) / 1000);
            if (timerEl) {
                timerEl.textContent = sec + ' с';
            }
        }, 250);
        statusTimers.set(wrap, { id: id, started: started });
    }

    function setLaterStatus(wrap, text, state, opts) {
        const box = wrap.querySelector('[data-later-status]');
        const label = wrap.querySelector('[data-later-status-text]');
        if (!box || !label) return;
        const st = state || 'pending';
        box.setAttribute('data-status', st);
        label.textContent = text || '';
        label.classList.toggle('slot-status-with-spinner', st === 'generating');
        if (st === 'generating') {
            box.classList.add('later-lab__status--active');
            startStatusTimer(wrap, (opts && opts.detail) || '');
        } else {
            box.classList.remove('later-lab__status--active');
            stopStatusTimer(wrap);
            const detailEl = wrap.querySelector('[data-later-status-detail]');
            const timerEl = wrap.querySelector('[data-later-status-timer]');
            if (detailEl) detailEl.classList.add('is-hidden');
            if (timerEl) timerEl.classList.add('is-hidden');
        }
    }

    function currentImageUrl(wrap) {
        const preview = wrap.querySelector('[data-later-preview]');
        const src = (preview && preview.getAttribute('src')) || '';
        return src.trim();
    }

    function setRawAnswer(wrap, text, opts) {
        const out = wrap.querySelector('[data-later-raw-out]');
        if (!out) return;
        if (opts && opts.onlyIfEmpty && (out.value || '').trim()) return;
        out.value = text || '';
    }

    function getRawAnswerText(wrap) {
        const out = wrap.querySelector('[data-later-raw-out]');
        return out ? (out.value || '').trim() : '';
    }

    async function runReparseFromRaw(wrap) {
        const text = getRawAnswerText(wrap);
        if (!text) {
            setLaterStatus(wrap, 'Вставьте или отредактируйте ответ в поле выше', 'error');
            return;
        }
        const reparseBtn = wrap.querySelector('[data-later-reparse]');
        if (reparseBtn) reparseBtn.disabled = true;
        setLaterStatus(wrap, 'Проверка и сборка…', 'generating', {
            detail: 'Разбор SVG / JSON / NOTES и валидация на сервере…',
        });
        try {
            await parseOnServer(wrap, text);
        } catch (e) {
            setLaterStatus(wrap, String(e.message || e), 'error');
        } finally {
            if (reparseBtn) reparseBtn.disabled = false;
        }
    }

    let renderPollTimer = null;

    function stopRenderPoll() {
        if (renderPollTimer) {
            clearInterval(renderPollTimer);
            renderPollTimer = null;
        }
    }

    function setRemotionPanelVisible(wrap, visible) {
        const panel = wrap.querySelector('[data-later-remotion]');
        if (panel) panel.classList.toggle('is-hidden', !visible);
    }

    function setPropsHint(wrap, text, ok) {
        const hint = wrap.querySelector('[data-later-props-hint]');
        if (!hint) return;
        const t = (text || '').trim();
        hint.classList.toggle('is-hidden', !t);
        hint.className = 'later-remotion__props-hint' + (t ? (ok ? ' later-remotion__props-hint--ok' : ' later-remotion__props-hint--err') : ' is-hidden');
        hint.textContent = t;
    }

    function syncLaterStatusFromRender(wrap, st) {
        const state = (st && st.state) || '';
        if (state === 'done') {
            setLaterStatus(wrap, (st && st.message) || 'Рендер MP4 завершён', 'done');
            return;
        }
        if (state === 'error') {
            setLaterStatus(wrap, (st && st.message) || 'Ошибка рендера', 'error');
            return;
        }
        if (state === 'cancelled') {
            setLaterStatus(wrap, (st && st.message) || 'Рендер остановлен', 'error');
            return;
        }
        if (state === 'queued' || state === 'running' || state === 'stuck') {
            const pct = st && st.progress_pct != null ? Number(st.progress_pct) : 0;
            let detail = 'Remotion LaterInfographic — может занять несколько минут';
            if (st.frames_done != null && st.frames_total != null) {
                detail += ' · кадры ' + st.frames_done + '/' + st.frames_total;
            }
            setLaterStatus(wrap, 'Рендер MP4… ' + pct + '%', 'generating', { detail: detail });
        }
    }

    function setMp4Preview(wrap, url, show) {
        const box = wrap.querySelector('[data-later-mp4-preview]');
        const video = wrap.querySelector('[data-later-mp4-video]');
        if (!box || !video) return;
        if (!show || !url) {
            box.classList.add('is-hidden');
            video.removeAttribute('src');
            video.load();
            return;
        }
        const bust = url + (url.indexOf('?') >= 0 ? '&' : '?') + 't=' + Date.now();
        if (video.getAttribute('src') !== bust) {
            video.setAttribute('src', bust);
            video.load();
        }
        box.classList.remove('is-hidden');
    }

    function updateRenderUi(wrap, st) {
        const statusEl = wrap.querySelector('[data-later-render-status]');
        const progWrap = wrap.querySelector('[data-later-render-progress]');
        const bar = wrap.querySelector('[data-later-render-bar]');
        const mp4Btn = wrap.querySelector('[data-later-mp4-open]');
        const renderBtn = wrap.querySelector('[data-later-render-start]');
        if (!statusEl) return;

        const state = (st && st.state) || 'idle';
        const pct = st && st.progress_pct != null ? Number(st.progress_pct) : 0;
        let msg = (st && st.message) || '';
        if (st && st.error_detail && (state === 'error' || state === 'stuck')) {
            msg += '\n' + st.error_detail;
        }
        if (st && st.stuck_reason && state === 'stuck') {
            msg += '\n' + st.stuck_reason;
        }
        statusEl.textContent = msg || state;
        statusEl.className = 'later-remotion__status';
        if (state === 'done' || state === 'props_ok') {
            statusEl.classList.add('later-remotion__status--ok');
        } else if (state === 'error' || state === 'cancelled') {
            statusEl.classList.add('later-remotion__status--err');
        } else if (state === 'queued' || state === 'running' || state === 'stuck') {
            statusEl.classList.add('later-remotion__status--run');
        }

        const active = state === 'queued' || state === 'running' || state === 'stuck';
        if (progWrap) progWrap.classList.toggle('is-hidden', !active);
        if (bar) bar.style.width = Math.min(100, Math.max(0, pct)) + '%';
        if (renderBtn) renderBtn.disabled = active;

        const mp4Url = (st && (st.output_url || st.mp4_url)) || '';
        if (mp4Btn) {
            if (mp4Url) {
                mp4Btn.dataset.mp4Url = mp4Url;
                mp4Btn.classList.remove('is-hidden');
                mp4Btn.disabled = false;
            } else {
                mp4Btn.classList.add('is-hidden');
                delete mp4Btn.dataset.mp4Url;
            }
        }
        if (state === 'done' && mp4Url) {
            setMp4Preview(wrap, mp4Url, true);
        } else if (state === 'queued' || state === 'running') {
            setMp4Preview(wrap, '', false);
        }
    }

    async function refreshRemotionInfo(wrap) {
        try {
            const r = await fetch('/scenes-lab/api/remotion/info');
            const data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok) return;
            if (data.props_ready && data.message) {
                setPropsHint(wrap, data.message, true);
            } else if (data.props_ready) {
                const tc = data.tracks_count != null ? data.tracks_count : '?';
                const fr = data.duration_frames != null ? data.duration_frames : '?';
                setPropsHint(wrap, 'props.json на диске (' + tc + ' треков, ' + fr + ' кадров).', true);
            } else {
                setPropsHint(wrap, '', true);
            }
            const st = data.render || {};
            if (data.mp4_ready && !st.output_url) {
                st.output_url = data.mp4_url;
                st.state = st.state || 'done';
                st.message = st.message || 'MP4 готов.';
            }
            if (st && st.state) {
                updateRenderUi(wrap, st);
                syncLaterStatusFromRender(wrap, st);
                if (st.state === 'queued' || st.state === 'running' || st.state === 'stuck') {
                    startRenderPoll(wrap, st.task_id || '');
                }
            } else if (data.mp4_ready) {
                const doneSt = { state: 'done', message: 'MP4 готов.', output_url: data.mp4_url, progress_pct: 100 };
                updateRenderUi(wrap, doneSt);
                syncLaterStatusFromRender(wrap, doneSt);
            }
        } catch (e) {
            /* no-op */
        }
    }

    async function pollRenderStatus(wrap, taskId) {
        try {
            const q = taskId ? '?task_id=' + encodeURIComponent(taskId) : '';
            const r = await fetch('/scenes-lab/api/remotion/render/status' + q);
            const data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok) return;
            updateRenderUi(wrap, data);
            syncLaterStatusFromRender(wrap, data);
            const state = data.state;
            if (state === 'done' || state === 'error' || state === 'cancelled') {
                stopRenderPoll();
                const renderBtn = wrap.querySelector('[data-later-render-start]');
                if (renderBtn) renderBtn.disabled = false;
            }
        } catch (e) {
            /* ignore */
        }
    }

    function startRenderPoll(wrap, taskId) {
        stopRenderPoll();
        pollRenderStatus(wrap, taskId);
        renderPollTimer = setInterval(function () {
            pollRenderStatus(wrap, taskId);
        }, 1500);
    }

    async function refreshRenderStatus(wrap) {
        await refreshRemotionInfo(wrap);
    }

    function clearPipelineUi(wrap) {
        stopRenderPoll();
        setMp4Preview(wrap, '', false);
        setRemotionPanelVisible(wrap, false);
        const root = wrap.querySelector('[data-later-pipeline]');
        if (root) root.classList.add('is-hidden');
        const banner = wrap.querySelector('[data-later-validation-banner]');
        if (banner) {
            banner.className = 'later-pipeline__banner';
            banner.textContent = '';
        }
        wrap.querySelectorAll('[data-later-svg-out], [data-later-json-out], [data-later-notes-out]').forEach(function (el) {
            el.value = '';
        });
        setRawAnswer(wrap, '', { onlyIfEmpty: false });
        const preview = wrap.querySelector('[data-later-svg-preview]');
        if (preview) preview.innerHTML = '';
    }

    function restoreFormFields(wrap, data) {
        const promptEl = wrap.querySelector('[data-later-prompt]');
        const modelEl = wrap.querySelector('[data-later-model]');
        const preview = wrap.querySelector('[data-later-preview]');
        if (promptEl && data.user_prompt) promptEl.value = data.user_prompt;
        if (modelEl && data.model) modelEl.value = data.model;
        if (preview && data.image_url) {
            preview.src = data.image_url;
            preview.classList.remove('is-hidden');
        }
    }

    function showSvgIframePreview(preview, svg) {
        preview.innerHTML = '';
        const iframe = document.createElement('iframe');
        iframe.setAttribute('sandbox', '');
        iframe.setAttribute('title', 'SVG preview (vector)');
        iframe.srcdoc =
            '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' +
            'html,body{margin:0;height:100%;background:#0a0a0c;display:flex;align-items:center;justify-content:center;overflow:hidden}' +
            'svg{max-width:100%;max-height:100%;width:auto;height:auto;display:block}' +
            '</style></head><body>' +
            svg +
            '</body></html>';
        preview.appendChild(iframe);
    }

    async function showSvgRasterPreview(wrap, preview, svg, validationOk, previewWrap) {
        preview.innerHTML = '<div class="later-svg-preview__loading">Рендер PNG 1920×1080…</div>';
        try {
            const r = await fetch('/scenes-lab/api/svg-render-preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ svg_fragment: svg }),
            });
            const data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok || !data.preview_url) {
                throw new Error((data && data.error) || 'Рендер превью');
            }
            preview.innerHTML = '';
            const img = document.createElement('img');
            img.className = 'later-svg-preview__img';
            img.src = data.preview_url + (data.preview_url.indexOf('?') >= 0 ? '&' : '?') + 'v=' + Date.now();
            img.alt = 'Превью кадра 1920×1080';
            img.width = 1920;
            img.height = 1080;
            preview.appendChild(img);
        } catch (e) {
            preview.innerHTML = '';
            showSvgIframePreview(preview, svg);
            if (previewWrap) {
                const note = document.createElement('div');
                note.className = 'later-svg-preview__warn';
                note.textContent =
                    'PNG-превью недоступно (' + String(e.message || e) + ') — показан векторный iframe.';
                previewWrap.insertBefore(note, preview);
            }
        }
    }

    function showPipeline(wrap, data) {
        const root = wrap.querySelector('[data-later-pipeline]');
        if (!root) return;
        root.classList.remove('is-hidden');
        const parsed = (data && data.parsed) || {};
        const validation = (data && data.validation) || {};
        const banner = wrap.querySelector('[data-later-validation-banner]');
        if (banner) {
            const errs = validation.errors || [];
            const warns = validation.warnings || [];
            let msg = '';
            if (warns.length) msg += warns.join('\n') + '\n\n';
            if (validation.ok) {
                banner.className = 'later-pipeline__banner later-pipeline__banner--ok';
                msg += 'Валидация пройдена — можно отдавать во вьюер / Remotion.';
            } else {
                banner.className = 'later-pipeline__banner later-pipeline__banner--err';
                msg += errs.length
                    ? 'Отклонено:\n' + errs.join('\n')
                    : 'Валидация не пройдена.';
            }
            banner.textContent = msg.trim();
        }
        const svgOut = wrap.querySelector('[data-later-svg-out]');
        const jsonOut = wrap.querySelector('[data-later-json-out]');
        const notesOut = wrap.querySelector('[data-later-notes-out]');
        const preview = wrap.querySelector('[data-later-svg-preview]');
        const svg = parsed.svg || '';
        setRawAnswer(wrap, (data && data.text) || '', { onlyIfEmpty: false });
        if (svgOut) svgOut.value = svg;
        if (jsonOut) {
            jsonOut.value = parsed.animation_raw
                || (parsed.animation ? JSON.stringify(parsed.animation, null, 2) : '');
        }
        if (notesOut) notesOut.value = parsed.notes || '';
        setRemotionPanelVisible(wrap, Boolean(validation.ok));
        if (validation.ok) {
            refreshRemotionInfo(wrap);
        }
        const previewWrap = wrap.querySelector('.later-svg-preview-wrap');
        if (previewWrap) {
            previewWrap.querySelectorAll('.later-svg-preview__warn').forEach(function (el) {
                el.remove();
            });
        }
        if (preview) {
            if (svg) {
                if (!validation.ok && previewWrap) {
                    const note = document.createElement('div');
                    note.className = 'later-svg-preview__warn';
                    note.textContent =
                        'Превью черновое: валидация не пройдена — исправьте ответ и нажмите «Проверить и собрать».';
                    previewWrap.insertBefore(note, preview);
                }
                showSvgRasterPreview(wrap, preview, svg, validation.ok, previewWrap);
            } else {
                preview.innerHTML = '';
                preview.textContent = 'SVG пустой — проверьте блок ===SVG_START=== в ответе.';
            }
        }
    }

    function setSvgPatchStatus(wrap, msg, kind) {
        const el = wrap.querySelector('[data-svg-patch-status]');
        if (!el) return;
        if (!msg) {
            el.classList.add('is-hidden');
            el.textContent = '';
            el.className = 'later-svg-patch__status is-hidden';
            return;
        }
        el.classList.remove('is-hidden');
        el.className = 'later-svg-patch__status';
        if (kind === 'err') el.classList.add('later-svg-patch__status--err');
        if (kind === 'ok') el.classList.add('later-svg-patch__status--ok');
        el.textContent = msg;
    }

    function setSvgPatchPreview(wrap, url) {
        const wrapImg = wrap.querySelector('[data-svg-patch-preview-wrap]');
        const img = wrap.querySelector('[data-svg-patch-preview]');
        if (!wrapImg || !img) return;
        if (!url) {
            wrapImg.classList.add('is-hidden');
            img.removeAttribute('src');
            return;
        }
        img.src = url;
        wrapImg.classList.remove('is-hidden');
    }

    function bindSvgPatch(wrap) {
        const pullBtn = wrap.querySelector('[data-svg-patch-pull]');
        const renderBtn = wrap.querySelector('[data-svg-patch-render]');
        const sendBtn = wrap.querySelector('[data-svg-patch-send]');
        const fragEl = wrap.querySelector('[data-svg-patch-fragment]');
        if (!fragEl) return;

        pullBtn?.addEventListener('click', function () {
            const svgOut = wrap.querySelector('[data-later-svg-out]');
            const svg = svgOut ? svgOut.value : '';
            if (!svg.trim()) {
                setSvgPatchStatus(wrap, 'Сначала соберите SVG (ответ модели или «Проверить и собрать»).', 'err');
                return;
            }
            fragEl.value = svg;
            setSvgPatchStatus(wrap, 'Фрагмент скопирован из вкладки SVG.', 'ok');
        });

        async function renderPreview() {
            const fragment = fragEl.value.trim();
            if (!fragment) {
                setSvgPatchStatus(wrap, 'Вставьте фрагмент SVG.', 'err');
                return;
            }
            renderBtn && (renderBtn.disabled = true);
            setSvgPatchStatus(wrap, 'Рендер PNG…', '');
            try {
                const r = await fetch('/scenes-lab/api/svg-render-preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ svg_fragment: fragment }),
                });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || 'Ошибка рендера');
                }
                setSvgPatchPreview(wrap, data.preview_url || '');
                setSvgPatchStatus(wrap, 'Превью PNG готово.', 'ok');
            } catch (e) {
                setSvgPatchStatus(wrap, String(e.message || e), 'err');
            } finally {
                if (renderBtn) renderBtn.disabled = false;
            }
        }

        renderBtn?.addEventListener('click', renderPreview);

        sendBtn?.addEventListener('click', async function () {
            const modelEl = wrap.querySelector('[data-later-model]');
            const mid = modelEl ? modelEl.value : '';
            if (!modelKeyOk(mid)) {
                setSvgPatchStatus(
                    wrap,
                    isOpenaiLaterModel(mid) ? 'Нужен OPENAI_API_KEY' : 'Нужен KEYAI_API_KEY',
                    'err'
                );
                return;
            }
            const fragment = fragEl.value.trim();
            if (!fragment) {
                setSvgPatchStatus(wrap, 'Фрагмент SVG пустой.', 'err');
                return;
            }
            const rawOut = wrap.querySelector('[data-later-raw-out]');
            const fullText = rawOut ? rawOut.value.trim() : '';
            if (!fullText) {
                setSvgPatchStatus(wrap, 'Нет полного ответа — вставьте или получите ответ с маркерами.', 'err');
                return;
            }
            const sysEl = wrap.querySelector('[data-svg-patch-system]');
            const userEl = wrap.querySelector('[data-svg-patch-user]');
            sendBtn.disabled = true;
            setSvgPatchStatus(wrap, 'Модель правит SVG… (1–3 мин)', '');
            setLaterStatus(wrap, 'Правка фрагмента SVG…', 'generating', {
                detail: 'Рендер PNG + запрос к модели',
            });
            try {
                const r = await fetch('/scenes-lab/api/svg-patch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        model: mid,
                        system_prompt: sysEl ? sysEl.value : '',
                        user_prompt: userEl ? userEl.value : '',
                        svg_fragment: fragment,
                        text: fullText,
                    }),
                });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || 'Ошибка правки SVG');
                }
                if (data.preview_url) setSvgPatchPreview(wrap, data.preview_url);
                if (data.patch_svg && fragEl) fragEl.value = data.patch_svg;
                await applyParseResult(wrap, data);
                setSvgPatchStatus(wrap, 'SVG заменён в полном ответе, пайплайн обновлён.', 'ok');
            } catch (e) {
                setSvgPatchStatus(wrap, String(e.message || e), 'err');
                setLaterStatus(wrap, String(e.message || e), 'error');
            } finally {
                sendBtn.disabled = false;
            }
        });
    }

    function bindTabs(wrap) {
        const tabs = wrap.querySelectorAll('.later-pipeline__tab');
        const panels = wrap.querySelectorAll('.later-pipeline__panel');
        tabs.forEach(function (tab) {
            tab.addEventListener('click', function () {
                const name = tab.getAttribute('data-tab');
                tabs.forEach(function (t) { t.classList.toggle('is-active', t === tab); });
                panels.forEach(function (p) {
                    const on = p.getAttribute('data-panel') === name;
                    p.classList.toggle('is-active', on);
                    p.hidden = !on;
                });
            });
        });
    }

    async function applyParseResult(wrap, data) {
        showPipeline(wrap, data);
        const ok = data && data.pipeline_ok;
        const saved = data && data.saved_at;
        let status = ok ? 'Готово — валидация OK' : 'Ответ получен — есть ошибки валидации';
        if (saved) status += ' (сохранено)';
        setLaterStatus(wrap, status, ok ? 'done' : 'error');
    }

    async function parseOnServer(wrap, text) {
        const r = await fetch('/scenes-lab/api/parse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: text,
                model: (wrap.querySelector('[data-later-model]') || {}).value || '',
                user_prompt: (wrap.querySelector('[data-later-prompt]') || {}).value || '',
                image_url: currentImageUrl(wrap),
            }),
        });
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            throw new Error((data && data.error) || 'Ошибка разбора');
        }
        data.text = text;
        await applyParseResult(wrap, data);
    }

    async function loadSavedSession(wrap) {
        try {
            const r = await fetch('/scenes-lab/api/state');
            const data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok || !data.has_saved) return;
            restoreFormFields(wrap, data);
            await applyParseResult(wrap, data);
        } catch (e) {
            /* no-op */
        }
    }

    function bindLaterLab(wrap) {
        const sendBtn = wrap.querySelector('[data-later-send]');
        const reparseBtn = wrap.querySelector('[data-later-reparse]');
        const fileInput = wrap.querySelector('[data-later-file]');
        const preview = wrap.querySelector('[data-later-preview]');
        const promptEl = wrap.querySelector('[data-later-prompt]');
        const modelEl = wrap.querySelector('[data-later-model]');
        if (!sendBtn || !KEY_ANY) return;

        syncLaterPanelEnabled(wrap);
        modelEl?.addEventListener('change', function () {
            syncLaterPanelEnabled(wrap);
        });

        bindTabs(wrap);
        bindSvgPatch(wrap);
        loadSavedSession(wrap);
        let uploadToken = 0;

        fileInput?.addEventListener('change', async function () {
            const file = fileInput.files && fileInput.files[0];
            if (!file) return;
            const token = ++uploadToken;
            setLaterStatus(wrap, 'Загрузка фото…', 'generating', {
                detail: 'Отправка файла на сервер…',
            });
            try {
                const fd = new FormData();
                fd.append('image', file);
                const r = await fetch('/scenes-lab/api/upload', { method: 'POST', body: fd });
                const data = await r.json().catch(function () { return {}; });
                if (token !== uploadToken) return;
                if (!r.ok || !data.ok || !data.image_url) {
                    throw new Error((data && data.error) || 'Не удалось загрузить фото');
                }
                if (preview) {
                    preview.src = data.image_url;
                    preview.classList.remove('is-hidden');
                }
                setLaterStatus(wrap, 'Фото загружено', 'done');
            } catch (e) {
                if (token !== uploadToken) return;
                setLaterStatus(wrap, String(e.message || e), 'error');
            }
        });

        sendBtn.addEventListener('click', async function () {
            const mid = modelEl ? modelEl.value : '';
            if (!modelKeyOk(mid)) {
                setLaterStatus(
                    wrap,
                    isOpenaiLaterModel(mid)
                        ? 'Нужен OPENAI_API_KEY для ChatGPT 5.4'
                        : 'Нужен KEYAI_API_KEY для Claude',
                    'error'
                );
                return;
            }
            const imageUrl = currentImageUrl(wrap);
            if (!imageUrl) {
                setLaterStatus(wrap, 'Прикрепите фото перед отправкой', 'error');
                return;
            }
            clearPipelineUi(wrap);
            sendBtn.disabled = true;
            const waitDetail = isOpenaiLaterModel(mid)
                ? 'ChatGPT 5.4 (OpenAI) — разбор SVG и JSON может занять 1–3 минуты'
                : 'Claude через Kie.ai — разбор SVG и JSON может занять 1–3 минуты';
            setLaterStatus(wrap, 'Ожидание ответа модели…', 'generating', {
                detail: waitDetail,
            });
            try {
                const r = await fetch('/scenes-lab/api/claude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        model: modelEl ? modelEl.value : '',
                        user_prompt: promptEl ? promptEl.value : '',
                        image_url: imageUrl,
                    }),
                });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || 'Ошибка запроса');
                }
                await applyParseResult(wrap, data);
            } catch (e) {
                setLaterStatus(wrap, String(e.message || e), 'error');
            } finally {
                sendBtn.disabled = false;
            }
        });

        const propsBtn = wrap.querySelector('[data-later-props-write]');
        const renderBtn = wrap.querySelector('[data-later-render-start]');

        const studioBtn = wrap.querySelector('[data-later-studio-open]');
        const mp4Btn = wrap.querySelector('[data-later-mp4-open]');

        studioBtn?.addEventListener('click', function () {
            window.open('/scenes-lab/remotion/studio', '_blank', 'noopener');
        });
        mp4Btn?.addEventListener('click', function () {
            const url = mp4Btn.dataset.mp4Url;
            if (!url) return;
            const sep = url.indexOf('?') >= 0 ? '&' : '?';
            const dlUrl = url + sep + 'download=1';
            const a = document.createElement('a');
            a.href = dlUrl;
            a.download = 'later_infographic.mp4';
            a.rel = 'noopener';
            document.body.appendChild(a);
            a.click();
            a.remove();
        });

        propsBtn?.addEventListener('click', async function () {
            propsBtn.disabled = true;
            setPropsHint(wrap, 'Запись props.json…', true);
            try {
                const r = await fetch('/scenes-lab/api/remotion-props', { method: 'POST' });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || (data && data.message) || 'Ошибка записи props');
                }
                setPropsHint(wrap, data.message || 'props.json записан.', true);
                updateRenderUi(wrap, { state: 'props_ok', message: 'Можно открыть Studio или запустить рендер MP4.' });
            } catch (e) {
                setPropsHint(wrap, String(e.message || e), false);
                updateRenderUi(wrap, { state: 'error', message: String(e.message || e) });
            } finally {
                propsBtn.disabled = false;
            }
        });

        renderBtn?.addEventListener('click', async function () {
            renderBtn.disabled = true;
            setLaterStatus(wrap, 'Запуск рендера MP4…', 'generating', {
                detail: 'Remotion LaterInfographic — может занять несколько минут',
            });
            setMp4Preview(wrap, '', false);
            updateRenderUi(wrap, { state: 'queued', message: 'Запуск…', progress_pct: 0 });
            try {
                const r = await fetch('/scenes-lab/api/remotion/render', { method: 'POST' });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    const errText = (data && data.message) || (data && data.error_detail) || (data && data.error) || 'Ошибка рендера (HTTP ' + r.status + ')';
                    throw new Error(errText);
                }
                setPropsHint(wrap, 'props.json записан перед рендером.', true);
                updateRenderUi(wrap, data);
                if (data.task_id) {
                    startRenderPoll(wrap, data.task_id);
                }
            } catch (e) {
                const errSt = { state: 'error', message: String(e.message || e) };
                updateRenderUi(wrap, errSt);
                syncLaterStatusFromRender(wrap, errSt);
                renderBtn.disabled = false;
            }
        });

        refreshRenderStatus(wrap);

        reparseBtn?.addEventListener('click', function () {
            runReparseFromRaw(wrap);
        });

        const rawOut = wrap.querySelector('[data-later-raw-out]');
        rawOut?.addEventListener('keydown', function (ev) {
            if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
                ev.preventDefault();
                runReparseFromRaw(wrap);
            }
        });
    }

    document.querySelectorAll('[data-later-lab]').forEach(bindLaterLab);
})();
