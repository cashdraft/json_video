(function () {
    'use strict';

    var modal = document.getElementById('rewrite-template-editor-modal');
    if (!modal) return;

    var apiBase = (modal.getAttribute('data-templates-api') || '').replace(/\/$/, '');
    var elevenlabsKeySet = modal.getAttribute('data-elevenlabs-key') === '1';

    var titleEl = document.getElementById('rewrite-template-editor-title');
    var nameEl = document.getElementById('rte-name');
    var descEl = document.getElementById('rte-description');
    var logoFileEl = document.getElementById('rte-logo-file');
    var logoHit = document.getElementById('rte-logo-hit');
    var logoPreview = document.getElementById('rte-logo-preview');
    var logoWrap = document.getElementById('rte-logo-preview-wrap');
    var logoPlaceholder = document.getElementById('rte-logo-placeholder');
    var statusEl = document.getElementById('rewrite-template-editor-status');
    var deleteBtn = document.getElementById('rewrite-template-editor-delete');
    var deleteModal = document.getElementById('rewrite-template-editor-delete-modal');
    var deleteLeadEl = document.getElementById('rewrite-template-editor-delete-lead');
    var deleteOkBtn = document.getElementById('rewrite-template-editor-delete-ok');
    var saveQueue = Promise.resolve();

    var ttsSettingsToggle = document.getElementById('rte-tts-settings-toggle');
    var ttsSpeakerBoostEl = document.getElementById('rte-tts-speaker-boost');
    var ttsSettingsRangeIds = ['rte-tts-speed', 'rte-tts-stability', 'rte-tts-similarity', 'rte-tts-style'];
    var ttsSettingsLocked = true;

    var DEFAULT_TEMPLATE_NAME = 'Template';

    function resolveTemplateName(raw) {
        var n = String(raw || '').trim();
        return n || DEFAULT_TEMPLATE_NAME;
    }

    var state = {
        name: '',
        protected: false,
        saving: false,
        initialStages: {},
        initialTts: {},
    };

    function templateUrl(name) {
        return apiBase + '/' + encodeURIComponent(name);
    }

    function templateIsProtected(name) {
        return String(name || '').trim().toLowerCase() === 'base template';
    }

    function syncDeleteBtnState() {
        if (!deleteBtn) return;
        deleteBtn.disabled = !state.name || state.protected;
    }

    function openDeleteConfirmModal() {
        if (!deleteModal || !state.name || state.protected) return;
        var displayName = (nameEl && String(nameEl.value || '').trim()) || state.name;
        if (deleteLeadEl) {
            deleteLeadEl.textContent = 'Вы точно уверены? Удалить шаблон «' + displayName + '»';
        }
        deleteModal.classList.remove('hidden');
        deleteModal.setAttribute('aria-hidden', 'false');
    }

    function closeDeleteConfirmModal() {
        if (!deleteModal) return;
        deleteModal.classList.add('hidden');
        deleteModal.setAttribute('aria-hidden', 'true');
    }

    async function confirmDeleteTemplate() {
        if (!state.name || state.protected) return;
        var name = state.name;
        if (deleteOkBtn) deleteOkBtn.disabled = true;
        try {
            await queueSaveTemplate();
            var r = await fetch(templateUrl(name), { method: 'DELETE' });
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok) {
                throw new Error((data && (data.message || data.error)) || 'Не удалось удалить шаблон');
            }
            closeDeleteConfirmModal();
            modal.classList.add('hidden');
            modal.setAttribute('aria-hidden', 'true');
            state.name = '';
            setStatus('', false);
            if (typeof window.refreshRewriteTemplatePicker === 'function') {
                await window.refreshRewriteTemplatePicker('Base Template');
            }
        } catch (e) {
            setStatus(String(e && e.message ? e.message : e), true);
        } finally {
            if (deleteOkBtn) deleteOkBtn.disabled = false;
        }
    }

    function elevenlabsVoicesUrl() {
        return apiBase.replace(/\/templates\/?$/, '') + '/elevenlabs/voices';
    }

    function defaultTtsModelId() {
        var fromState = state.initialTts && state.initialTts.model_id;
        if (fromState) return String(fromState);
        return modal.getAttribute('data-default-tts-model') || '';
    }

    function voiceHueFromId(id) {
        var h = 0;
        var s = String(id || '');
        for (var i = 0; i < s.length; i++) {
            h = ((h << 5) - h + s.charCodeAt(i)) | 0;
        }
        return String(Math.abs(h) % 360);
    }

    function voiceOptMeta(opt) {
        if (!opt) return null;
        return {
            title: opt.dataset.voiceTitle || String(opt.textContent || '').trim(),
            hue: opt.dataset.voiceHue || voiceHueFromId(opt.value),
            fullName: String(opt.textContent || opt.dataset.voiceTitle || '').trim(),
        };
    }

    function appendVoiceAvatar(parent, meta) {
        var av = document.createElement('span');
        av.className = 'tts-voice-option-avatar';
        av.setAttribute('aria-hidden', 'true');
        av.textContent = (meta.title || '?').charAt(0).toUpperCase();
        av.style.setProperty('--voice-hue', meta.hue);
        parent.appendChild(av);
    }

    function renderVoiceBtnLabel(lbl, meta) {
        if (!lbl || !meta) return;
        lbl.classList.add('rewrite-anim-dropdown__btn-label--voice');
        lbl.textContent = '';
        var wrap = document.createElement('span');
        wrap.className = 'tts-voice-btn-preview';
        appendVoiceAvatar(wrap, meta);
        var text = document.createElement('span');
        text.className = 'tts-voice-btn-preview__text';
        var t = document.createElement('span');
        t.className = 'tts-voice-btn-preview__title';
        t.textContent = meta.fullName || meta.title;
        text.appendChild(t);
        wrap.appendChild(text);
        lbl.appendChild(wrap);
    }

    function appendVoiceOptionPreview(parent, meta) {
        var wrap = document.createElement('span');
        wrap.className = 'tts-voice-btn-preview';
        appendVoiceAvatar(wrap, meta);
        var text = document.createElement('span');
        text.className = 'tts-voice-btn-preview__text';
        var t = document.createElement('span');
        t.className = 'tts-voice-btn-preview__title';
        t.textContent = meta.fullName || meta.title;
        text.appendChild(t);
        wrap.appendChild(text);
        parent.appendChild(wrap);
    }

    function closeRteVoicePanelsExcept(except) {
        modal.querySelectorAll('[data-rte-model-field] .rewrite-anim-dropdown__panel').forEach(function (p) {
            if (p === except) return;
            p.hidden = true;
            var b = p.parentElement && p.parentElement.querySelector('.rewrite-anim-dropdown__btn');
            if (b) b.setAttribute('aria-expanded', 'false');
        });
    }

    function syncTtsCompactSummary() {
        var compact = modal.querySelector('[data-rte-tts-compact-voice]');
        if (!compact) return;
        var lbl = modal.querySelector('[data-rte-model-label="tts-voice"]');
        var voiceSel = document.getElementById('rte-tts-voice');
        var txt = '';
        if (lbl) {
            var titleEl = lbl.querySelector('.tts-voice-btn-preview__title');
            txt = titleEl ? titleEl.textContent : lbl.textContent;
        }
        if (!String(txt || '').trim() && voiceSel && voiceSel.selectedIndex >= 0) {
            txt = voiceSel.options[voiceSel.selectedIndex].textContent || '';
        }
        var s = String(txt || '').trim();
        if (s === 'Загрузка голосов…' || s === 'Загрузка…') s = '—';
        compact.textContent = s || '—';
    }

    function initRteVoiceDropdown() {
        var field = modal.querySelector('[data-rte-model-field="tts-voice"]');
        var select = document.getElementById('rte-tts-voice');
        if (!field || !select) return;
        var btn = field.querySelector('.rewrite-anim-dropdown__btn');
        var panel = field.querySelector('.rewrite-anim-dropdown__panel');
        var radioName = field.getAttribute('data-rte-model-radio-name') || 'rte-tts-voice-radio';
        var radioSel = 'input[name="' + radioName + '"]';
        var lbl = field.querySelector('[data-rte-model-label="tts-voice"]');
        function syncSelectFromRadios() {
            var checked = field.querySelector(radioSel + ':checked');
            if (checked) select.value = checked.value;
        }
        function updateBtnLabel() {
            if (!lbl) return;
            var checked = field.querySelector(radioSel + ':checked');
            var selOpt = select.options[select.selectedIndex];
            if (checked) {
                selOpt = Array.from(select.options).find(function (o) {
                    return o.value === checked.value;
                }) || selOpt;
            }
            var meta = voiceOptMeta(selOpt);
            if (meta && meta.title) {
                renderVoiceBtnLabel(lbl, meta);
                syncTtsCompactSummary();
                return;
            }
            if (checked) {
                lbl.textContent = String(checked.value || '');
                syncTtsCompactSummary();
                return;
            }
            var opt = select.options[select.selectedIndex];
            if (opt) lbl.textContent = String(opt.textContent || '').trim();
            syncTtsCompactSummary();
        }
        function closePanel() {
            if (panel) panel.hidden = true;
            if (btn) btn.setAttribute('aria-expanded', 'false');
        }
        function rebuildPanelFromSelect() {
            if (!panel) return;
            var selectedVal = select.value;
            panel.innerHTML = '';
            Array.from(select.options).forEach(function (opt) {
                if (!String(opt.value || '').trim() && !String(opt.textContent || '').trim()) return;
                var meta = voiceOptMeta(opt);
                var lab = document.createElement('label');
                lab.className = 'rewrite-anim-option rewrite-anim-option--radio rewrite-global-model-option rewrite-anim-option--voice';
                var inp = document.createElement('input');
                inp.type = 'radio';
                inp.name = radioName;
                inp.className = 'rewrite-anim-radio visually-hidden-input';
                inp.value = opt.value;
                inp.checked = opt.selected || opt.value === selectedVal;
                lab.appendChild(inp);
                if (meta) appendVoiceOptionPreview(lab, meta);
                panel.appendChild(lab);
            });
            syncSelectFromRadios();
            updateBtnLabel();
        }
        field._rteModelRebuild = rebuildPanelFromSelect;
        rebuildPanelFromSelect();
        if (btn) btn.disabled = !!select.disabled;
        if (!field.dataset.rteModelBound) {
            field.dataset.rteModelBound = '1';
            if (btn && panel) {
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    if (btn.disabled) return;
                    if (!panel.hidden) closePanel();
                    else {
                        closeRteVoicePanelsExcept(panel);
                        panel.hidden = false;
                        btn.setAttribute('aria-expanded', 'true');
                    }
                });
                panel.addEventListener('click', function (e) { e.stopPropagation(); });
            }
            field.addEventListener('change', function (e) {
                var rb = e.target;
                if (!rb || rb.name !== radioName) return;
                var prev = select.value;
                select.value = rb.value;
                updateBtnLabel();
                closePanel();
                select.dispatchEvent(new Event('change', { bubbles: true }));
                if (select.value !== prev && !modal.classList.contains('hidden') && state.name) {
                    void queueSaveTemplateAndNotify();
                }
            });
            select.addEventListener('change', function () {
                var val = select.value;
                field.querySelectorAll(radioSel).forEach(function (r) {
                    r.checked = r.value === val;
                });
                updateBtnLabel();
                if (btn) btn.disabled = !!select.disabled;
            });
        }
    }

    function rebuildRteVoiceDropdown() {
        var field = modal.querySelector('[data-rte-model-field="tts-voice"]');
        if (field && typeof field._rteModelRebuild === 'function') field._rteModelRebuild();
    }

    function syncRteVoiceDropdownDisabled() {
        var select = document.getElementById('rte-tts-voice');
        var field = modal.querySelector('[data-rte-model-field="tts-voice"]');
        var btn = field && field.querySelector('.rewrite-anim-dropdown__btn');
        if (select && btn) btn.disabled = !!select.disabled;
    }

    function setStatus(msg, isErr, kind) {
        if (!statusEl) return;
        var text = msg || '';
        var has = !!String(text).trim();
        statusEl.textContent = text;
        statusEl.classList.toggle('hidden', !has);
        statusEl.classList.remove('tts-template-status--error', 'tts-template-status--ok');
        if (!has) return;
        if (isErr) {
            statusEl.classList.add('tts-template-status--error');
        } else if (kind === 'ok') {
            statusEl.classList.add('tts-template-status--ok');
        }
    }

    function speedPctToLabel(pct) {
        var p = Math.max(0, Math.min(100, parseInt(pct, 10) || 0));
        return (0.25 + (p / 100) * 3.75).toFixed(2) + '×';
    }

    function bindSpeedSlider() {
        var speed = document.getElementById('rte-tts-speed');
        var speedVal = document.getElementById('rte-tts-speed-val');
        if (!speed || !speedVal) return;
        function sync() {
            speedVal.textContent = speedPctToLabel(speed.value);
        }
        speed.addEventListener('input', sync);
        sync();
    }

    function bindPctSlider(id, valId) {
        var sl = document.getElementById(id);
        var vl = document.getElementById(valId);
        if (!sl || !vl) return;
        function sync() {
            vl.textContent = String(sl.value);
        }
        sl.addEventListener('input', sync);
        sync();
    }

    function syncMetaBadge(block) {
        if (!block) return;
        var field = block.getAttribute('data-rte-meta');
        var badge = block.querySelector('[data-rte-meta-badge]');
        var input = field === 'name' ? nameEl : (field === 'description' ? descEl : null);
        if (!badge || !input) return;
        var has = (input.value || '').trim().length > 0;
        badge.classList.toggle('badge-yes', has);
        badge.classList.toggle('badge-no', !has);
    }

    function lockMetaField(input, toggle, locked) {
        if (!input || !toggle) return;
        if (state.protected && input === nameEl) {
            styleToggleBtn(toggle, true);
            input.readOnly = true;
            input.classList.add('rewrite-source-textarea--locked');
            toggle.disabled = true;
            return;
        }
        toggle.disabled = false;
        if (locked) {
            styleToggleBtn(toggle, true);
            input.readOnly = true;
            input.classList.add('rewrite-source-textarea--locked');
        } else {
            styleToggleBtn(toggle, false);
            input.readOnly = false;
            input.classList.remove('rewrite-source-textarea--locked');
            input.focus();
        }
    }

    function wireMetaBlocks() {
        modal.querySelectorAll('[data-rte-meta]').forEach(function (block) {
            var key = block.getAttribute('data-rte-meta');
            var input = key === 'name' ? nameEl : (key === 'description' ? descEl : null);
            var toggle = key === 'name'
                ? document.getElementById('rte-name-toggle')
                : (key === 'description' ? document.getElementById('rte-description-toggle') : null);
            if (!input || !toggle || toggle.dataset.rteMetaWired === '1') return;
            toggle.dataset.rteMetaWired = '1';
            lockMetaField(input, toggle, true);
            syncMetaBadge(block);
            toggle.addEventListener('click', function () {
                if (state.protected && input === nameEl) return;
                var wasLocked = input.readOnly;
                lockMetaField(input, toggle, !wasLocked);
                if (!wasLocked) void queueSaveTemplateAndNotify();
                else input.focus();
            });
            input.addEventListener('input', function () {
                syncMetaBadge(block);
            });
        });
    }

    function syncFoldCompactBadge(panel, key) {
        if (!panel || !key) return;
        var compact = panel.querySelector('[data-rte-fold-compact="' + key + '"]');
        var block = panel.querySelector('[data-rte-fold-key="' + key + '"]');
        if (!compact || !block) return;
        var badge = block.querySelector('[data-rte-badge]');
        if (!badge) return;
        compact.textContent = badge.textContent;
        compact.classList.toggle('template-prompt-badge--yes', badge.classList.contains('template-prompt-badge--yes'));
        compact.classList.toggle('template-prompt-badge--no', badge.classList.contains('template-prompt-badge--no'));
    }

    function syncAllFoldCompactBadges(panel) {
        if (!panel) return;
        panel.querySelectorAll('[data-rte-fold-key]').forEach(function (block) {
            syncFoldCompactBadge(panel, block.getAttribute('data-rte-fold-key'));
        });
    }

    function syncAllEditorFoldPanels() {
        modal.querySelectorAll('[data-rte-fold-panel]').forEach(syncAllFoldCompactBadges);
    }

    function syncPromptBadge(block) {
        if (!block) return;
        var ta = block.querySelector('[data-rte-field]');
        var badge = block.querySelector('[data-rte-badge]');
        if (!ta || !badge) return;
        var prefix = badge.getAttribute('data-rte-badge-prefix');
        if (!prefix) {
            var parts = badge.textContent.split(':');
            prefix = parts[0].trim();
        }
        var has = (ta.value || '').trim().length > 0;
        badge.textContent = prefix + ': ' + (has ? 'YES' : 'NO');
        badge.classList.toggle('template-prompt-badge--yes', has);
        badge.classList.toggle('template-prompt-badge--no', !has);
        var panel = block.closest('[data-rte-fold-panel]');
        var key = block.getAttribute('data-rte-fold-key');
        if (panel && key) syncFoldCompactBadge(panel, key);
    }

    function setFoldPanelCollapsed(panel, collapsed) {
        if (!panel) return;
        var body = panel.querySelector('.rewrite-template-pisanina-panel__body');
        panel.classList.toggle('rewrite-template-pisanina-panel--body-collapsed', !!collapsed);
        panel.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        if (body) body.hidden = !!collapsed;
    }

    function wireFoldPanels() {
        modal.querySelectorAll('[data-rte-fold-panel]').forEach(function (panel) {
            var header = panel.querySelector('.rewrite-template-pisanina-panel__header');
            var toggle = panel.querySelector('.rewrite-template-pisanina-panel__toggle');
            if (!header || !toggle || header.dataset.rteFoldWired === '1') return;
            header.dataset.rteFoldWired = '1';
            setFoldPanelCollapsed(panel, true);
            function onToggle() {
                setFoldPanelCollapsed(panel, !panel.classList.contains('rewrite-template-pisanina-panel--body-collapsed'));
            }
            header.addEventListener('click', onToggle);
            toggle.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onToggle();
                }
            });
        });
    }

    function styleToggleBtn(btn, locked) {
        if (!btn) return;
        btn.classList.toggle('rewrite-lock-toggle--locked', locked);
        btn.setAttribute('aria-label', locked ? 'Редактировать' : 'Сохранить');
        btn.title = locked ? 'Редактировать' : 'Сохранить';
    }

    function lockPromptField(ta, toggle, block, locked) {
        if (!ta || !toggle) return;
        var collapsible = block && block.getAttribute('data-rte-collapsible') === '1';
        if (locked) {
            styleToggleBtn(toggle, true);
            ta.readOnly = true;
            ta.classList.add('rewrite-source-textarea--locked');
            if (collapsible) block.classList.add('rewrite-template-prompt-block--collapsed');
        } else {
            styleToggleBtn(toggle, false);
            ta.readOnly = false;
            ta.classList.remove('rewrite-source-textarea--locked');
            if (collapsible) block.classList.remove('rewrite-template-prompt-block--collapsed');
        }
    }

    function wirePromptBlocks() {
        modal.querySelectorAll('[data-rte-prompt]').forEach(function (block) {
            var ta = block.querySelector('[data-rte-field]');
            var toggle = block.querySelector('[data-rte-toggle]');
            if (!ta || !toggle || toggle.dataset.rteWired === '1') return;
            toggle.dataset.rteWired = '1';
            var collapsible = block.getAttribute('data-rte-collapsible') === '1';
            lockPromptField(ta, toggle, collapsible ? block : null, true);
            syncPromptBadge(block);
            ta.addEventListener('input', function () { syncPromptBadge(block); });
            toggle.addEventListener('click', function () {
                var wasLocked = toggle.classList.contains('rewrite-lock-toggle--locked');
                lockPromptField(ta, toggle, collapsible ? block : null, !wasLocked);
                if (!wasLocked) void queueSaveTemplateAndNotify();
                else ta.focus();
            });
        });
    }

    function defaultStageCell() {
        return { prompt: '', user_prompt: '', style_prompt: '', past_prompt: '' };
    }

    function readStagesPayload() {
        var stages = JSON.parse(JSON.stringify(state.initialStages || {}));
        function ensure(sk) {
            if (!stages[sk] || typeof stages[sk] !== 'object') stages[sk] = defaultStageCell();
        }
        ensure('rewrite');
        stages.rewrite.prompt = (document.getElementById('rte-rewrite-prompt') || {}).value || '';
        ensure('scene_writer');
        stages.scene_writer.prompt = (document.getElementById('rte-scene-writer-prompt') || {}).value || '';
        stages.scene_writer.style_prompt = (document.getElementById('rte-scene-writer-style') || {}).value || '';
        stages.scene_writer.past_prompt = (document.getElementById('rte-scene-writer-past') || {}).value || '';
        return stages;
    }

    function readTtsPayload() {
        var voiceSel = document.getElementById('rte-tts-voice');
        var voiceId = voiceSel ? (voiceSel.value || '') : '';
        var voiceName = '';
        if (voiceSel && voiceSel.selectedIndex >= 0) {
            voiceName = voiceSel.options[voiceSel.selectedIndex].textContent || '';
        }
        return {
            voice_id: voiceId,
            voice_name: voiceName,
            model_id: defaultTtsModelId(),
            stability_pct: parseInt((document.getElementById('rte-tts-stability') || {}).value, 10) || 50,
            similarity_pct: parseInt((document.getElementById('rte-tts-similarity') || {}).value, 10) || 75,
            style_pct: parseInt((document.getElementById('rte-tts-style') || {}).value, 10) || 0,
            speed_pct: parseInt((document.getElementById('rte-tts-speed') || {}).value, 10) || 20,
            use_speaker_boost: !!(document.getElementById('rte-tts-speaker-boost') || {}).checked,
        };
    }

    function syncRewritePickerLogoPreview(templateName, logoUrl) {
        var n = String(templateName || '').trim();
        if (!n) return;
        var esc = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(n) : n.replace(/"/g, '\\"');
        var opt = document.querySelector('.rewrite-template-option[data-template-name="' + esc + '"]');
        if (!opt) return;
        var frame = opt.querySelector('.template-option-frame');
        if (!frame) return;
        if (!logoUrl) return;
        var visual = frame.querySelector('.template-option-visual');
        if (!visual) return;
        var img = visual.querySelector('.template-logo-preview');
        if (!img) {
            visual.className = 'template-option-visual';
            visual.removeAttribute('aria-hidden');
            visual.textContent = '';
            img = document.createElement('img');
            img.className = 'template-logo-preview';
            img.alt = '';
            img.loading = 'lazy';
            visual.appendChild(img);
        }
        var sep = logoUrl.indexOf('?') >= 0 ? '&' : '?';
        img.src = logoUrl + sep + 't=' + Date.now();
    }

    function setLogoPreview(url) {
        if (!logoPreview || !logoWrap) return;
        if (url) {
            logoPreview.src = url;
            logoPreview.classList.remove('hidden');
            logoWrap.classList.remove('rewrite-template-edit-logo-preview-wrap--empty');
            if (logoPlaceholder) logoPlaceholder.classList.add('hidden');
        } else {
            logoPreview.removeAttribute('src');
            logoPreview.classList.add('hidden');
            logoWrap.classList.add('rewrite-template-edit-logo-preview-wrap--empty');
            if (logoPlaceholder) logoPlaceholder.classList.remove('hidden');
        }
    }

    function applyTtsSettingsLockUi() {
        var locked = ttsSettingsLocked;
        var noKey = !elevenlabsKeySet;
        ttsSettingsRangeIds.forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.disabled = locked || noKey;
        });
        if (ttsSpeakerBoostEl) ttsSpeakerBoostEl.disabled = locked || noKey;
        styleToggleBtn(ttsSettingsToggle, locked);
    }

    function applyTtsDefaults(tts) {
        tts = tts && typeof tts === 'object' ? tts : {};
        state.initialTts = tts;
        var speed = document.getElementById('rte-tts-speed');
        if (speed) speed.value = String(tts.speed_pct != null ? tts.speed_pct : 20);
        var stab = document.getElementById('rte-tts-stability');
        if (stab) stab.value = String(tts.stability_pct != null ? tts.stability_pct : 50);
        var sim = document.getElementById('rte-tts-similarity');
        if (sim) sim.value = String(tts.similarity_pct != null ? tts.similarity_pct : 75);
        var sty = document.getElementById('rte-tts-style');
        if (sty) sty.value = String(tts.style_pct != null ? tts.style_pct : 0);
        var boost = document.getElementById('rte-tts-speaker-boost');
        if (boost) boost.checked = tts.use_speaker_boost !== false;
        ['rte-tts-speed', 'rte-tts-stability', 'rte-tts-similarity', 'rte-tts-style'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.dispatchEvent(new Event('input', { bubbles: true }));
        });
        applyTtsSettingsLockUi();
    }

    async function loadVoices(selectId) {
        var voiceSel = document.getElementById('rte-tts-voice');
        if (!voiceSel || !elevenlabsKeySet) {
            if (voiceSel) voiceSel.innerHTML = '<option value="">—</option>';
            rebuildRteVoiceDropdown();
            syncRteVoiceDropdownDisabled();
            syncTtsCompactSummary();
            return;
        }
        try {
            var r = await fetch(elevenlabsVoicesUrl());
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.voices) {
                voiceSel.innerHTML = '<option value="">Нет голосов</option>';
                voiceSel.disabled = true;
                rebuildRteVoiceDropdown();
                syncRteVoiceDropdownDisabled();
                return;
            }
            var savedId = String(selectId || state.initialTts.voice_id || '');
            voiceSel.innerHTML = '';
            data.voices.forEach(function (v) {
                var opt = document.createElement('option');
                opt.value = v.voice_id || v.id || '';
                opt.textContent = v.name || v.title || opt.value;
                opt.dataset.voiceTitle = v.title || '';
                opt.dataset.voiceHue = voiceHueFromId(opt.value);
                if (savedId && opt.value === savedId) opt.selected = true;
                voiceSel.appendChild(opt);
            });
            voiceSel.disabled = false;
            if (!voiceSel.value && voiceSel.options.length) voiceSel.selectedIndex = 0;
            rebuildRteVoiceDropdown();
            syncRteVoiceDropdownDisabled();
        } catch (_e) {
            voiceSel.innerHTML = '<option value="">Ошибка загрузки</option>';
            voiceSel.disabled = true;
            rebuildRteVoiceDropdown();
            syncRteVoiceDropdownDisabled();
        } finally {
            syncTtsCompactSummary();
        }
    }

    function populateForm(data) {
        var n = String(data.name || state.name || '').trim();
        state.name = n;
        state.protected = n.toLowerCase() === 'base template';
        state.initialStages = data.stages && typeof data.stages === 'object' ? data.stages : {};
        ttsSettingsLocked = true;

        if (titleEl) titleEl.textContent = 'Редактировать шаблон';
        if (nameEl) {
            nameEl.value = n;
            nameEl.title = state.protected ? 'Base Template нельзя переименовать' : '';
        }
        syncDeleteBtnState();
        if (descEl) descEl.value = data.description || '';
        modal.querySelectorAll('[data-rte-meta]').forEach(function (block) {
            var key = block.getAttribute('data-rte-meta');
            var input = key === 'name' ? nameEl : (key === 'description' ? descEl : null);
            var toggle = key === 'name'
                ? document.getElementById('rte-name-toggle')
                : (key === 'description' ? document.getElementById('rte-description-toggle') : null);
            lockMetaField(input, toggle, true);
            syncMetaBadge(block);
        });

        var hero = document.getElementById('rte-hero');
        var master = document.getElementById('rte-master');
        var rw = document.getElementById('rte-rewrite-prompt');
        var sw = document.getElementById('rte-scene-writer-prompt');
        var swStyle = document.getElementById('rte-scene-writer-style');
        var swPast = document.getElementById('rte-scene-writer-past');
        if (hero) hero.value = data.hero_prompt || '';
        if (master) master.value = data.master_prompt || '';
        var st = state.initialStages;
        if (rw) rw.value = (st.rewrite && st.rewrite.prompt) || '';
        if (sw) sw.value = (st.scene_writer && st.scene_writer.prompt) || '';
        if (swStyle) swStyle.value = (st.scene_writer && st.scene_writer.style_prompt) || '';
        if (swPast) swPast.value = (st.scene_writer && st.scene_writer.past_prompt) || '';

        modal.querySelectorAll('[data-rte-prompt]').forEach(function (block) {
            var ta = block.querySelector('[data-rte-field]');
            var toggle = block.querySelector('[data-rte-toggle]');
            var collapsible = block.getAttribute('data-rte-collapsible') === '1';
            lockPromptField(ta, toggle, collapsible ? block : null, true);
            syncPromptBadge(block);
        });
        syncAllEditorFoldPanels();
        modal.querySelectorAll('[data-rte-fold-panel]').forEach(function (panel) {
            setFoldPanelCollapsed(panel, true);
        });

        setLogoPreview(data.logo_url || null);
        applyTtsDefaults(data.tts_defaults || {});
        return loadVoices((data.tts_defaults || {}).voice_id);
    }

    function openModal(name) {
        var n = String(name || '').trim();
        if (!n) return;
        state.name = n;
        state.saving = false;
        syncDeleteBtnState();
        setStatus('Загрузка…', false);
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
        fetch(templateUrl(n), { cache: 'no-store' })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
            .then(function (res) {
                if (!res.ok || !res.data || !res.data.ok) {
                    throw new Error((res.data && (res.data.message || res.data.error)) || 'Не удалось загрузить шаблон');
                }
                return populateForm(res.data);
            })
            .then(function () {
                setStatus('', false);
            })
            .catch(function (e) {
                setStatus(String(e && e.message ? e.message : e), true);
            });
    }

    function lockAllEditableFields() {
        modal.querySelectorAll('[data-rte-prompt]').forEach(function (block) {
            var ta = block.querySelector('[data-rte-field]');
            var toggle = block.querySelector('[data-rte-toggle]');
            var collapsible = block.getAttribute('data-rte-collapsible') === '1';
            if (ta && toggle && !toggle.classList.contains('rewrite-lock-toggle--locked')) {
                lockPromptField(ta, toggle, collapsible ? block : null, true);
                syncPromptBadge(block);
            }
        });
        modal.querySelectorAll('[data-rte-meta]').forEach(function (block) {
            var key = block.getAttribute('data-rte-meta');
            var input = key === 'name' ? nameEl : (key === 'description' ? descEl : null);
            var toggle = key === 'name'
                ? document.getElementById('rte-name-toggle')
                : (key === 'description' ? document.getElementById('rte-description-toggle') : null);
            if (input && toggle && !input.readOnly) {
                lockMetaField(input, toggle, true);
                syncMetaBadge(block);
            }
        });
        if (!ttsSettingsLocked) {
            ttsSettingsLocked = true;
            applyTtsSettingsLockUi();
        }
    }

    function notifyTemplateChanged() {
        if (!state.name) return;
        var toastLogo = '';
        if (logoPreview && !logoPreview.classList.contains('hidden')) {
            toastLogo = logoPreview.getAttribute('src') || '';
        }
        if (typeof window.showRewriteTemplateChangedToast === 'function') {
            window.showRewriteTemplateChangedToast(state.name, toastLogo || null);
        }
    }

    function queueSaveTemplate() {
        saveQueue = saveQueue.then(function () { return saveTemplate(); });
        return saveQueue;
    }

    function queueSaveTemplateAndNotify() {
        return queueSaveTemplate().then(function (ok) {
            if (ok) notifyTemplateChanged();
            return ok;
        });
    }

    async function closeModal() {
        if (modal.classList.contains('hidden')) return;
        try {
            lockAllEditableFields();
            await queueSaveTemplate();
        } catch (e) {
            setStatus(String(e && e.message ? e.message : e), true);
            return;
        }
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
        setStatus('', false);
        state.name = '';
    }

    async function uploadLogoFile(file) {
        if (!file || !state.name) return null;
        var fd = new FormData();
        fd.append('logo', file);
        setStatus('Загружаем логотип…', false);
        var r = await fetch(templateUrl(state.name) + '/logo', { method: 'POST', body: fd });
        var data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            throw new Error((data && (data.error || data.message)) || 'Не удалось сохранить логотип');
        }
        if (data.logo_url) {
            var logoUrl = data.logo_url + (data.logo_url.indexOf('?') >= 0 ? '&' : '?') + 't=' + Date.now();
            setLogoPreview(logoUrl);
            syncRewritePickerLogoPreview(state.name, logoUrl);
        }
        setStatus('Логотип сохранён', false, 'ok');
        setTimeout(function () { setStatus('', false); }, 1500);
        return data.logo_url;
    }

    async function saveTemplate() {
        if (!state.name) return false;
        var oldName = state.name;
        var newName = resolveTemplateName(nameEl ? nameEl.value : oldName);
        if (nameEl && !String(nameEl.value || '').trim()) {
            nameEl.value = newName;
            syncMetaBadge(modal.querySelector('[data-rte-meta="name"]'));
        }
        state.saving = true;
        setStatus('Сохраняем…', false);
        try {
            var payload = {
                name: newName,
                description: descEl ? descEl.value : '',
                hero_prompt: (document.getElementById('rte-hero') || {}).value || '',
                master_prompt: (document.getElementById('rte-master') || {}).value || '',
                stages: readStagesPayload(),
                tts_defaults: readTtsPayload(),
            };
            var r = await fetch(templateUrl(oldName), {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.ok) {
                throw new Error((data && (data.message || data.error)) || 'Ошибка сохранения');
            }
            state.name = data.name || newName;
            state.protected = state.name.toLowerCase() === 'base template';
            if (nameEl && data.name) {
                nameEl.value = data.name;
                nameEl.title = state.protected ? 'Base Template нельзя переименовать' : '';
                syncMetaBadge(modal.querySelector('[data-rte-meta="name"]'));
            }
            syncDeleteBtnState();
            if (data.stages) state.initialStages = data.stages;
            if (data.tts_defaults) state.initialTts = data.tts_defaults;
            if (data.logo_url) syncRewritePickerLogoPreview(state.name, data.logo_url);
            setStatus('Сохранено', false, 'ok');
            setTimeout(function () { setStatus('', false); }, 2000);
            if (state.name !== oldName) {
                var picker = document.getElementById('rewrite-template-picker');
                if (picker) {
                    var opt = picker.querySelector('[data-template-name="' + CSS.escape(oldName) + '"]');
                    if (opt) {
                        opt.setAttribute('data-template-name', state.name);
                        var radio = opt.querySelector('input.rewrite-template-radio');
                        if (radio) radio.value = state.name;
                        var optName = opt.querySelector('.template-option-name');
                        if (optName && state.name.toLowerCase() !== 'base template') {
                            optName.textContent = state.name;
                        }
                    }
                }
            }
            return true;
        } catch (e) {
            setStatus(String(e && e.message ? e.message : e), true);
            return false;
        } finally {
            state.saving = false;
        }
    }

    modal.querySelectorAll('[data-rewrite-template-editor-close]').forEach(function (el) {
        el.addEventListener('click', function (ev) {
            ev.preventDefault();
            void closeModal();
        });
    });

    document.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Escape') return;
        if (deleteModal && !deleteModal.classList.contains('hidden')) {
            ev.preventDefault();
            closeDeleteConfirmModal();
            return;
        }
        if (!modal.classList.contains('hidden')) {
            ev.preventDefault();
            void closeModal();
        }
    });

    if (deleteBtn) {
        deleteBtn.addEventListener('click', function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            openDeleteConfirmModal();
        });
    }
    if (deleteOkBtn) {
        deleteOkBtn.addEventListener('click', function (ev) {
            ev.preventDefault();
            void confirmDeleteTemplate();
        });
    }
    if (deleteModal) {
        deleteModal.querySelectorAll('[data-rte-template-delete-close]').forEach(function (el) {
            el.addEventListener('click', function (ev) {
                ev.preventDefault();
                closeDeleteConfirmModal();
            });
        });
    }

    if (logoHit && logoFileEl) {
        logoHit.addEventListener('click', function () { logoFileEl.click(); });
        logoFileEl.addEventListener('change', function () {
            var f = logoFileEl.files && logoFileEl.files[0];
            if (!f) return;
            var previewUrl = URL.createObjectURL(f);
            setLogoPreview(previewUrl);
            void uploadLogoFile(f).catch(function (e) {
                setStatus(String(e && e.message ? e.message : e), true);
            }).finally(function () {
                try { URL.revokeObjectURL(previewUrl); } catch (_e) { /* ignore */ }
                logoFileEl.value = '';
            });
        });
    }

    var picker = document.getElementById('rewrite-template-picker');
    if (picker) {
        picker.addEventListener('dblclick', function (ev) {
            var opt = ev.target.closest('label.rewrite-template-option');
            if (!opt || opt.classList.contains('rewrite-template-option--missing')) return;
            ev.preventDefault();
            var name = opt.getAttribute('data-template-name') || '';
            var radio = opt.querySelector('input.rewrite-template-radio');
            if (radio && !radio.disabled) {
                radio.checked = true;
                radio.dispatchEvent(new Event('change', { bubbles: true }));
            }
            openModal(name);
        });
    }

    if (ttsSettingsToggle) {
        ttsSettingsToggle.addEventListener('click', function () {
            if (!elevenlabsKeySet) return;
            var wasLocked = ttsSettingsLocked;
            ttsSettingsLocked = !ttsSettingsLocked;
            applyTtsSettingsLockUi();
            if (wasLocked) {
                var first = document.getElementById(ttsSettingsRangeIds[0]);
                if (first) first.focus();
            } else {
                void queueSaveTemplateAndNotify();
            }
        });
    }

    wireMetaBlocks();
    wirePromptBlocks();
    wireFoldPanels();
    bindSpeedSlider();
    bindPctSlider('rte-tts-stability', 'rte-tts-stability-val');
    bindPctSlider('rte-tts-similarity', 'rte-tts-similarity-val');
    bindPctSlider('rte-tts-style', 'rte-tts-style-val');
    applyTtsSettingsLockUi();
    initRteVoiceDropdown();
    document.addEventListener('click', function (ev) {
        if (!modal.contains(ev.target)) return;
        if (ev.target.closest('[data-rte-model-field="tts-voice"]')) return;
        closeRteVoicePanelsExcept(null);
    });

    window.openRewriteTemplateEditor = openModal;
})();
