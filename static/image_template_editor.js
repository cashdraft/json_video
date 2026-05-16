(function () {
    'use strict';

    var MAX_REFS = 7;
    var modal = document.getElementById('image-template-editor-modal');
    if (!modal) return;

    var ROOT = (window.__JSON_VIDEO_ROOT__ || '').replace(/\/$/, '');
    var apiBase = ROOT + '/api/image-templates';

    var nameEl = document.getElementById('image-template-editor-name');
    var logoFileEl = document.getElementById('image-template-editor-logo-file');
    var logoPreviewWrap = document.getElementById('image-template-editor-logo-preview-wrap');
    var logoPreviewImg = document.getElementById('image-template-editor-logo-preview');
    var cropWrap = document.getElementById('image-template-editor-crop-wrap');
    var cropCanvas = document.getElementById('image-template-editor-crop-canvas');
    var cropBox = document.getElementById('image-template-editor-crop-box');
    var cropStage = document.getElementById('image-template-editor-crop-stage');
    var cropApplyBtn = document.getElementById('image-template-editor-crop-apply');
    var dropzone = document.getElementById('image-template-editor-dropzone');
    var dropzoneLoading = document.getElementById('image-template-editor-dropzone-loading');
    var dropzoneLoadingText = document.getElementById('image-template-editor-dropzone-loading-text');
    var refsFileEl = document.getElementById('image-template-editor-refs-file');
    var refGrid = document.getElementById('image-template-editor-ref-grid');
    var refCountEl = document.getElementById('image-template-editor-ref-count');
    var statusEl = document.getElementById('image-template-editor-status');
    var titleEl = document.getElementById('image-template-editor-title');

    var state = {
        mode: 'create',
        folder: '',
        references: [],
        logoUrl: null,
        pendingLogoBlob: null,
        cropImage: null,
        cropRect: { x: 0.25, y: 0.25, w: 0.5, h: 0.5 },
        cropDrag: null,
        dragRefFilename: null,
        orderSaving: false,
        openFolder: '',
        openName: '',
    };

    var closing = false;

    var DROPZONE_LOADING_RE = /^(Загрузка|Создание шаблона|Удаление|Сохранение)/;
    var DROPZONE_ERROR_RE = /(максимум|фото|изображен|загруз|файл|передан|не удалось|нет файлов|слот|шаблон)/i;

    function shouldShowInDropzone(msg, isError) {
        if (!msg) return false;
        if (DROPZONE_LOADING_RE.test(msg)) return true;
        if (isError && (DROPZONE_ERROR_RE.test(msg) || /^Сначала /i.test(msg))) return true;
        return false;
    }

    function setDropzoneOverlay(msg, isError) {
        if (!dropzone || !dropzoneLoading || !dropzoneLoadingText) return;
        if (msg) {
            dropzoneLoadingText.textContent = msg;
            dropzoneLoading.classList.remove('hidden');
            dropzoneLoading.classList.toggle('image-template-editor-dropzone-loading--error', !!isError);
            dropzoneLoading.setAttribute('aria-hidden', 'false');
            dropzone.classList.add('image-template-editor-dropzone--loading');
            if (!isError) dropzone.setAttribute('aria-busy', 'true');
            else dropzone.removeAttribute('aria-busy');
        } else {
            dropzoneLoading.classList.add('hidden');
            dropzoneLoading.classList.remove('image-template-editor-dropzone-loading--error');
            dropzoneLoading.setAttribute('aria-hidden', 'true');
            dropzoneLoadingText.textContent = '';
            dropzone.classList.remove('image-template-editor-dropzone--loading');
            dropzone.removeAttribute('aria-busy');
        }
    }

    function setStatus(msg, isError) {
        if (shouldShowInDropzone(msg, isError)) {
            setDropzoneOverlay(msg, isError);
            if (statusEl) {
                statusEl.textContent = '';
                statusEl.classList.add('hidden');
                statusEl.classList.remove('image-template-editor-status--error');
            }
            return;
        }
        setDropzoneOverlay('', false);
        if (!statusEl) return;
        statusEl.textContent = msg || '';
        statusEl.classList.toggle('hidden', !msg);
        statusEl.classList.toggle('image-template-editor-status--error', !!isError && !!msg);
    }

    function encodePath(name) {
        return encodeURIComponent(name).replace(/%2F/g, '/');
    }

    function apiUrl(path) {
        return apiBase + path;
    }

    async function parseJson(resp) {
        try {
            return await resp.json();
        } catch (e) {
            return { ok: false, error: 'Некорректный ответ сервера' };
        }
    }

    function updateRefCount() {
        if (refCountEl) {
            refCountEl.textContent = state.references.length + ' / ' + MAX_REFS;
        }
    }

    function clearDropIndicators() {
        if (!refGrid) return;
        refGrid.querySelectorAll('.image-template-editor-ref-thumb').forEach(function (el) {
            el.classList.remove(
                'image-template-editor-ref-thumb--drop-before',
                'image-template-editor-ref-thumb--drop-after'
            );
        });
    }

    function moveReference(draggedFilename, targetFilename, insertBefore) {
        var arr = state.references.slice();
        var fromIdx = -1;
        var i;
        for (i = 0; i < arr.length; i++) {
            if (arr[i].filename === draggedFilename) {
                fromIdx = i;
                break;
            }
        }
        if (fromIdx < 0) return false;
        var item = arr.splice(fromIdx, 1)[0];
        var toIdx = -1;
        for (i = 0; i < arr.length; i++) {
            if (arr[i].filename === targetFilename) {
                toIdx = i;
                break;
            }
        }
        if (toIdx < 0) {
            arr.push(item);
        } else {
            if (!insertBefore) toIdx += 1;
            arr.splice(toIdx, 0, item);
        }
        state.references = arr;
        return true;
    }

    function saveReferenceOrder() {
        if (!state.folder || state.references.length < 2) {
            return Promise.resolve({ ok: true });
        }
        var order = state.references.map(function (r) { return r.filename; });
        return fetch(apiUrl('/' + encodePath(state.folder) + '/references/order'), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ order: order }),
        }).then(function (r) { return parseJson(r); });
    }

    async function persistReferenceOrder() {
        if (!state.folder || state.references.length < 2 || state.orderSaving) return;
        state.orderSaving = true;
        try {
            var data = await saveReferenceOrder();
            if (!data.ok) throw new Error(data.error || 'Не удалось сохранить порядок');
            if (data.template) {
                state.references = (data.template.references || []).slice();
            }
        } catch (e) {
            setStatus(String(e.message || e), true);
        } finally {
            state.orderSaving = false;
            renderReferences();
        }
    }

    function renderReferences() {
        if (!refGrid) return;
        refGrid.innerHTML = '';
        state.references.forEach(function (ref, idx) {
            var item = document.createElement('div');
            item.className = 'image-template-editor-ref-item';
            var label = document.createElement('span');
            label.className = 'image-template-editor-ref-label';
            label.textContent = 'Image ' + (idx + 1);
            var cell = document.createElement('div');
            cell.className = 'image-template-editor-ref-thumb';
            cell.draggable = true;
            cell.dataset.filename = ref.filename;
            var img = document.createElement('img');
            img.src = ref.url + (ref.url.indexOf('?') >= 0 ? '&' : '?') + 't=' + Date.now();
            img.alt = ref.filename;
            var del = document.createElement('button');
            del.type = 'button';
            del.className = 'image-template-editor-ref-del';
            del.title = 'Удалить';
            del.setAttribute('aria-label', 'Удалить');
            del.textContent = '×';
            del.addEventListener('click', function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                deleteReference(ref.filename);
            });
            cell.addEventListener('dragstart', function (ev) {
                state.dragRefFilename = ref.filename;
                cell.classList.add('image-template-editor-ref-thumb--dragging');
                if (ev.dataTransfer) {
                    ev.dataTransfer.effectAllowed = 'move';
                    ev.dataTransfer.setData('text/plain', ref.filename);
                }
            });
            cell.addEventListener('dragend', function () {
                cell.classList.remove('image-template-editor-ref-thumb--dragging');
                clearDropIndicators();
                state.dragRefFilename = null;
            });
            cell.addEventListener('dragover', function (ev) {
                if (!state.dragRefFilename || state.dragRefFilename === ref.filename) return;
                ev.preventDefault();
                if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
                clearDropIndicators();
                var rect = cell.getBoundingClientRect();
                var before = ev.clientX < rect.left + rect.width / 2;
                cell.classList.add(
                    before
                        ? 'image-template-editor-ref-thumb--drop-before'
                        : 'image-template-editor-ref-thumb--drop-after'
                );
            });
            cell.addEventListener('dragleave', function (ev) {
                if (ev.currentTarget.contains(ev.relatedTarget)) return;
                cell.classList.remove(
                    'image-template-editor-ref-thumb--drop-before',
                    'image-template-editor-ref-thumb--drop-after'
                );
            });
            cell.addEventListener('drop', function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                clearDropIndicators();
                var dragged = state.dragRefFilename
                    || (ev.dataTransfer && ev.dataTransfer.getData('text/plain'));
                if (!dragged || dragged === ref.filename) return;
                var rect = cell.getBoundingClientRect();
                var insertBefore = ev.clientX < rect.left + rect.width / 2;
                if (!moveReference(dragged, ref.filename, insertBefore)) return;
                renderReferences();
                persistReferenceOrder();
            });
            cell.appendChild(img);
            cell.appendChild(del);
            item.appendChild(label);
            item.appendChild(cell);
            refGrid.appendChild(item);
        });
        updateRefCount();
    }

    function showLogoPreview(url) {
        if (!logoPreviewWrap || !logoPreviewImg) return;
        if (url) {
            logoPreviewImg.src = url;
            logoPreviewWrap.classList.remove('hidden');
        } else {
            logoPreviewImg.removeAttribute('src');
            logoPreviewWrap.classList.add('hidden');
        }
    }

    function resetCrop() {
        state.cropImage = null;
        state.pendingLogoBlob = null;
        if (cropWrap) cropWrap.classList.add('hidden');
    }

    function loadTemplateIntoModal(tpl) {
        state.folder = tpl.folder_name || '';
        state.openFolder = state.folder;
        state.openName = state.folder;
        state.references = (tpl.references || []).slice();
        state.logoUrl = tpl.logo_url || null;
        state.pendingLogoBlob = null;
        resetCrop();
        if (nameEl) nameEl.value = state.folder;
        showLogoPreview(state.logoUrl);
        renderReferences();
    }

    function openModalCreate() {
        state.mode = 'create';
        state.folder = '';
        state.openFolder = '';
        state.openName = '';
        state.references = [];
        state.logoUrl = null;
        state.pendingLogoBlob = null;
        resetCrop();
        if (titleEl) titleEl.textContent = 'Новый шаблон изображений';
        if (nameEl) {
            nameEl.value = '';
            nameEl.disabled = false;
        }
        showLogoPreview(null);
        renderReferences();
        setStatus('');
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
        if (nameEl) nameEl.focus();
    }

    async function openModalEdit(folder) {
        state.mode = 'edit';
        if (titleEl) titleEl.textContent = 'Редактировать шаблон';
        if (nameEl) nameEl.disabled = false;
        setStatus('Загрузка…');
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
        try {
            var resp = await fetch(apiUrl('/' + encodePath(folder)), { credentials: 'same-origin' });
            var data = await parseJson(resp);
            if (!data.ok || !data.template) throw new Error(data.error || 'Не удалось загрузить шаблон');
            loadTemplateIntoModal(data.template);
            setStatus('');
        } catch (e) {
            setStatus(String(e.message || e), true);
        }
    }

    function hideModalUI() {
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
        resetCrop();
        setStatus('');
        setDropzoneOverlay('', false);
    }

    async function persistAllOnClose() {
        var newName = (nameEl && nameEl.value || '').trim();
        var folder = state.folder;
        var needReload = false;

        if (!newName) {
            if (folder) throw new Error('Введите название шаблона.');
            return { needReload: false, folder: '' };
        }

        var wasNew = !state.openFolder;
        var nameChanged = newName !== state.openName;

        if (!folder || nameChanged) {
            folder = await saveName(folder, newName);
            state.folder = folder;
            state.mode = 'edit';
            needReload = true;
        }

        if (state.pendingLogoBlob && folder) {
            var logoRes = await uploadLogo(folder, state.pendingLogoBlob);
            if (!logoRes.ok) throw new Error(logoRes.error || 'Ошибка логотипа');
            state.pendingLogoBlob = null;
            needReload = true;
        }

        if (folder && state.references.length >= 2) {
            var orderRes = await saveReferenceOrder();
            if (!orderRes.ok) throw new Error(orderRes.error || 'Не удалось сохранить порядок');
        }

        if (wasNew && folder) needReload = true;

        return { needReload: needReload, folder: folder || '' };
    }

    async function requestCloseModal() {
        if (closing || state.orderSaving) return;
        closing = true;
        setDropzoneOverlay('Сохранение…', false);
        try {
            var result = await persistAllOnClose();
            hideModalUI();
            if (result.needReload && result.folder) {
                try {
                    sessionStorage.setItem('json_video_select_image_template', result.folder);
                } catch (e) { /* ignore */ }
                window.location.reload();
            }
        } catch (e) {
            setDropzoneOverlay('');
            setStatus(String(e.message || e), true);
        } finally {
            closing = false;
        }
    }

    function ensureFolderExists(name) {
        return fetch(apiUrl(''), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ name: name }),
        }).then(function (r) { return parseJson(r); });
    }

    function saveName(oldName, newName) {
        if (state.mode === 'create') {
            return ensureFolderExists(newName).then(function (data) {
                if (!data.ok) throw new Error(data.error || 'Не удалось создать шаблон');
                return newName;
            });
        }
        if (oldName === newName) return Promise.resolve(newName);
        return fetch(apiUrl('/' + encodePath(oldName)), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ name: newName }),
        }).then(function (r) {
            return parseJson(r).then(function (data) {
                if (!data.ok) throw new Error(data.error || 'Не удалось переименовать');
                return data.template.folder_name;
            });
        });
    }

    function uploadLogo(folder, blob) {
        var fd = new FormData();
        fd.append('logo', blob, 'logo.png');
        return fetch(apiUrl('/' + encodePath(folder) + '/logo'), {
            method: 'POST',
            credentials: 'same-origin',
            body: fd,
        }).then(function (r) { return parseJson(r); });
    }

    function uploadReferences(folder, files) {
        var fd = new FormData();
        for (var i = 0; i < files.length; i++) {
            fd.append('files', files[i]);
        }
        return fetch(apiUrl('/' + encodePath(folder) + '/references'), {
            method: 'POST',
            credentials: 'same-origin',
            body: fd,
        }).then(function (r) { return parseJson(r); });
    }

    async function deleteReference(filename) {
        if (!state.folder) return;
        setStatus('Удаление…');
        try {
            var resp = await fetch(
                apiUrl('/' + encodePath(state.folder) + '/references/' + encodeURIComponent(filename)),
                { method: 'DELETE', credentials: 'same-origin' }
            );
            var data = await parseJson(resp);
            if (!data.ok) throw new Error(data.error || 'Ошибка удаления');
            loadTemplateIntoModal(data.template);
            setStatus('');
        } catch (e) {
            setStatus(String(e.message || e), true);
        }
    }

    async function refreshTemplate(folder) {
        var resp = await fetch(apiUrl('/' + encodePath(folder)), { credentials: 'same-origin' });
        var data = await parseJson(resp);
        if (data.ok && data.template) loadTemplateIntoModal(data.template);
    }

    function setupCropFromFile(file) {
        var reader = new FileReader();
        reader.onload = function () {
            var img = new Image();
            img.onload = function () {
                state.cropImage = img;
                state.cropRect = { x: 0.2, y: 0.2, w: 0.6, h: 0.6 };
                if (cropWrap) cropWrap.classList.remove('hidden');
                drawCropCanvas();
                positionCropBox();
            };
            img.src = reader.result;
        };
        reader.readAsDataURL(file);
    }

    function drawCropCanvas() {
        if (!cropCanvas || !state.cropImage) return;
        var maxW = 420;
        var scale = Math.min(1, maxW / state.cropImage.width);
        var w = Math.round(state.cropImage.width * scale);
        var h = Math.round(state.cropImage.height * scale);
        cropCanvas.width = w;
        cropCanvas.height = h;
        var ctx = cropCanvas.getContext('2d');
        ctx.drawImage(state.cropImage, 0, 0, w, h);
        cropCanvas.dataset.scale = String(scale);
    }

    function positionCropBox() {
        if (!cropBox || !cropCanvas) return;
        var r = state.cropRect;
        cropBox.style.left = (r.x * 100) + '%';
        cropBox.style.top = (r.y * 100) + '%';
        cropBox.style.width = (r.w * 100) + '%';
        cropBox.style.height = (r.h * 100) + '%';
    }

    function cropToBlob(cb) {
        if (!state.cropImage || !cropCanvas) return;
        var scale = parseFloat(cropCanvas.dataset.scale || '1');
        var iw = state.cropImage.width;
        var ih = state.cropImage.height;
        var r = state.cropRect;
        var sx = Math.round(r.x * iw);
        var sy = Math.round(r.y * ih);
        var sw = Math.max(1, Math.round(r.w * iw));
        var sh = Math.max(1, Math.round(r.h * ih));
        var out = document.createElement('canvas');
        out.width = sw;
        out.height = sh;
        var ctx = out.getContext('2d');
        ctx.drawImage(state.cropImage, sx, sy, sw, sh, 0, 0, sw, sh);
        out.toBlob(function (blob) {
            if (blob) cb(blob);
        }, 'image/png');
    }

    if (logoFileEl) {
        logoFileEl.addEventListener('change', function () {
            var f = logoFileEl.files && logoFileEl.files[0];
            logoFileEl.value = '';
            if (!f) return;
            setupCropFromFile(f);
        });
    }

    if (cropApplyBtn) {
        cropApplyBtn.addEventListener('click', function () {
            cropToBlob(function (blob) {
                state.pendingLogoBlob = blob;
                var url = URL.createObjectURL(blob);
                showLogoPreview(url);
                if (cropWrap) cropWrap.classList.add('hidden');
                setStatus('Логотип сохранится при закрытии окна.');
            });
        });
    }

    if (cropBox && cropStage) {
        cropBox.addEventListener('pointerdown', function (ev) {
            ev.preventDefault();
            state.cropDrag = {
                startX: ev.clientX,
                startY: ev.clientY,
                rect: Object.assign({}, state.cropRect),
            };
            cropBox.setPointerCapture(ev.pointerId);
        });
        cropBox.addEventListener('pointermove', function (ev) {
            if (!state.cropDrag || !cropStage) return;
            var sr = cropStage.getBoundingClientRect();
            var dx = (ev.clientX - state.cropDrag.startX) / sr.width;
            var dy = (ev.clientY - state.cropDrag.startY) / sr.height;
            var r = state.cropDrag.rect;
            var nx = Math.max(0, Math.min(1 - r.w, r.x + dx));
            var ny = Math.max(0, Math.min(1 - r.h, r.y + dy));
            state.cropRect = { x: nx, y: ny, w: r.w, h: r.h };
            positionCropBox();
        });
        cropBox.addEventListener('pointerup', function () {
            state.cropDrag = null;
        });
        cropBox.addEventListener('pointercancel', function () {
            state.cropDrag = null;
        });
    }

    function handleRefFiles(fileList) {
        if (!fileList || !fileList.length) return;
        var files = Array.from(fileList).filter(function (f) {
            return f.type && f.type.indexOf('image/') === 0;
        });
        if (!files.length) return;
        var slots = MAX_REFS - state.references.length;
        if (slots <= 0) {
            setStatus('Уже загружено максимум ' + MAX_REFS + ' фото.', true);
            return;
        }
        files = files.slice(0, slots);
        (async function () {
            var folder = state.folder;
            var newName = (nameEl && nameEl.value || '').trim();
            if (!folder && state.mode === 'create') {
                if (!newName) {
                    setStatus('Сначала введите название шаблона.', true);
                    return;
                }
                setStatus('Создание шаблона…');
                try {
                    var created = await ensureFolderExists(newName);
                    if (!created.ok) throw new Error(created.error || 'Ошибка создания');
                    folder = created.template.folder_name;
                    state.folder = folder;
                    state.openFolder = folder;
                    if (!state.openName) state.openName = newName;
                    state.mode = 'edit';
                } catch (e) {
                    setStatus(String(e.message || e), true);
                    return;
                }
            }
            if (!folder) {
                setStatus('Сначала сохраните название шаблона.', true);
                return;
            }
            setStatus('Загрузка…');
            try {
                var up = await uploadReferences(folder, files);
                if (!up.ok) throw new Error(up.error || 'Ошибка загрузки');
                loadTemplateIntoModal(up.template);
                if (up.warning) setStatus(up.warning, true);
                else setStatus('');
            } catch (e) {
                setStatus(String(e.message || e), true);
            }
        })();
    }

    if (dropzone && refsFileEl) {
        dropzone.addEventListener('click', function () { refsFileEl.click(); });
        dropzone.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' || ev.key === ' ') {
                ev.preventDefault();
                refsFileEl.click();
            }
        });
        dropzone.addEventListener('dragover', function (ev) {
            ev.preventDefault();
            dropzone.classList.add('image-template-editor-dropzone--over');
        });
        dropzone.addEventListener('dragleave', function (ev) {
            if (dropzone.contains(ev.relatedTarget)) return;
            dropzone.classList.remove('image-template-editor-dropzone--over');
        });
        dropzone.addEventListener('drop', function (ev) {
            ev.preventDefault();
            dropzone.classList.remove('image-template-editor-dropzone--over');
            handleRefFiles(ev.dataTransfer.files);
        });
        refsFileEl.addEventListener('change', function () {
            handleRefFiles(refsFileEl.files);
            refsFileEl.value = '';
        });
    }

    modal.querySelectorAll('[data-image-template-modal-close]').forEach(function (el) {
        el.addEventListener('click', function (ev) {
            ev.preventDefault();
            requestCloseModal();
        });
    });

    document.querySelectorAll('.template-option-edit-btn').forEach(function (btn) {
        btn.addEventListener('mousedown', function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
        });
        btn.addEventListener('click', function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            var folder = btn.getAttribute('data-template-folder') || '';
            if (folder) openModalEdit(folder);
        });
    });

    var addBtns = document.querySelectorAll('.image-template-add-btn');
    addBtns.forEach(function (btn) {
        btn.addEventListener('click', function (ev) {
            ev.preventDefault();
            openModalCreate();
        });
    });

    try {
        var sel = sessionStorage.getItem('json_video_select_image_template');
        if (sel) {
            sessionStorage.removeItem('json_video_select_image_template');
            var radio = document.querySelector('input[name="image_template"][value="' + CSS.escape(sel) + '"]');
            if (radio) radio.checked = true;
        }
    } catch (e) { /* ignore */ }
})();
