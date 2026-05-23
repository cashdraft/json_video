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
    const opTimers = new WeakMap();
    const animTimers = new WeakMap();

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

    function appendLog(wrap, line) {
        const log = wrap.querySelector('[data-later-log]');
        if (!log || !line) return;
        log.classList.remove('is-hidden');
        const ts = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        log.textContent += (log.textContent ? '\n' : '') + '[' + ts + '] ' + line;
        log.scrollTop = log.scrollHeight;
    }

    function stopOpTimer(wrap) {
        const t = opTimers.get(wrap);
        if (t) {
            clearInterval(t.id);
            opTimers.delete(wrap);
        }
        const timerEl = wrap.querySelector('[data-later-op-status-timer]');
        if (timerEl) timerEl.classList.add('is-hidden');
    }

    function startOpTimer(wrap) {
        stopOpTimer(wrap);
        const timerEl = wrap.querySelector('[data-later-op-status-timer]');
        const started = Date.now();
        if (timerEl) {
            timerEl.classList.remove('is-hidden');
            timerEl.textContent = '0 с';
        }
        const id = setInterval(function () {
            const sec = Math.floor((Date.now() - started) / 1000);
            if (timerEl) timerEl.textContent = sec + ' с';
        }, 1000);
        opTimers.set(wrap, { id: id, started: started });
    }

    function setOpStatus(wrap, text, state) {
        const box = wrap.querySelector('[data-later-op-status]');
        const label = wrap.querySelector('[data-later-op-status-text]');
        if (!box || !label) return;
        const st = state || 'idle';
        box.classList.remove('is-hidden', 'later-op-status--generating', 'later-op-status--ok', 'later-op-status--err');
        if (st === 'idle' || !text) {
            box.classList.add('is-hidden');
            label.textContent = '';
            stopOpTimer(wrap);
            return;
        }
        box.classList.remove('is-hidden');
        if (st === 'generating') {
            box.classList.add('later-op-status--generating');
            startOpTimer(wrap);
        } else {
            stopOpTimer(wrap);
            if (st === 'ok') box.classList.add('later-op-status--ok');
            if (st === 'error') box.classList.add('later-op-status--err');
        }
        label.textContent = text;
    }

    function stopAnimTimer(wrap) {
        const t = animTimers.get(wrap);
        if (t) {
            clearInterval(t.id);
            animTimers.delete(wrap);
        }
        const timerEl = wrap.querySelector('[data-later-anim-status-timer]');
        if (timerEl) timerEl.classList.add('is-hidden');
    }

    function startAnimTimer(wrap) {
        stopAnimTimer(wrap);
        const timerEl = wrap.querySelector('[data-later-anim-status-timer]');
        const started = Date.now();
        if (timerEl) {
            timerEl.classList.remove('is-hidden');
            timerEl.textContent = '0 с';
        }
        const id = setInterval(function () {
            const sec = Math.floor((Date.now() - started) / 1000);
            if (timerEl) timerEl.textContent = sec + ' с';
        }, 1000);
        animTimers.set(wrap, { id: id, started: started });
    }

    function setAnimStatus(wrap, text, state) {
        const box = wrap.querySelector('[data-later-anim-status]');
        const label = wrap.querySelector('[data-later-anim-status-text]');
        if (!box || !label) return;
        const st = state || 'idle';
        box.classList.remove(
            'is-hidden',
            'later-anim-status--generating',
            'later-anim-status--ok',
            'later-anim-status--err'
        );
        if (st === 'idle' || !text) {
            box.classList.add('is-hidden');
            label.textContent = '';
            stopAnimTimer(wrap);
            return;
        }
        box.classList.remove('is-hidden');
        if (st === 'generating') {
            box.classList.add('later-anim-status--generating');
            startAnimTimer(wrap);
        } else {
            stopAnimTimer(wrap);
            if (st === 'ok') box.classList.add('later-anim-status--ok');
            if (st === 'error') box.classList.add('later-anim-status--err');
        }
        label.textContent = text;
    }

    function clearLog(wrap) {
        const log = wrap.querySelector('[data-later-log]');
        if (!log) return;
        log.textContent = '';
        log.classList.add('is-hidden');
    }

    function setLaterStatus(wrap, text, state, opts) {
        const box = wrap.querySelector('[data-later-status]');
        const label = wrap.querySelector('[data-later-status-text]');
        const st = state || 'pending';
        if (box) box.setAttribute('data-status', st);
        if (label) {
            label.textContent = text || '';
            label.classList.toggle('slot-status-with-spinner', st === 'generating');
        }
        if (st === 'generating') {
            let line = text || '…';
            if (opts && opts.detail) line += ' — ' + opts.detail;
            appendLog(wrap, line);
            startStatusTimer(wrap, (opts && opts.detail) || '');
        } else {
            stopStatusTimer(wrap);
            if (text) appendLog(wrap, text);
        }
    }

    function normalizeLaterImageUrl(url) {
        const u = String(url || '').trim();
        if (!u) return '';
        try {
            const parsed = new URL(u);
            parsed.searchParams.delete('v');
            return parsed.toString();
        } catch (_) {
            return u.replace(/([?&])v=\d+(&|$)/, '$1').replace(/[?&]$/, '');
        }
    }

    function laterImageFilename(url) {
        const clean = normalizeLaterImageUrl(url);
        if (!clean) return '';
        try {
            const name = decodeURIComponent(clean.split('/').pop() || '').replace(/\?.*$/, '');
            return name || clean;
        } catch (_) {
            return clean.split('/').pop() || clean;
        }
    }

    function setLaterAttachedImage(wrap, url) {
        const preview = wrap.querySelector('[data-later-preview]');
        const meta = wrap.querySelector('[data-later-preview-meta]');
        const clean = normalizeLaterImageUrl(url);
        if (!clean) {
            delete wrap.dataset.laterImageUrl;
            if (preview) {
                preview.removeAttribute('src');
                preview.classList.add('is-hidden');
            }
            if (meta) meta.textContent = '';
            return '';
        }
        wrap.dataset.laterImageUrl = clean;
        if (preview) {
            const bust = clean + (clean.indexOf('?') >= 0 ? '&' : '?') + 'v=' + Date.now();
            preview.src = bust;
            preview.classList.remove('is-hidden');
        }
        const fname = laterImageFilename(clean);
        if (meta) meta.textContent = fname ? ('Файл: ' + fname) : '';
        return clean;
    }

    function currentImageUrl(wrap) {
        const fromData = normalizeLaterImageUrl(wrap.dataset.laterImageUrl || '');
        if (fromData) return fromData;
        const preview = wrap.querySelector('[data-later-preview]');
        const src = (preview && preview.getAttribute('src')) || '';
        return normalizeLaterImageUrl(src);
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
            setOpStatus(wrap, 'Вставьте ответ модели выше', 'error');
            appendLog(wrap, 'Ошибка: пустой ответ');
            return;
        }
        const reparseBtn = wrap.querySelector('[data-later-reparse]');
        if (reparseBtn) reparseBtn.disabled = true;
        appendLog(wrap, '→ POST /scenes-lab/api/parse');
        setOpStatus(wrap, 'Проверка и сборка на сервере…', 'generating');
        try {
            await parseOnServer(wrap, text);
            appendLog(wrap, 'Готово: SVG проверен');
            setOpStatus(wrap, 'Готово — проверка пройдена', 'ok');
        } catch (e) {
            const msg = String(e.message || e);
            appendLog(wrap, 'Ошибка: ' + msg);
            setOpStatus(wrap, msg, 'error');
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
        const viewer = wrap.querySelector('[data-later-slot-viewer]');
        if (viewer) viewer.classList.add('is-hidden');
        const st = getSlotCarousel(wrap);
        st.ids = [];
        st.index = 0;
        st.cache = {};
        st.slotMeta = {};
        st.textCache = {};
        st.animCache = {};
        st.fixlogCache = {};
        setSlotNavUi(wrap);
        const banner = wrap.querySelector('[data-later-validation-banner]');
        if (banner) {
            banner.className = 'later-result__banner';
            banner.textContent = '';
        }
        wrap.querySelectorAll('[data-later-svg-out], [data-later-json-out], [data-later-notes-out]').forEach(function (el) {
            el.value = '';
        });
        setRawAnswer(wrap, '', { onlyIfEmpty: false });
        const preview = wrap.querySelector('[data-later-svg-preview]');
        if (preview) preview.innerHTML = '';
        const savedHint = wrap.querySelector('[data-later-slot-saved-hint]');
        if (savedHint) {
            savedHint.classList.add('is-hidden');
            savedHint.textContent = '';
        }
        const animBlock = wrap.querySelector('[data-later-anim-block]');
        if (animBlock) animBlock.classList.add('is-hidden');
        st.animCache = {};
        setAnimStatus(wrap, '', 'idle');
        const animBanner = wrap.querySelector('[data-later-anim-validation-banner]');
        if (animBanner) {
            animBanner.className = 'later-result__banner is-hidden';
            animBanner.textContent = '';
        }
        const animRaw = wrap.querySelector('[data-later-anim-raw-out]');
        if (animRaw) animRaw.value = '';
        setFixlogAnswer(wrap, '');
        setFixlogBlockVisible(wrap, false);
        const log = wrap.querySelector('[data-later-log]');
        if (log) {
            log.textContent = '';
            log.classList.add('is-hidden');
        }
        setPropsHint(wrap, '', true);
        updateRenderUi(wrap, { state: 'idle', message: '' });
    }

    function clearLabAttachment(wrap) {
        const fileInput = wrap.querySelector('[data-later-file]');
        setLaterAttachedImage(wrap, '');
        if (fileInput) fileInput.value = '';
    }

    async function clearLabWorkspace(wrap) {
        stopRenderPoll();
        clearPipelineUi(wrap);
        setOpStatus(wrap, '', 'idle');
        setLaterStatus(wrap, '', 'idle');
        setRemotionPanelVisible(wrap, false);
        setMp4Preview(wrap, '', false);
    }

    function getSceneDescription(wrap) {
        const el = wrap.querySelector('[data-later-scene-description]');
        return el ? (el.value || '').trim() : '';
    }

    function getSceneDuration(wrap) {
        const el = wrap.querySelector('[data-later-scene-duration]');
        return el ? (el.value || '').trim() : '';
    }

    function getSvgPromptTemplate(wrap) {
        const el = wrap.querySelector('[data-later-svg-prompt]');
        return el ? (el.value || '').trim() : '';
    }

    function getSvgPrompt2Template(wrap) {
        const el = wrap.querySelector('[data-later-svg-prompt2]');
        return el ? (el.value || '').trim() : '';
    }

    function getSvgExample2Template(wrap) {
        const el = wrap.querySelector('[data-later-svg-example2]');
        return el ? (el.value || '').trim() : '';
    }

    function getEditorPromptTemplate(wrap) {
        const el = wrap.querySelector('[data-later-editor-prompt]');
        return el ? (el.value || '').trim() : '';
    }

    function getAnimPromptTemplate(wrap) {
        const el = wrap.querySelector('[data-later-anim-prompt]');
        return el ? (el.value || '').trim() : '';
    }

    const slotCarousels = new WeakMap();
    const slotAnimLocks = new WeakMap();
    const prefsSaveTimers = new WeakMap();

    function getSlotCarousel(wrap) {
        let st = slotCarousels.get(wrap);
        if (!st) {
            st = { ids: [], index: 0, cache: {}, slotMeta: {}, textCache: {}, animCache: {}, fixlogCache: {} };
            slotCarousels.set(wrap, st);
        }
        return st;
    }

    function isRawAnswerExpanded(wrap) {
        const block = wrap.querySelector('[data-later-raw-answer-collapse]');
        return block && !block.classList.contains('later-collapsible--body-collapsed');
    }

    function preloadImageUrl(url) {
        if (!url) return;
        const img = new Image();
        img.decoding = 'async';
        img.src = url;
    }

    function preloadSlotNeighbors(wrap) {
        const st = getSlotCarousel(wrap);
        const i = st.index;
        [i - 1, i + 1, i].forEach(function (idx) {
            if (idx < 0 || idx >= st.ids.length) return;
            const meta = st.slotMeta[st.ids[idx]];
            if (meta && meta.preview_url) preloadImageUrl(meta.preview_url);
        });
    }

    function syncSlotMetaFromList(st, slots) {
        st.slotMeta = st.slotMeta || {};
        (slots || []).forEach(function (s) {
            if (!s || !s.id) return;
            st.slotMeta[s.id] = {
                preview_url: s.preview_url || '',
                saved_at: s.saved_at || '',
            };
            if (s.preview_url) preloadImageUrl(s.preview_url);
        });
    }

    function getCurrentSlotId(wrap) {
        const st = getSlotCarousel(wrap);
        if (!st.ids.length) return '';
        return st.ids[st.index] || st.ids[st.ids.length - 1] || '';
    }

    /** Последний слот на сервере (макс. img_N) — для «Переделать». */
    function getLatestSlotId(wrap) {
        const st = getSlotCarousel(wrap);
        if (!st.ids.length) return '';
        return st.ids[st.ids.length - 1] || '';
    }

    function isSlotAnimLocked(wrap) {
        return !!slotAnimLocks.get(wrap);
    }

    function setSlotAnimLock(wrap, locked) {
        slotAnimLocks.set(wrap, !!locked);
        const viewer = wrap.querySelector('[data-later-slot-viewer]');
        const lockEl = wrap.querySelector('[data-later-slot-preview-lock]');
        const preview = wrap.querySelector('[data-later-svg-preview]');
        if (viewer) viewer.classList.toggle('later-slot-viewer--anim-locked', !!locked);
        if (lockEl) {
            lockEl.classList.toggle('is-hidden', !locked);
            lockEl.setAttribute('aria-hidden', locked ? 'false' : 'true');
        }
        if (preview) preview.classList.toggle('later-svg-preview--locked', !!locked);
        setSlotNavUi(wrap);
    }

    function setSlotNavUi(wrap) {
        const st = getSlotCarousel(wrap);
        const locked = isSlotAnimLocked(wrap);
        const prevBtn = wrap.querySelector('[data-later-slot-prev]');
        const nextBtn = wrap.querySelector('[data-later-slot-next]');
        const remakeBtn = wrap.querySelector('[data-later-remake]');
        const has = st.ids.length > 0;
        if (prevBtn) prevBtn.disabled = locked || !has || st.index <= 0;
        if (nextBtn) nextBtn.disabled = locked || !has || st.index >= st.ids.length - 1;
        if (remakeBtn) remakeBtn.disabled = !has;
        const animateBtn = wrap.querySelector('[data-later-animate]');
        if (animateBtn) animateBtn.disabled = locked || !has;
    }

    async function refreshSlotsList(wrap, preferSlotId) {
        const r = await fetch('/scenes-lab/api/img-slots');
        const data = await r.json().catch(function () { return {}; });
        const st = getSlotCarousel(wrap);
        if (!r.ok || !data.ok) {
            st.ids = [];
            st.index = 0;
            setSlotNavUi(wrap);
            return st;
        }
        st.ids = (data.slots || []).map(function (s) { return s.id; });
        syncSlotMetaFromList(st, data.slots);
        let idx = st.ids.length ? st.ids.length - 1 : 0;
        if (preferSlotId) {
            const found = st.ids.indexOf(preferSlotId);
            if (found >= 0) idx = found;
        }
        st.index = idx;
        setSlotNavUi(wrap);
        return st;
    }

    async function loadSlotText(wrap, slotId) {
        const st = getSlotCarousel(wrap);
        if (st.textCache[slotId] !== undefined) return st.textCache[slotId];
        const r = await fetch(
            '/scenes-lab/api/img-slots/' + encodeURIComponent(slotId) + '?text=1'
        );
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            throw new Error((data && data.error) || 'Не удалось загрузить ответ');
        }
        st.textCache[slotId] = data.text || '';
        return st.textCache[slotId];
    }

    function applySlotText(wrap, slotId, text) {
        const st = getSlotCarousel(wrap);
        st.textCache[slotId] = text;
        if (isRawAnswerExpanded(wrap)) {
            setRawAnswer(wrap, text, { onlyIfEmpty: false });
        }
    }

    function resolveSlotPreviewUrl(slotId, meta) {
        if (meta && meta.preview_url) return meta.preview_url;
        if (!slotId) return '';
        return '/scenes-lab/img-slots/' + encodeURIComponent(slotId) + '/preview_thumb.png';
    }

    function showSlotPreviewFast(preview, url, slotId, savedAt) {
        if (!preview) return;
        if (!url) {
            preview.innerHTML = '';
            preview.textContent = 'Нет превью для ' + (slotId || '');
            return;
        }
        preview.classList.add('later-svg-preview--loading');
        let img = preview.querySelector('.later-svg-preview__img');
        if (!img) {
            preview.innerHTML = '';
            img = document.createElement('img');
            img.className = 'later-svg-preview__img';
            preview.appendChild(img);
        }
        img.alt = slotId || 'img';
        const ver = savedAt ? String(savedAt).replace(/\+/g, '%2B') : '';
        const base = url.split('?')[0];
        const newSrc = ver ? base + '?v=' + encodeURIComponent(ver) : base;
        img.onload = function () {
            preview.classList.remove('later-svg-preview--loading');
        };
        img.onerror = function () {
            preview.classList.remove('later-svg-preview--loading');
            if (base.indexOf('preview_thumb') >= 0) {
                img.onerror = null;
                img.src = base.replace('preview_thumb.png', 'preview.png') + (ver ? '?v=' + encodeURIComponent(ver) : '');
            }
        };
        if (img.src !== newSrc) {
            img.removeAttribute('src');
            img.src = newSrc;
        }
    }

    async function loadSlotAnimText(wrap, slotId) {
        const st = getSlotCarousel(wrap);
        if (st.animCache[slotId] !== undefined) return st.animCache[slotId];
        const r = await fetch(
            '/scenes-lab/api/img-slots/' + encodeURIComponent(slotId) + '?anim=1'
        );
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            st.animCache[slotId] = '';
            return '';
        }
        st.animCache[slotId] = data.anim_text || '';
        return st.animCache[slotId];
    }

    function setAnimRawAnswer(wrap, text) {
        const ta = wrap.querySelector('[data-later-anim-raw-out]');
        if (ta) ta.value = text || '';
    }

    function setFixlogAnswer(wrap, text) {
        const ta = wrap.querySelector('[data-later-fixlog-out]');
        if (ta) ta.value = text || '';
    }

    function setFixlogBlockVisible(wrap, visible) {
        const block = wrap.querySelector('[data-later-fixlog-block]');
        if (block) block.classList.toggle('is-hidden', !visible);
    }

    function expandFixlogAnswer(wrap) {
        const block = wrap.querySelector('[data-later-fixlog-collapse]');
        const btn = wrap.querySelector('[data-later-fixlog-collapse-toggle]');
        const body = wrap.querySelector('[data-later-fixlog-body]');
        if (block && btn && body) setLaterCollapsible(block, btn, body, false);
    }

    async function loadSlotFixlogText(wrap, slotId) {
        const st = getSlotCarousel(wrap);
        if (st.fixlogCache[slotId] !== undefined) return st.fixlogCache[slotId];
        const r = await fetch(
            '/scenes-lab/api/img-slots/' + encodeURIComponent(slotId) + '?fixlog=1'
        );
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            st.fixlogCache[slotId] = '';
            return '';
        }
        st.fixlogCache[slotId] = data.fixlog_text || '';
        return st.fixlogCache[slotId];
    }

    function applyFixlogUiForSlot(wrap, slotId) {
        const st = getSlotCarousel(wrap);
        const cached = st.fixlogCache[slotId];
        if (cached !== undefined) {
            const has = Boolean((cached || '').trim());
            setFixlogAnswer(wrap, cached);
            setFixlogBlockVisible(wrap, has);
            return;
        }
        loadSlotFixlogText(wrap, slotId)
            .then(function (text) {
                if (getCurrentSlotId(wrap) !== slotId) return;
                const has = Boolean((text || '').trim());
                setFixlogAnswer(wrap, text);
                setFixlogBlockVisible(wrap, has);
            })
            .catch(function () { /* ignore */ });
    }

    function showAnimValidationBanner(wrap, validation) {
        const banner = wrap.querySelector('[data-later-anim-validation-banner]');
        if (!banner) return;
        const v = validation || {};
        const errs = v.errors || [];
        const warns = v.warnings || [];
        if (v.ok) {
            banner.className = 'later-result__banner later-result__banner--ok';
            let msg = 'Готово — валидация анимации ОК';
            if (warns.length) msg += '\n' + warns.join('\n');
            banner.textContent = msg;
            banner.classList.remove('is-hidden');
        } else {
            let msg = '';
            if (warns.length) msg += warns.join('\n') + '\n\n';
            banner.className = 'later-result__banner later-result__banner--err';
            msg += errs.length
                ? 'Анимация отклонена:\n' + errs.join('\n')
                : 'Валидация анимации не пройдена.';
            banner.textContent = msg.trim();
            banner.classList.remove('is-hidden');
        }
    }

    async function getSlotResponseText(wrap, slotId) {
        if (!slotId) return '';
        const st = getSlotCarousel(wrap);
        if (st.textCache[slotId] !== undefined) return st.textCache[slotId];
        const text = await loadSlotText(wrap, slotId);
        st.textCache[slotId] = text;
        return text;
    }

    function applyAnimUiForSlot(wrap, slotId) {
        const st = getSlotCarousel(wrap);
        const cached = st.animCache[slotId];
        if (cached !== undefined) {
            setAnimRawAnswer(wrap, cached);
            return;
        }
        loadSlotAnimText(wrap, slotId)
            .then(function (text) {
                if (getCurrentSlotId(wrap) === slotId) setAnimRawAnswer(wrap, text);
            })
            .catch(function () { /* ignore */ });
    }

    async function ensureSlotSvgInPipeline(wrap, slotId) {
        if (!slotId) return;
        const svgOut = wrap.querySelector('[data-later-svg-out]');
        if (!svgOut) return;
        if ((svgOut.value || '').trim()) return;
        const r = await fetch('/scenes-lab/api/img-slots/' + encodeURIComponent(slotId));
        const data = await r.json().catch(function () { return {}; });
        if (r.ok && data.ok && data.svg) {
            svgOut.value = data.svg;
        }
    }

    function laterRemotionPayload(wrap) {
        const slotId = getCurrentSlotId(wrap);
        if (!slotId) return {};
        // SVG и anim_response — только с сервера по slot_id (scene_at_anim.svg + anim_response.txt).
        return { slot_id: slotId };
    }

    async function syncRemotionForCurrentSlot(wrap) {
        const slotId = getCurrentSlotId(wrap);
        if (!slotId) {
            setRemotionPanelVisible(wrap, false);
            return;
        }
        const st = getSlotCarousel(wrap);
        let animText = st.animCache[slotId];
        if (animText === undefined) {
            try {
                animText = await loadSlotAnimText(wrap, slotId);
            } catch (e) {
                animText = '';
            }
        }
        const hasAnim = Boolean((animText || '').trim());
        setRemotionPanelVisible(wrap, hasAnim);
        if (hasAnim) {
            setAnimRawAnswer(wrap, animText);
            await ensureSlotSvgInPipeline(wrap, slotId);
            const statusEl = wrap.querySelector('[data-later-render-status]');
            if (statusEl && !statusEl.textContent.trim()) {
                updateRenderUi(wrap, {
                    state: 'idle',
                    message: 'Кадр ' + slotId + ': можно записать props.json и рендерить MP4.',
                });
            }
        }
    }

    async function applyAnimResult(wrap, data) {
        const validation = (data && data.validation) || {};
        const parsed = (data && data.parsed) || {};
        const animText = (data && data.anim_text) || '';
        const merged = (data && data.merged_text) || (data && data.text) || '';
        const slotId = (data && data.slot_id) || getCurrentSlotId(wrap);

        setAnimRawAnswer(wrap, animText);
        showAnimValidationBanner(wrap, validation);

        const st = getSlotCarousel(wrap);
        if (slotId) {
            st.animCache[slotId] = animText;
            if (merged) {
                st.textCache[slotId] = merged;
                if (getCurrentSlotId(wrap) === slotId) {
                    applySlotText(wrap, slotId, merged);
                }
            }
            await ensureSlotSvgInPipeline(wrap, slotId);
        }

        const jsonOut = wrap.querySelector('[data-later-json-out]');
        const animRaw = parsed.animation_raw
            || (parsed.animation ? JSON.stringify(parsed.animation, null, 2) : '');
        if (jsonOut) jsonOut.value = animRaw;
        const hasAnimation = Boolean(
            validation.ok
            && ((parsed.animation && typeof parsed.animation === 'object')
                || (animRaw && String(animRaw).trim())
                || (animText && String(animText).trim()))
        );
        setRemotionPanelVisible(wrap, hasAnimation);
        if (hasAnimation) {
            updateRenderUi(wrap, {
                state: 'idle',
                message: 'Анимация ОК — «Записать props.json» возьмёт SVG кадра '
                    + (slotId || '') + ' и ответ анимации.',
            });
            refreshRemotionInfo(wrap);
        } else if (!validation.ok) {
            updateRenderUi(wrap, {
                state: 'error',
                message: (validation.errors && validation.errors[0])
                    || 'Валидация анимации не пройдена.',
            });
        }
    }

    function displaySlotFrame(wrap, slotId, previewUrl, savedAt) {
        const viewer = wrap.querySelector('[data-later-slot-viewer]');
        const label = wrap.querySelector('[data-later-slot-label]');
        const preview = wrap.querySelector('[data-later-svg-preview]');
        const hint = wrap.querySelector('[data-later-slot-saved-hint]');
        const animBlock = wrap.querySelector('[data-later-anim-block]');
        if (label) label.textContent = slotId;
        if (hint) {
            hint.classList.remove('is-hidden');
            hint.textContent = 'На сервере: data/scenes_lab/' + slotId + '/ (превью 960×540)';
        }
        if (viewer) viewer.classList.remove('is-hidden');
        if (animBlock) animBlock.classList.remove('is-hidden');
        const url = previewUrl || resolveSlotPreviewUrl(slotId, null);
        showSlotPreviewFast(preview, url, slotId, savedAt);
        applyAnimUiForSlot(wrap, slotId);
        applyFixlogUiForSlot(wrap, slotId);
        syncRemotionForCurrentSlot(wrap).catch(function () { /* ignore */ });
    }

    function displaySlotInViewer(wrap, slotId, detail) {
        const meta = {
            preview_url: detail.preview_url,
            saved_at: detail.saved_at || '',
        };
        const st = getSlotCarousel(wrap);
        st.slotMeta[slotId] = meta;
        displaySlotFrame(wrap, slotId, meta.preview_url, meta.saved_at);
        applySlotText(wrap, slotId, detail.text || '');
    }

    async function showSlotAt(wrap, index) {
        const st = getSlotCarousel(wrap);
        if (!st.ids.length) return;
        st.index = Math.max(0, Math.min(index, st.ids.length - 1));
        const slotId = st.ids[st.index];
        setSlotNavUi(wrap);

        const meta = st.slotMeta[slotId] || {};
        const previewUrl = resolveSlotPreviewUrl(slotId, meta);
        displaySlotFrame(wrap, slotId, previewUrl, meta.saved_at);
        preloadSlotNeighbors(wrap);

        const cachedText = st.textCache[slotId];
        if (cachedText !== undefined) {
            if (isRawAnswerExpanded(wrap)) {
                applySlotText(wrap, slotId, cachedText);
            }
            return;
        }
        loadSlotText(wrap, slotId)
            .then(function (text) {
                st.textCache[slotId] = text;
                if (getCurrentSlotId(wrap) === slotId && isRawAnswerExpanded(wrap)) {
                    applySlotText(wrap, slotId, text);
                }
            })
            .catch(function () { /* фоновая подгрузка */ });
    }

    function laterFormPayload(wrap) {
        const ep = getEditorPromptTemplate(wrap);
        const slotId = getCurrentSlotId(wrap);
        return {
            model: (wrap.querySelector('[data-later-model]') || {}).value || '',
            svg_prompt: getSvgPromptTemplate(wrap),
            svg_prompt_2: getSvgPrompt2Template(wrap),
            svg_example_2: getSvgExample2Template(wrap),
            scene_description: getSceneDescription(wrap),
            scene_duration_sec: getSceneDuration(wrap),
            image_url: currentImageUrl(wrap),
            editor_prompt: ep,
            img_1_prompt: ep,
            anim_prompt: getAnimPromptTemplate(wrap),
            target_slot: slotId || undefined,
        };
    }

    async function saveLaterPrefs(wrap) {
        const r = await fetch('/scenes-lab/api/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(laterFormPayload(wrap)),
        });
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            throw new Error((data && data.error) || 'Не удалось сохранить настройки');
        }
    }

    function scheduleSaveLaterPrefs(wrap, delayMs) {
        let t = prefsSaveTimers.get(wrap);
        if (t) clearTimeout(t);
        t = setTimeout(function () {
            saveLaterPrefs(wrap).catch(function () { /* ignore */ });
        }, delayMs == null ? 450 : delayMs);
        prefsSaveTimers.set(wrap, t);
    }

    async function loadLaterPrefs(wrap) {
        const r = await fetch('/scenes-lab/api/prefs');
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) return;
        restoreFormFields(wrap, data);
    }

    function setLaterCollapsible(block, btn, body, collapsed) {
        if (!block || !body) return;
        block.classList.toggle('later-collapsible--body-collapsed', collapsed);
        if (btn) btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        block.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        body.hidden = collapsed;
    }

    function bindLaterCollapsible(wrap, blockSel, toggleSel, bodySel, startCollapsed) {
        const block = wrap.querySelector(blockSel);
        const btn = wrap.querySelector(toggleSel);
        const body = wrap.querySelector(bodySel);
        if (!block || !btn || !body) return;
        setLaterCollapsible(block, btn, body, startCollapsed !== false);
        btn.addEventListener('click', function () {
            const collapsed = block.classList.contains('later-collapsible--body-collapsed');
            setLaterCollapsible(block, btn, body, !collapsed);
        });
    }

    function setSvgPromptCollapsed(wrap, collapsed) {
        setLaterCollapsible(
            wrap.querySelector('[data-later-svg-prompt-collapse]'),
            wrap.querySelector('[data-later-svg-prompt-collapse-toggle]'),
            wrap.querySelector('[data-later-svg-prompt-body]'),
            collapsed
        );
    }

    function bindSvgPromptCollapse(wrap) {
        bindLaterCollapsible(
            wrap,
            '[data-later-svg-prompt-collapse]',
            '[data-later-svg-prompt-collapse-toggle]',
            '[data-later-svg-prompt-body]',
            true
        );
    }

    function bindRawAnswerCollapse(wrap) {
        const block = wrap.querySelector('[data-later-raw-answer-collapse]');
        const btn = wrap.querySelector('[data-later-raw-answer-collapse-toggle]');
        const body = wrap.querySelector('[data-later-raw-answer-body]');
        if (!block || !btn || !body) return;
        setLaterCollapsible(block, btn, body, true);
        btn.addEventListener('click', function () {
            const collapsed = block.classList.contains('later-collapsible--body-collapsed');
            setLaterCollapsible(block, btn, body, !collapsed);
            if (collapsed) {
                const slotId = getCurrentSlotId(wrap);
                const st = getSlotCarousel(wrap);
                if (!slotId) return;
                const text = st.textCache[slotId];
                if (text !== undefined) {
                    setRawAnswer(wrap, text, { onlyIfEmpty: false });
                } else {
                    loadSlotText(wrap, slotId)
                        .then(function (t) {
                            if (getCurrentSlotId(wrap) === slotId) {
                                applySlotText(wrap, slotId, t);
                            }
                        })
                        .catch(function (e) {
                            appendLog(wrap, 'Не удалось загрузить ответ: ' + String(e.message || e));
                        });
                }
            }
        });
    }

    function bindEditorPromptCollapse(wrap) {
        bindLaterCollapsible(
            wrap,
            '[data-later-editor-prompt-collapse]',
            '[data-later-editor-prompt-collapse-toggle]',
            '[data-later-editor-prompt-body]',
            true
        );
    }

    function setSvgPrompt2Collapsed(wrap, collapsed) {
        setLaterCollapsible(
            wrap.querySelector('[data-later-svg-prompt2-collapse]'),
            wrap.querySelector('[data-later-svg-prompt2-collapse-toggle]'),
            wrap.querySelector('[data-later-svg-prompt2-body]'),
            collapsed
        );
    }

    function bindSvgPrompt2Collapse(wrap) {
        bindLaterCollapsible(
            wrap,
            '[data-later-svg-prompt2-collapse]',
            '[data-later-svg-prompt2-collapse-toggle]',
            '[data-later-svg-prompt2-body]',
            true
        );
    }

    function bindSvgExample2Lock(wrap) {
        const toggle = wrap.querySelector('[data-later-svg-example2-toggle]');
        const ta = wrap.querySelector('[data-later-svg-example2]');
        if (!toggle || !ta) return;
        function setLocked(locked) {
            toggle.classList.toggle('rewrite-lock-toggle--locked', locked);
            ta.readOnly = locked;
            ta.classList.toggle('rewrite-source-textarea--locked', locked);
            toggle.title = locked ? 'Редактировать' : 'Сохранить';
            toggle.setAttribute('aria-label', locked ? 'Редактировать svg Пример 2' : 'Закрыть редактирование svg Пример 2');
        }
        setLocked(true);
        toggle.addEventListener('click', function (ev) {
            ev.stopPropagation();
            const wasLocked = toggle.classList.contains('rewrite-lock-toggle--locked');
            if (wasLocked) {
                setSvgExample2Collapsed(wrap, false);
            }
            setLocked(!wasLocked);
            if (!wasLocked) {
                saveLaterPrefs(wrap)
                    .then(function () {
                        appendLog(wrap, 'svg Пример 2 сохранён на сервере');
                    })
                    .catch(function (e) {
                        appendLog(wrap, 'Ошибка сохранения примера 2: ' + String(e.message || e));
                    });
            } else {
                ta.focus();
            }
        });
    }

    function setSvgExample2Collapsed(wrap, collapsed) {
        setLaterCollapsible(
            wrap.querySelector('[data-later-svg-example2-collapse]'),
            wrap.querySelector('[data-later-svg-example2-collapse-toggle]'),
            wrap.querySelector('[data-later-svg-example2-body]'),
            collapsed
        );
    }

    function bindSvgExample2Collapse(wrap) {
        bindLaterCollapsible(
            wrap,
            '[data-later-svg-example2-collapse]',
            '[data-later-svg-example2-collapse-toggle]',
            '[data-later-svg-example2-body]',
            true
        );
    }

    function bindSvgPrompt2Lock(wrap) {
        const toggle = wrap.querySelector('[data-later-svg-prompt2-toggle]');
        const ta = wrap.querySelector('[data-later-svg-prompt2]');
        if (!toggle || !ta) return;
        function setLocked(locked) {
            toggle.classList.toggle('rewrite-lock-toggle--locked', locked);
            ta.readOnly = locked;
            ta.classList.toggle('rewrite-source-textarea--locked', locked);
            toggle.title = locked ? 'Редактировать' : 'Сохранить';
            toggle.setAttribute('aria-label', locked ? 'Редактировать svg промт 2' : 'Закрыть редактирование svg промт 2');
        }
        setLocked(true);
        toggle.addEventListener('click', function (ev) {
            ev.stopPropagation();
            const wasLocked = toggle.classList.contains('rewrite-lock-toggle--locked');
            if (wasLocked) {
                setSvgPrompt2Collapsed(wrap, false);
            }
            setLocked(!wasLocked);
            if (!wasLocked) {
                saveLaterPrefs(wrap)
                    .then(function () {
                        appendLog(wrap, 'svg промт 2 сохранён на сервере');
                    })
                    .catch(function (e) {
                        appendLog(wrap, 'Ошибка сохранения промта 2: ' + String(e.message || e));
                    });
            } else {
                ta.focus();
            }
        });
    }

    function bindSvgPromptLock(wrap) {
        const toggle = wrap.querySelector('[data-later-svg-prompt-toggle]');
        const ta = wrap.querySelector('[data-later-svg-prompt]');
        if (!toggle || !ta) return;
        function setLocked(locked) {
            toggle.classList.toggle('rewrite-lock-toggle--locked', locked);
            ta.readOnly = locked;
            ta.classList.toggle('rewrite-source-textarea--locked', locked);
            toggle.title = locked ? 'Редактировать' : 'Сохранить';
            toggle.setAttribute('aria-label', locked ? 'Редактировать svg промт' : 'Закрыть редактирование svg промт');
        }
        setLocked(true);
        toggle.addEventListener('click', function (ev) {
            ev.stopPropagation();
            const wasLocked = toggle.classList.contains('rewrite-lock-toggle--locked');
            if (wasLocked) {
                setSvgPromptCollapsed(wrap, false);
            }
            setLocked(!wasLocked);
            if (!wasLocked) {
                saveLaterPrefs(wrap)
                    .then(function () {
                        appendLog(wrap, 'svg промт сохранён на сервере');
                    })
                    .catch(function (e) {
                        appendLog(wrap, 'Ошибка сохранения промта: ' + String(e.message || e));
                    });
            } else {
                ta.focus();
            }
        });
    }

    function bindEditorPromptLock(wrap) {
        const toggle = wrap.querySelector('[data-later-editor-prompt-toggle]');
        const ta = wrap.querySelector('[data-later-editor-prompt]');
        const block = wrap.querySelector('[data-later-editor-prompt-collapse]');
        const collapseBtn = wrap.querySelector('[data-later-editor-prompt-collapse-toggle]');
        const body = wrap.querySelector('[data-later-editor-prompt-body]');
        if (!toggle || !ta) return;

        function setLocked(locked) {
            toggle.classList.toggle('rewrite-lock-toggle--locked', locked);
            ta.readOnly = locked;
            ta.classList.toggle('rewrite-source-textarea--locked', locked);
            toggle.title = locked ? 'Редактировать' : 'Сохранить';
            toggle.setAttribute('aria-label', locked ? 'Редактировать промт редактор' : 'Закрыть редактирование промт редактор');
        }
        setLocked(true);
        toggle.addEventListener('click', function (ev) {
            ev.stopPropagation();
            const wasLocked = toggle.classList.contains('rewrite-lock-toggle--locked');
            if (wasLocked) {
                setLaterCollapsible(block, collapseBtn, body, false);
            }
            setLocked(!wasLocked);
            if (!wasLocked) {
                saveLaterPrefs(wrap)
                    .then(function () {
                        appendLog(wrap, 'промт редактор сохранён на сервере');
                    })
                    .catch(function (e) {
                        appendLog(wrap, 'Ошибка сохранения: ' + String(e.message || e));
                    });
            } else {
                ta.focus();
            }
        });
    }

    function bindAnimPromptCollapse(wrap) {
        bindLaterCollapsible(
            wrap,
            '[data-later-anim-prompt-collapse]',
            '[data-later-anim-prompt-collapse-toggle]',
            '[data-later-anim-prompt-body]',
            true
        );
    }

    function bindAnimPromptLock(wrap) {
        const toggle = wrap.querySelector('[data-later-anim-prompt-toggle]');
        const ta = wrap.querySelector('[data-later-anim-prompt]');
        const block = wrap.querySelector('[data-later-anim-prompt-collapse]');
        const collapseBtn = wrap.querySelector('[data-later-anim-prompt-collapse-toggle]');
        const body = wrap.querySelector('[data-later-anim-prompt-body]');
        if (!toggle || !ta) return;

        function setLocked(locked) {
            toggle.classList.toggle('rewrite-lock-toggle--locked', locked);
            ta.readOnly = locked;
            ta.classList.toggle('rewrite-source-textarea--locked', locked);
            toggle.title = locked ? 'Редактировать' : 'Сохранить';
            toggle.setAttribute(
                'aria-label',
                locked ? 'Редактировать промт Анимация' : 'Закрыть редактирование промт Анимация'
            );
        }
        setLocked(true);
        toggle.addEventListener('click', function (ev) {
            ev.stopPropagation();
            const wasLocked = toggle.classList.contains('rewrite-lock-toggle--locked');
            if (wasLocked) {
                setLaterCollapsible(block, collapseBtn, body, false);
            }
            setLocked(!wasLocked);
            if (!wasLocked) {
                scheduleSaveLaterPrefs(wrap, 0);
                saveLaterPrefs(wrap)
                    .then(function () {
                        appendLog(wrap, 'промт Анимация сохранён на сервере');
                    })
                    .catch(function (e) {
                        appendLog(wrap, 'Ошибка сохранения: ' + String(e.message || e));
                    });
            } else {
                ta.focus();
            }
        });
    }

    function bindAnimAnswerCollapse(wrap) {
        bindLaterCollapsible(
            wrap,
            '[data-later-anim-answer-collapse]',
            '[data-later-anim-answer-collapse-toggle]',
            '[data-later-anim-answer-body]',
            true
        );
    }

    function bindFixlogAnswerCollapse(wrap) {
        bindLaterCollapsible(
            wrap,
            '[data-later-fixlog-collapse]',
            '[data-later-fixlog-collapse-toggle]',
            '[data-later-fixlog-body]',
            true
        );
    }

    function restoreFormFields(wrap, data, opts) {
        const skipImage = !!(opts && opts.skipImage);
        const svgPromptEl = wrap.querySelector('[data-later-svg-prompt]');
        const svgPrompt2El = wrap.querySelector('[data-later-svg-prompt2]');
        const svgExample2El = wrap.querySelector('[data-later-svg-example2]');
        const imgPromptEl = wrap.querySelector('[data-later-editor-prompt]');
        const descEl = wrap.querySelector('[data-later-scene-description]');
        const durEl = wrap.querySelector('[data-later-scene-duration]');
        const modelEl = wrap.querySelector('[data-later-model]');
        const preview = wrap.querySelector('[data-later-preview]');
        const toggle = wrap.querySelector('[data-later-svg-prompt-toggle]');
        if (svgPromptEl) {
            const tpl = (data.svg_prompt || '').trim();
            if (tpl) {
                svgPromptEl.value = tpl;
            } else if (data.user_prompt) {
                svgPromptEl.value = data.user_prompt;
            }
        }
        if (svgPrompt2El && (data.svg_prompt_2 || '').trim()) {
            svgPrompt2El.value = data.svg_prompt_2;
        }
        if (svgExample2El && (data.svg_example_2 || '').trim()) {
            svgExample2El.value = data.svg_example_2;
        }
        if (imgPromptEl && ((data.editor_prompt || data.img_1_prompt) || '').trim()) {
            imgPromptEl.value = data.editor_prompt || data.img_1_prompt;
        }
        const animPromptEl = wrap.querySelector('[data-later-anim-prompt]');
        if (animPromptEl && (data.anim_prompt || '').trim()) {
            animPromptEl.value = data.anim_prompt;
        }
        if (descEl && (data.scene_description || '').trim()) {
            descEl.value = data.scene_description;
        }
        if (durEl && (data.scene_duration_sec || '').trim()) {
            durEl.value = data.scene_duration_sec;
        }
        if (modelEl && (data.model || '').trim()) {
            modelEl.value = data.model;
            syncLaterPanelEnabled(wrap);
        }
        if (!skipImage && preview && data.image_url) {
            setLaterAttachedImage(wrap, data.image_url);
        }
        if (toggle && svgPromptEl) {
            svgPromptEl.readOnly = true;
            svgPromptEl.classList.add('rewrite-source-textarea--locked');
            toggle.classList.add('rewrite-lock-toggle--locked');
        }
        const svg2Toggle = wrap.querySelector('[data-later-svg-prompt2-toggle]');
        if (svg2Toggle && svgPrompt2El) {
            svgPrompt2El.readOnly = true;
            svgPrompt2El.classList.add('rewrite-source-textarea--locked');
            svg2Toggle.classList.add('rewrite-lock-toggle--locked');
        }
        const example2Toggle = wrap.querySelector('[data-later-svg-example2-toggle]');
        if (example2Toggle && svgExample2El) {
            svgExample2El.readOnly = true;
            svgExample2El.classList.add('rewrite-source-textarea--locked');
            example2Toggle.classList.add('rewrite-lock-toggle--locked');
        }
        const imgToggle = wrap.querySelector('[data-later-editor-prompt-toggle]');
        if (imgToggle && imgPromptEl) {
            imgPromptEl.readOnly = true;
            imgPromptEl.classList.add('rewrite-source-textarea--locked');
            imgToggle.classList.add('rewrite-lock-toggle--locked');
        }
    }

    function showSlotPreviewImage(preview, url, slotId, savedAt) {
        showSlotPreviewFast(preview, url, slotId, savedAt || '');
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

    async function showPipeline(wrap, data) {
        const parsed = (data && data.parsed) || {};
        const validation = (data && data.validation) || {};
        const banner = wrap.querySelector('[data-later-validation-banner]');
        if (banner) {
            const errs = validation.errors || [];
            const warns = validation.warnings || [];
            if (validation.ok) {
                banner.className = 'later-result__banner is-hidden';
                banner.textContent = '';
            } else {
                let msg = '';
                if (warns.length) msg += warns.join('\n') + '\n\n';
                banner.className = 'later-result__banner later-result__banner--err';
                msg += errs.length
                    ? 'Отклонено:\n' + errs.join('\n')
                    : 'Валидация не пройдена.';
                banner.textContent = msg.trim();
            }
        }
        const svgOut = wrap.querySelector('[data-later-svg-out]');
        const jsonOut = wrap.querySelector('[data-later-json-out]');
        const notesOut = wrap.querySelector('[data-later-notes-out]');
        const svg = parsed.svg || '';
        setRawAnswer(wrap, (data && data.text) || '', { onlyIfEmpty: false });
        if (svgOut) svgOut.value = svg;
        if (jsonOut) {
            jsonOut.value = parsed.animation_raw
                || (parsed.animation ? JSON.stringify(parsed.animation, null, 2) : '');
        }
        if (notesOut) notesOut.value = parsed.notes || '';
        const fixlog = (parsed.fixlog || data.fixlog_text || '').trim();
        const preferSlotEarly =
            (data && data.slot_id)
            || (data && data.img_slot && data.img_slot.slot_id)
            || (data && data.latest_slot_id)
            || getCurrentSlotId(wrap)
            || null;
        if (preferSlotEarly && fixlog) {
            getSlotCarousel(wrap).fixlogCache[preferSlotEarly] = fixlog;
        }
        setFixlogAnswer(wrap, fixlog);
        setFixlogBlockVisible(wrap, Boolean(fixlog));
        if (fixlog) expandFixlogAnswer(wrap);
        const hasAnimation = Boolean(
            (parsed.animation && typeof parsed.animation === 'object')
            || (parsed.animation_raw && String(parsed.animation_raw).trim())
        );
        setRemotionPanelVisible(wrap, Boolean(validation.ok && hasAnimation));
        if (validation.ok && hasAnimation) {
            refreshRemotionInfo(wrap);
        }

        const preferSlot =
            (data && data.slot_id)
            || (data && data.img_slot && data.img_slot.slot_id)
            || (data && data.latest_slot_id)
            || null;
        const st = getSlotCarousel(wrap);
        if (data && data.slots && data.slots.length) {
            st.ids = data.slots.map(function (s) { return s.id; });
            syncSlotMetaFromList(st, data.slots);
            let idx = st.ids.length - 1;
            if (preferSlot) {
                const found = st.ids.indexOf(preferSlot);
                if (found >= 0) idx = found;
            }
            st.index = idx;
            st.cache = {};
            if (data.text) st.textCache[preferSlot] = data.text;
        } else if (validation.ok && preferSlot) {
            await refreshSlotsList(wrap, preferSlot);
        }

        if (validation.ok && preferSlot) {
            const slotSaved = (data.img_slot && data.img_slot.saved_at) || '';
            if (data.img_slot && data.img_slot.preview_url) {
                st.slotMeta[preferSlot] = {
                    preview_url: data.img_slot.preview_url,
                    saved_at: slotSaved,
                };
                preloadImageUrl(data.img_slot.preview_url);
            }
            if (data.text) st.textCache[preferSlot] = data.text;
            displaySlotFrame(
                wrap,
                preferSlot,
                data.img_slot && data.img_slot.preview_url,
                slotSaved
            );
            applySlotText(wrap, preferSlot, data.text || '');
            const viewer = wrap.querySelector('[data-later-slot-viewer]');
            if (viewer) viewer.classList.remove('is-hidden');
            setSlotNavUi(wrap);
        } else if (st.ids.length) {
            await showSlotAt(wrap, st.index);
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
        await showPipeline(wrap, data);
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
            body: JSON.stringify(Object.assign({ text: text }, laterFormPayload(wrap))),
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
            restoreFormFields(wrap, data, { skipImage: true });
            await applyParseResult(wrap, data);
        } catch (e) {
            /* no-op */
        }
    }

    function bindLaterLab(wrap) {
        const sendBtn = wrap.querySelector('[data-later-send]');
        const send2Btn = wrap.querySelector('[data-later-send2]');
        const reparseBtn = wrap.querySelector('[data-later-reparse]');
        const fileInput = wrap.querySelector('[data-later-file]');
        const preview = wrap.querySelector('[data-later-preview]');
        const modelEl = wrap.querySelector('[data-later-model]');
        if (!sendBtn || !KEY_ANY) return;

        if (preview && currentImageUrl(wrap)) {
            setLaterAttachedImage(wrap, currentImageUrl(wrap));
        }

        syncLaterPanelEnabled(wrap);
        bindSvgPromptCollapse(wrap);
        bindSvgPromptLock(wrap);
        bindSvgPrompt2Collapse(wrap);
        bindSvgPrompt2Lock(wrap);
        bindSvgExample2Collapse(wrap);
        bindSvgExample2Lock(wrap);
        bindRawAnswerCollapse(wrap);
        bindEditorPromptCollapse(wrap);
        bindEditorPromptLock(wrap);
        bindAnimPromptCollapse(wrap);
        bindAnimPromptLock(wrap);
        bindAnimAnswerCollapse(wrap);
        bindFixlogAnswerCollapse(wrap);
        modelEl?.addEventListener('change', function () {
            syncLaterPanelEnabled(wrap);
            saveLaterPrefs(wrap).catch(function () { /* ignore */ });
        });

        const clearBtn = wrap.querySelector('[data-later-clear]');
        clearBtn?.addEventListener('click', async function () {
            if (
                !window.confirm(
                    'Удалить все кадры (img_N), ответы модели и remotion?\n\n'
                        + 'Сохранятся: фото, все промты, описание сцены, хронометраж и модель.'
                )
            ) {
                return;
            }
            clearBtn.disabled = true;
            setOpStatus(wrap, 'Очистка…', 'generating');
            try {
                const r = await fetch('/scenes-lab/api/clear', { method: 'POST' });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || 'Ошибка очистки');
                }
                await clearLabWorkspace(wrap);
                const n = Array.isArray(data.deleted_slots) ? data.deleted_slots.length : 0;
                setOpStatus(wrap, 'Очищено — удалено кадров: ' + n, 'ok');
            } catch (e) {
                setOpStatus(wrap, String(e.message || e), 'error');
            } finally {
                clearBtn.disabled = false;
            }
        });

        const descEl = wrap.querySelector('[data-later-scene-description]');
        const durEl = wrap.querySelector('[data-later-scene-duration]');
        descEl?.addEventListener('input', function () {
            scheduleSaveLaterPrefs(wrap);
        });
        descEl?.addEventListener('change', function () {
            scheduleSaveLaterPrefs(wrap);
        });
        durEl?.addEventListener('input', function () {
            scheduleSaveLaterPrefs(wrap);
        });
        durEl?.addEventListener('change', function () {
            scheduleSaveLaterPrefs(wrap);
        });

        bindSvgPatch(wrap);
        loadLaterPrefs(wrap).then(function () {
            return refreshSlotsList(wrap);
        }).then(function () {
            const st = getSlotCarousel(wrap);
            if (st.ids.length) {
                showSlotAt(wrap, st.index).catch(function () { /* ignore */ });
            }
            return loadSavedSession(wrap);
        });

        function goSlotByDelta(wrap, delta) {
            if (isSlotAnimLocked(wrap)) return;
            const st = getSlotCarousel(wrap);
            if (!st.ids.length) return;
            const next = Math.max(0, Math.min(st.index + delta, st.ids.length - 1));
            if (next === st.index) return;
            showSlotAt(wrap, next);
        }

        wrap.querySelector('[data-later-slot-prev]')?.addEventListener('click', function () {
            goSlotByDelta(wrap, -1);
        });
        wrap.querySelector('[data-later-slot-next]')?.addEventListener('click', function () {
            goSlotByDelta(wrap, 1);
        });

        const animateBtn = wrap.querySelector('[data-later-animate]');
        animateBtn?.addEventListener('click', async function () {
            const mid = modelEl ? modelEl.value : '';
            if (!modelKeyOk(mid)) {
                setAnimStatus(wrap, 'Нужен API-ключ для выбранной модели', 'error');
                appendLog(wrap, 'Ошибка анимации: нет API-ключа');
                return;
            }
            const slotId = getCurrentSlotId(wrap);
            if (!slotId) {
                setAnimStatus(wrap, 'Сначала соберите кадр', 'error');
                return;
            }
            const ap = getAnimPromptTemplate(wrap);
            if (!ap) {
                setAnimStatus(wrap, 'Заполните промт Анимация', 'error');
                return;
            }
            animateBtn.disabled = true;
            const busyLabel = animateBtn.textContent;
            animateBtn.textContent = 'Анимировать…';
            const animBanner = wrap.querySelector('[data-later-anim-validation-banner]');
            if (animBanner) {
                animBanner.className = 'later-result__banner is-hidden';
                animBanner.textContent = '';
            }
            appendLog(wrap, 'Анимировать ' + slotId + ': подготовка…');
            setSlotAnimLock(wrap, true);
            setAnimStatus(wrap, 'Отправка на сервер…', 'generating');
            try {
                const slotResponse = await getSlotResponseText(wrap, slotId);
                const payload = laterFormPayload(wrap);
                payload.slot_id = slotId;
                payload.target_slot = slotId;
                payload.slot_response = slotResponse;
                appendLog(
                    wrap,
                    '→ POST /scenes-lab/api/animate (слот ' + slotId + ', ответ ' + slotResponse.length + ' симв.)'
                );
                setAnimStatus(wrap, 'Запрос к модели… (обычно 1–2 мин)', 'generating');
                const r = await fetch('/scenes-lab/api/animate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    if (data.anim_text) await applyAnimResult(wrap, data);
                    throw new Error((data && data.error) || 'Ошибка анимации (HTTP ' + r.status + ')');
                }
                await applyAnimResult(wrap, data);
                appendLog(wrap, 'Анимация для ' + slotId + ' — валидация ОК');
                setAnimStatus(wrap, 'Готово — валидация анимации ОК', 'ok');
            } catch (e) {
                const msg = String(e.message || e);
                appendLog(wrap, 'Ошибка анимации: ' + msg);
                setAnimStatus(wrap, msg, 'error');
            } finally {
                setSlotAnimLock(wrap, false);
                animateBtn.textContent = busyLabel;
                setSlotNavUi(wrap);
            }
        });

        const remakeBtn = wrap.querySelector('[data-later-remake]');
        remakeBtn?.addEventListener('click', async function () {
            const mid = modelEl ? modelEl.value : '';
            if (!modelKeyOk(mid)) {
                setOpStatus(wrap, 'Нужен API-ключ для выбранной модели', 'error');
                appendLog(wrap, 'Ошибка: нет API-ключа');
                return;
            }
            const st = getSlotCarousel(wrap);
            if (!st.ids.length) {
                setOpStatus(wrap, 'Сначала соберите img_1', 'error');
                appendLog(wrap, 'Ошибка: нет кадра на сервере');
                return;
            }
            const ep = getEditorPromptTemplate(wrap);
            if (!ep) {
                setOpStatus(wrap, 'Заполните промт редактор', 'error');
                appendLog(wrap, 'Ошибка: пустой промт редактор');
                return;
            }
            remakeBtn.disabled = true;
            remakeBtn.classList.add('later-remake--busy');
            const busyLabel = remakeBtn.textContent;
            remakeBtn.textContent = 'Переделать…';
            appendLog(wrap, 'Переделать: подготовка запроса…');
            setOpStatus(wrap, 'Отправка на сервер…', 'generating');
            try {
                await refreshSlotsList(wrap);
                const latestId = getLatestSlotId(wrap);
                const payload = laterFormPayload(wrap);
                delete payload.target_slot;
                appendLog(
                    wrap,
                    '→ POST /scenes-lab/api/remake (последний слот на сервере: '
                        + (latestId || '?') + ')'
                );
                setOpStatus(wrap, 'Запрос к модели… (обычно 1–3 мин)', 'generating');
                const r = await fetch('/scenes-lab/api/remake', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                appendLog(wrap, '← ответ сервера: HTTP ' + r.status);
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || 'Ошибка переделать (HTTP ' + r.status + ')');
                }
                setOpStatus(wrap, 'Сохранение ' + (data.slot_id || 'слота') + '…', 'generating');
                appendLog(wrap, 'Модель ответила — разбор SVG…');
                const st2 = getSlotCarousel(wrap);
                st2.cache = {};
                if (data.text && data.slot_id) {
                    st2.textCache[data.slot_id] = data.text;
                }
                if (data.fixlog_text && data.slot_id) {
                    st2.fixlogCache[data.slot_id] = data.fixlog_text;
                }
                await refreshSlotsList(wrap, data.slot_id);
                await applyParseResult(wrap, data);
                appendLog(wrap, 'Готово: сохранено как ' + (data.slot_id || '?'));
                setOpStatus(wrap, 'Готово — ' + (data.slot_id || 'новый слот'), 'ok');
            } catch (e) {
                const msg = String(e.message || e);
                appendLog(wrap, 'Ошибка: ' + msg);
                setOpStatus(wrap, msg, 'error');
            } finally {
                remakeBtn.disabled = false;
                remakeBtn.classList.remove('later-remake--busy');
                remakeBtn.textContent = busyLabel;
            }
        });

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
                const attached = setLaterAttachedImage(wrap, data.image_url);
                setLaterStatus(wrap, 'Фото загружено', 'done');
                appendLog(wrap, 'Фото прикреплено: ' + laterImageFilename(attached));
                await saveLaterPrefs(wrap);
            } catch (e) {
                if (token !== uploadToken) return;
                setLaterStatus(wrap, String(e.message || e), 'error');
            }
        });

        async function submitLaterGeneration(wrap, opts) {
            const mode = opts && opts.mode === 'dual_prompt' ? 'dual_prompt' : 'photo';
            const btn = mode === 'dual_prompt' ? send2Btn : sendBtn;
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
            const payload = laterFormPayload(wrap);
            payload.send_mode = mode;
            if (mode === 'photo') {
                const imageUrl = currentImageUrl(wrap);
                if (!imageUrl) {
                    setLaterStatus(wrap, 'Прикрепите фото перед отправкой', 'error');
                    return;
                }
                payload.image_url = imageUrl;
                appendLog(wrap, '→ фото в запросе: ' + laterImageFilename(imageUrl));
            } else {
                if (!getSvgPrompt2Template(wrap)) {
                    setLaterStatus(wrap, 'Заполните svg промт 2', 'error');
                    return;
                }
                if (!getSvgExample2Template(wrap)) {
                    setLaterStatus(wrap, 'Заполните svg Пример 2', 'error');
                    return;
                }
                delete payload.image_url;
            }
            clearPipelineUi(wrap);
            appendLog(wrap, mode === 'dual_prompt' ? 'Отправить 2: подготовка…' : 'Отправить: подготовка…');
            if (btn) btn.disabled = true;
            setOpStatus(wrap, 'Отправка на сервер…', 'generating');
            try {
                appendLog(wrap, '→ POST /scenes-lab/api/claude');
                setOpStatus(wrap, 'Запрос к модели… (обычно 1–3 мин)', 'generating');
                const r = await fetch('/scenes-lab/api/claude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                appendLog(wrap, '← ответ сервера: HTTP ' + r.status);
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || 'Ошибка запроса');
                }
                setOpStatus(wrap, 'Разбор ответа…', 'generating');
                await applyParseResult(wrap, data);
                appendLog(wrap, 'Готово: ответ получен');
                setOpStatus(wrap, 'Готово — ответ модели', 'ok');
            } catch (e) {
                const msg = String(e.message || e);
                appendLog(wrap, 'Ошибка: ' + msg);
                setOpStatus(wrap, msg, 'error');
            } finally {
                if (btn) btn.disabled = false;
            }
        }

        sendBtn.addEventListener('click', function () {
            submitLaterGeneration(wrap, { mode: 'photo' });
        });
        send2Btn?.addEventListener('click', function () {
            submitLaterGeneration(wrap, { mode: 'dual_prompt' });
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
                const slotId = getCurrentSlotId(wrap);
                if (slotId) await ensureSlotSvgInPipeline(wrap, slotId);
                const r = await fetch('/scenes-lab/api/remotion-props', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(laterRemotionPayload(wrap)),
                });
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
                const slotId = getCurrentSlotId(wrap);
                if (slotId) await ensureSlotSvgInPipeline(wrap, slotId);
                const r = await fetch('/scenes-lab/api/remotion/render', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(laterRemotionPayload(wrap)),
                });
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
                appendLog(wrap, 'Проверка и сборка SVG…');
                runReparseFromRaw(wrap);
            }
        });
    }

    document.querySelectorAll('[data-later-lab]').forEach(bindLaterLab);
})();
