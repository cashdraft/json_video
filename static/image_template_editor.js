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
    var logoHit = document.getElementById('image-template-editor-logo-hit');
    var logoPlaceholder = document.getElementById('image-template-editor-logo-placeholder');
    var dropzone = document.getElementById('image-template-editor-dropzone');
    var dropzoneLoading = document.getElementById('image-template-editor-dropzone-loading');
    var dropzoneLoadingText = document.getElementById('image-template-editor-dropzone-loading-text');
    var refsFileEl = document.getElementById('image-template-editor-refs-file');
    var refGrid = document.getElementById('image-template-editor-ref-grid');
    var refCountEl = document.getElementById('image-template-editor-ref-count');
    var statusEl = document.getElementById('image-template-editor-status');
    var titleEl = document.getElementById('image-template-editor-title');
    var deleteBtn = document.getElementById('image-template-editor-delete');
    var deleteModal = document.getElementById('image-template-delete-modal');
    var deleteLeadEl = document.getElementById('image-template-delete-lead');
    var deleteOkBtn = document.getElementById('image-template-delete-ok');
    var deleteCancelBtn = document.getElementById('image-template-delete-cancel-btn');

    var state = {
        mode: 'create',
        folder: '',
        references: [],
        logoUrl: null,
        pendingLogoFile: null,
        dragRefFilename: null,
        orderSaving: false,
        openFolder: '',
        openName: '',
    };

    var closing = false;

    var DROPZONE_LOADING_RE = /^(Загрузка|Создание шаблона|Удаление|Сохранение)/;
    var DROPZONE_ERROR_RE = /(максимум|фото|изображен|загруз|файл|передан|не удалось|нет файлов|слот|шаблон|логотип)/i;

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
        var text = '';
        try {
            text = await resp.text();
            var data = text ? JSON.parse(text) : {};
            if (!resp.ok && data && !data.error) {
                data.error = 'Ошибка сервера (HTTP ' + resp.status + ')';
                data.ok = false;
            }
            return data;
        } catch (e) {
            if (resp.status === 405) {
                return {
                    ok: false,
                    error: 'Сервер не поддерживает этот запрос. Обновите страницу или перезапустите json-video.',
                };
            }
            return {
                ok: false,
                error: 'Некорректный ответ сервера (HTTP ' + resp.status + ')',
            };
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

    function syncPickerLogoPreview(folder, logoUrl) {
        if (!folder || !logoUrl) return;
        var esc = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(folder) : folder.replace(/"/g, '\\"');
        var card = document.querySelector('.template-option[data-template-folder="' + esc + '"]');
        if (!card) return;
        var img = card.querySelector('.template-logo-preview');
        if (!img) return;
        var sep = logoUrl.indexOf('?') >= 0 ? '&' : '?';
        img.src = logoUrl + sep + 't=' + Date.now();
    }

    function showLogoPreview(url) {
        if (!logoPreviewWrap || !logoPreviewImg) return;
        if (url) {
            var sep = url.indexOf('?') >= 0 ? '&' : '?';
            logoPreviewImg.src = url + sep + 't=' + Date.now();
            logoPreviewImg.classList.remove('hidden');
            logoPreviewWrap.classList.remove(
                'image-template-editor-logo-preview-wrap--empty',
                'rewrite-template-edit-logo-preview-wrap--empty'
            );
            if (logoPlaceholder) logoPlaceholder.classList.add('hidden');
        } else {
            logoPreviewImg.removeAttribute('src');
            logoPreviewImg.classList.add('hidden');
            logoPreviewWrap.classList.add(
                'image-template-editor-logo-preview-wrap--empty',
                'rewrite-template-edit-logo-preview-wrap--empty'
            );
            if (logoPlaceholder) logoPlaceholder.classList.remove('hidden');
        }
    }

    async function ensureFolderForLogo() {
        var folder = state.folder;
        var newName = (nameEl && nameEl.value || '').trim();
        if (folder) return folder;
        if (!newName) throw new Error('Сначала введите название шаблона.');
        setStatus('Создание шаблона…');
        var created = await ensureFolderExists(newName);
        if (!created.ok) throw new Error(created.error || 'Ошибка создания');
        folder = created.template.folder_name;
        state.folder = folder;
        state.openFolder = folder;
        if (!state.openName) state.openName = newName;
        state.mode = 'edit';
        setStatus('');
        return folder;
    }

    async function uploadLogoFile(file) {
        if (!file) return null;
        var previewUrl = URL.createObjectURL(file);
        showLogoPreview(previewUrl);
        setStatus('Сохранение логотипа…');
        try {
            var folder = state.folder || await ensureFolderForLogo();
            var logoRes = await uploadLogo(folder, file);
            if (!logoRes.ok) throw new Error(logoRes.error || 'Ошибка логотипа');
            state.pendingLogoFile = null;
            if (logoRes.template) {
                loadTemplateIntoModal(logoRes.template);
                syncPickerLogoPreview(folder, logoRes.template.logo_url);
            }
            setStatus('');
            return logoRes;
        } finally {
            try { URL.revokeObjectURL(previewUrl); } catch (_e) { /* ignore */ }
        }
    }

    function syncDeleteBtnVisibility() {
        if (!deleteBtn) return;
        deleteBtn.classList.toggle('hidden', !state.folder);
    }

    function openDeleteConfirmModal() {
        if (!deleteModal || !state.folder) return;
        if (deleteLeadEl) {
            deleteLeadEl.textContent =
                'Это действие безвозвратно. Удалить шаблон «' + state.folder + '» и все файлы?';
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
        if (!state.folder) return;
        var folder = state.folder;
        if (deleteOkBtn) deleteOkBtn.disabled = true;
        try {
            var resp = await fetch(apiUrl('/' + encodePath(folder)), {
                method: 'DELETE',
                credentials: 'same-origin',
            });
            var data = await parseJson(resp);
            if (!data.ok) throw new Error(data.error || 'Не удалось удалить шаблон');
            closeDeleteConfirmModal();
            hideModalUI();
            try {
                sessionStorage.removeItem('json_video_select_image_template');
            } catch (e) { /* ignore */ }
            window.location.reload();
        } catch (e) {
            closeDeleteConfirmModal();
            setStatus(String(e.message || e), true);
        } finally {
            if (deleteOkBtn) deleteOkBtn.disabled = false;
        }
    }

    function loadTemplateIntoModal(tpl) {
        state.folder = tpl.folder_name || '';
        state.openFolder = state.folder;
        state.openName = state.folder;
        state.references = (tpl.references || []).slice();
        state.logoUrl = tpl.logo_url || null;
        state.pendingLogoFile = null;
        if (nameEl) nameEl.value = state.folder;
        showLogoPreview(state.logoUrl);
        renderReferences();
        syncDeleteBtnVisibility();
    }

    function openModalCreate() {
        state.mode = 'create';
        state.folder = '';
        state.openFolder = '';
        state.openName = '';
        state.references = [];
        state.logoUrl = null;
        state.pendingLogoFile = null;
        if (titleEl) titleEl.textContent = 'Новый шаблон изображений';
        if (nameEl) nameEl.value = '';
        nameLocked = false;
        applyImageTemplateNameLockUi();
        showLogoPreview(null);
        renderReferences();
        syncDeleteBtnVisibility();
        setStatus('');
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
        if (nameEl) nameEl.focus();
    }

    async function openModalEdit(folder) {
        state.mode = 'edit';
        if (titleEl) titleEl.textContent = 'Шаблон изображений';
        nameLocked = true;
        applyImageTemplateNameLockUi();
        setStatus('Загрузка…');
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
        try {
            var resp = await fetch(apiUrl('/' + encodePath(folder)), { credentials: 'same-origin' });
            var data = await parseJson(resp);
            if (!data.ok || !data.template) throw new Error(data.error || 'Не удалось загрузить шаблон');
            loadTemplateIntoModal(data.template);
            applyImageTemplateNameLockUi();
            setStatus('');
        } catch (e) {
            setStatus(String(e.message || e), true);
        }
    }

    function hideModalUI() {
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
        state.pendingLogoFile = null;
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

        if (state.pendingLogoFile && folder) {
            var logoRes = await uploadLogo(folder, state.pendingLogoFile);
            if (!logoRes.ok) throw new Error(logoRes.error || 'Ошибка логотипа');
            state.pendingLogoFile = null;
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

    function uploadLogo(folder, fileOrBlob) {
        var fd = new FormData();
        var logoName = (fileOrBlob && fileOrBlob.name) ? fileOrBlob.name : 'logo.png';
        fd.append('logo', fileOrBlob, logoName);
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

    if (logoHit && logoFileEl) {
        logoHit.addEventListener('click', function () {
            logoFileEl.click();
        });
        logoFileEl.addEventListener('change', function () {
            var f = logoFileEl.files && logoFileEl.files[0];
            logoFileEl.value = '';
            if (!f) return;
            var newName = (nameEl && nameEl.value || '').trim();
            if (!state.folder && !newName) {
                var pendingPreview = URL.createObjectURL(f);
                showLogoPreview(pendingPreview);
                state.pendingLogoFile = f;
                setStatus('');
                return;
            }
            void uploadLogoFile(f).catch(function (e) {
                setStatus(String(e.message || e), true);
            });
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
                    syncDeleteBtnVisibility();
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
            confirmDeleteTemplate();
        });
    }

    if (deleteCancelBtn) {
        deleteCancelBtn.addEventListener('click', function (ev) {
            ev.preventDefault();
            closeDeleteConfirmModal();
        });
    }

    if (deleteModal) {
        deleteModal.querySelectorAll('[data-image-template-delete-close]').forEach(function (el) {
            el.addEventListener('click', function (ev) {
                ev.preventDefault();
                closeDeleteConfirmModal();
            });
        });
    }

    var jobImagePicker = document.getElementById('job-image-template-picker');
    if (jobImagePicker) {
        jobImagePicker.addEventListener('dblclick', function (ev) {
            var opt = ev.target.closest('label.template-option');
            if (!opt || opt.classList.contains('template-option--add')) return;
            var folder = opt.getAttribute('data-template-folder') || '';
            if (!folder) return;
            ev.preventDefault();
            var radio = opt.querySelector('input.template-option-input');
            if (radio && !radio.disabled) {
                radio.checked = true;
                radio.dispatchEvent(new Event('change', { bubbles: true }));
            }
            openModalEdit(folder);
        });
    }

    var addBtns = document.querySelectorAll('.image-template-add-btn');
    addBtns.forEach(function (btn) {
        btn.addEventListener('click', function (ev) {
            ev.preventDefault();
            openModalCreate();
        });
    });

    var nameToggle = document.getElementById('image-template-editor-name-toggle');
    var nameBadge = document.getElementById('image-template-editor-name-badge');
    var nameLocked = true;

    function applyImageTemplateNameLockUi() {
        if (!nameEl) return;
        nameEl.readOnly = nameLocked;
        nameEl.classList.toggle('rewrite-source-textarea--locked', nameLocked);
        if (nameToggle) {
            nameToggle.classList.toggle('rewrite-lock-toggle--locked', nameLocked);
            nameToggle.setAttribute('aria-label', nameLocked ? 'Редактировать' : 'Сохранить');
            nameToggle.title = nameLocked ? 'Редактировать' : 'Сохранить';
        }
        if (nameBadge) {
            var has = !!(nameEl.value || '').trim();
            nameBadge.classList.toggle('badge-yes', has);
            nameBadge.classList.toggle('badge-no', !has);
        }
    }

    if (nameToggle) {
        nameToggle.addEventListener('click', function (ev) {
            ev.preventDefault();
            nameLocked = !nameLocked;
            applyImageTemplateNameLockUi();
            if (!nameLocked) nameEl.focus();
        });
    }
    if (nameEl) nameEl.addEventListener('input', applyImageTemplateNameLockUi);
    applyImageTemplateNameLockUi();

    try {
        var sel = sessionStorage.getItem('json_video_select_image_template');
        if (sel) {
            sessionStorage.removeItem('json_video_select_image_template');
            var radio = document.querySelector('input[name="image_template"][value="' + CSS.escape(sel) + '"]');
            if (radio) radio.checked = true;
        }
    } catch (e) { /* ignore */ }
})();
