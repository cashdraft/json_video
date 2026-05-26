(function () {
    'use strict';

    const API_OK = document.body.getAttribute('data-api-key') === '1';
    const PEXELS_KEY_SET = document.body.getAttribute('data-pexels-key-set') === '1';
    const root = document.querySelector('[data-scenes-stock-agent="finder"]');
    if (!root) return;

    const els = {
        model: document.getElementById('sst-model'),
        source: document.getElementById('sst-source'),
        systemToggle: root.querySelector('[data-sst-system-toggle]'),
        systemWrap: root.querySelector('[data-sst-system-wrap]'),
        systemTa: root.querySelector('[data-sst-system-prompt]'),
        systemBadge: root.querySelector('[data-sst-system-badge]'),
        userToggle: root.querySelector('[data-sst-user-toggle]'),
        userWrap: root.querySelector('[data-sst-user-wrap]'),
        userTa: root.querySelector('[data-sst-user-prompt]'),
        userBadge: root.querySelector('[data-sst-user-badge]'),
        sceneToggle: root.querySelector('[data-sst-scene-toggle]'),
        sceneTa: root.querySelector('[data-sst-scene]'),
        sceneBadge: root.querySelector('[data-sst-scene-badge]'),
        resultToggle: root.querySelector('[data-sst-result-toggle]'),
        resultWrap: root.querySelector('[data-sst-result-wrap]'),
        resultTa: root.querySelector('[data-sst-result]'),
        resultBadge: root.querySelector('[data-sst-result-badge]'),
        resultCounts: root.querySelector('[data-sst-result-counts]'),
        resultCopy: root.querySelector('[data-sst-result-copy]'),
        exportBtn: root.querySelector('[data-sst-export]'),
        runBtn: root.querySelector('[data-sst-run]'),
        statusRow: root.querySelector('[data-sst-status-row]'),
        statusText: root.querySelector('[data-sst-status-text]'),
        cancelBtn: root.querySelector('[data-sst-cancel-btn]'),
        finderCheckWrap: root.querySelector('[data-sst-finder-check]'),
        boardWrap: root.querySelector('[data-sst-board]'),
        boardSyncBtn: root.querySelector('[data-sst-board-sync]'),
        boardSyncHint: root.querySelector('[data-sst-board-sync-hint]'),
        pexelsKeyInput: root.querySelector('[data-sst-pexels-key-input]'),
        pexelsKeyBadge: root.querySelector('[data-sst-pexels-key-badge]'),
        pexelsKeySave: root.querySelector('[data-sst-pexels-key-save]'),
    };

    let boardScenes = [];
    let boardSaveTimer = null;
    const boardSearching = {};

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

    function setSceneBadge() {
        if (!els.sceneBadge || !els.sceneTa) return;
        const yes = !!(els.sceneTa.value || '').trim();
        els.sceneBadge.classList.toggle('badge-yes', yes);
        els.sceneBadge.classList.toggle('badge-no', !yes);
    }

    function updateResultCounts() {
        if (!els.resultCounts || !els.resultTa) return;
        const text = els.resultTa.value || '';
        const words = text.trim() ? text.trim().split(/\s+/).length : 0;
        els.resultCounts.textContent = text.trim()
            ? formatNumRu(String(text.length)) + ' симв. · ' + formatNumRu(String(words)) + ' сл.'
            : '';
    }

    const STOCK_VIDEO_SOURCE = 'Stock_Video';

    function stripMarkdownFence(raw) {
        let text = String(raw || '').trim();
        if (text.startsWith('```')) {
            text = text.replace(/^```(?:json)?\s*/i, '');
            text = text.replace(/\s*```$/, '');
        }
        return text.trim();
    }

    function normalizeVisualSource(value) {
        return String(value || '').trim().toLowerCase().replace(/[\s_]+/g, '');
    }

    function isStockVideoSource(value) {
        return normalizeVisualSource(value) === normalizeVisualSource(STOCK_VIDEO_SOURCE);
    }

    function normalizeHeroText(value) {
        return String(value || '').trim().replace(/\s+/g, ' ');
    }

    function sceneHeroText(scene) {
        const hero = String((scene && scene.hero_text) || '').trim();
        if (hero) return hero;
        return String((scene && scene.text) || '').trim();
    }

    function stripResultErrorSuffix(raw) {
        const marker = '\n\n--- Ошибка ---\n';
        const idx = String(raw || '').indexOf(marker);
        if (idx >= 0) return String(raw).slice(0, idx);
        return String(raw || '');
    }

    function parseSceneJsonl(raw) {
        const text = stripMarkdownFence(stripResultErrorSuffix(raw));
        if (!text.trim()) return { scenes: [], errors: [] };

        if (text.trim().startsWith('[')) {
            try {
                const arr = JSON.parse(text);
                if (!Array.isArray(arr)) {
                    return { scenes: [], errors: ['Ожидается JSON-массив сцен'] };
                }
                return { scenes: arr.filter(function (x) { return x && typeof x === 'object'; }), errors: [] };
            } catch (e) {
                return { scenes: [], errors: ['Невалидный JSON-массив: ' + String(e.message || e)] };
            }
        }

        const scenes = [];
        const errors = [];
        text.split('\n').forEach(function (line, idx) {
            const chunk = line.trim();
            if (!chunk) return;
            try {
                const obj = JSON.parse(chunk);
                if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
                    errors.push('Строка ' + String(idx + 1) + ': ожидается JSON-объект');
                    return;
                }
                scenes.push(obj);
            } catch (e) {
                errors.push('Строка ' + String(idx + 1) + ': невалидный JSON — ' + String(e.message || e));
            }
        });
        return { scenes: scenes, errors: errors };
    }

    function validateFinderStockVideoMatch(sceneRaw, resultRaw) {
        const inputParsed = parseSceneJsonl(sceneRaw);
        const resultParsed = parseSceneJsonl(resultRaw);
        const inputScenes = inputParsed.scenes;
        const resultScenes = resultParsed.scenes;
        const parseErrors = inputParsed.errors.concat(resultParsed.errors);

        const expected = inputScenes.filter(function (s) {
            return isStockVideoSource(s.visual_source);
        });

        const resultById = {};
        const resultByHero = {};
        resultScenes.forEach(function (row) {
            const sid = String(row.scene_id || '').trim();
            if (sid) resultById[sid] = row;
            const heroKey = normalizeHeroText(sceneHeroText(row));
            if (heroKey) {
                if (!resultByHero[heroKey]) resultByHero[heroKey] = [];
                resultByHero[heroKey].push(row);
            }
        });

        const matched = [];
        const missing = [];
        const mismatches = [];
        const usedResultIds = {};

        expected.forEach(function (exp) {
            const sid = String(exp.scene_id || '').trim();
            const expHero = sceneHeroText(exp);
            const expHeroNorm = normalizeHeroText(expHero);
            let got = sid ? resultById[sid] : null;
            let matchKind = 'scene_id';

            if (!got && expHeroNorm) {
                const candidates = resultByHero[expHeroNorm] || [];
                for (let i = 0; i < candidates.length; i += 1) {
                    const cand = candidates[i];
                    const cid = String(cand.scene_id || '').trim();
                    if (cid && usedResultIds[cid]) continue;
                    got = cand;
                    matchKind = 'hero_text';
                    break;
                }
            }

            if (!got) {
                missing.push({ scene_id: sid, hero_text: expHero });
                return;
            }

            const gotId = String(got.scene_id || '').trim();
            if (gotId) usedResultIds[gotId] = true;
            const gotHero = sceneHeroText(got);
            const heroOk = normalizeHeroText(gotHero) === expHeroNorm;
            const sidOk = !sid || !gotId || sid === gotId;
            const row = {
                scene_id: sid || gotId,
                hero_text: expHero,
                result_hero_text: gotHero,
                match_kind: matchKind,
                hero_ok: heroOk,
                scene_id_ok: sidOk,
                ok: heroOk && sidOk,
            };
            matched.push(row);
            if (!row.ok) mismatches.push(row);
        });

        const expectedIds = {};
        const expectedHeroes = {};
        expected.forEach(function (s) {
            const sid = String(s.scene_id || '').trim();
            if (sid) expectedIds[sid] = true;
            const h = normalizeHeroText(sceneHeroText(s));
            if (h) expectedHeroes[h] = true;
        });

        const extra = [];
        resultScenes.forEach(function (row) {
            const sid = String(row.scene_id || '').trim();
            const heroNorm = normalizeHeroText(sceneHeroText(row));
            if (sid && expectedIds[sid]) return;
            if (heroNorm && expectedHeroes[heroNorm]) return;
            extra.push({ scene_id: sid, hero_text: sceneHeroText(row) });
        });

        const inputStock = expected.length;
        const resultCount = resultScenes.length;
        const matchedOk = matched.filter(function (r) { return r.ok; }).length;
        const countOk = inputStock === resultCount;
        const coverageOk = matchedOk === inputStock && !missing.length;
        const heroTextOk = !mismatches.length;
        const parseError = parseErrors.join('; ');
        const allOk = coverageOk && countOk && heroTextOk && !extra.length && !parseError;

        return {
            summary: {
                ok: allOk,
                input_stock_video_count: inputStock,
                result_count: resultCount,
                matched_ok: matchedOk,
                missing_count: missing.length,
                extra_count: extra.length,
                mismatch_count: mismatches.length,
                count_ok: countOk,
                coverage_ok: coverageOk,
                hero_text_ok: heroTextOk,
                parse_error: parseError,
            },
            missing: missing,
            extra: extra,
            mismatches: mismatches,
            matched: matched,
        };
    }

    function renderFinderCheck(opts) {
        if (!els.finderCheckWrap) return;
        opts = opts || {};

        if (opts.busy) {
            els.finderCheckWrap.innerHTML = '<div class="rewrite-scene-writer-check__head"><strong>Проверка Finder</strong> <span class="rewrite-scene-writer-check__wait">в процессе...</span></div>';
            return;
        }

        const sceneRaw = els.sceneTa ? els.sceneTa.value : '';
        const resultRaw = els.resultTa ? els.resultTa.value : '';
        if (!String(sceneRaw || '').trim()) {
            els.finderCheckWrap.innerHTML = '<div class="rewrite-scene-writer-check__head"><strong>Проверка Finder</strong> <span class="rewrite-scene-writer-check__wait">ожидание сцены</span></div>';
            return;
        }

        const data = validateFinderStockVideoMatch(sceneRaw, resultRaw);
        const s = data.summary || {};
        const okClass = s.ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no';

        let html = '';
        html += '<div class="rewrite-scene-writer-check__head"><strong>Проверка Finder</strong> <span class="' + okClass + '">' + (s.ok ? 'OK' : 'NO') + '</span></div>';
        html += '<div class="rewrite-scene-writer-check__hint rewrite-editor-check-line">';
        html += '<span class="rewrite-editor-check-line__left">IN Stock_Video: <strong>' + formatNumRu(s.input_stock_video_count || 0) + '</strong></span>';
        html += '<span class="rewrite-editor-check-line__sep">|</span>';
        html += '<span class="rewrite-editor-check-line__center">OUT Result: <strong>' + formatNumRu(s.result_count || 0) + '</strong></span>';
        html += '<span class="rewrite-editor-check-line__sep">|</span>';
        html += '<span class="rewrite-editor-check-line__right">Совпало: <strong>' + formatNumRu(s.matched_ok || 0) + '/' + formatNumRu(s.input_stock_video_count || 0) + '</strong></span>';
        html += '</div>';
        html += '<div class="rewrite-scene-writer-check__hint">';
        html += 'Количество: <span class="' + (s.count_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.count_ok ? 'OK' : 'NO') + '</span> | ';
        html += 'Покрытие Stock_Video: <span class="' + (s.coverage_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.coverage_ok ? 'OK' : 'NO') + '</span> | ';
        html += 'hero_text: <span class="' + (s.hero_text_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.hero_text_ok ? 'OK' : 'NO') + '</span>';
        if (s.missing_count) html += ' | нет в Result: <strong class="rewrite-scene-writer-check__no">' + formatNumRu(s.missing_count) + '</strong>';
        if (s.extra_count) html += ' | лишние в Result: <strong class="rewrite-scene-writer-check__no">' + formatNumRu(s.extra_count) + '</strong>';
        if (s.mismatch_count) html += ' | расхождения: <strong class="rewrite-scene-writer-check__no">' + formatNumRu(s.mismatch_count) + '</strong>';
        html += '</div>';

        function renderIssueList(title, items, cls) {
            if (!items || !items.length) return '';
            let block = '<div class="rewrite-scene-writer-check__hint scenes-stock-finder-check__issues ' + cls + '"><strong>' + title + '</strong>';
            items.forEach(function (it) {
                const sid = String(it.scene_id || '').trim();
                const hero = String(it.hero_text || '').trim();
                const shortHero = hero.length > 72 ? hero.slice(0, 69) + '…' : hero;
                block += '<div class="rewrite-scene-writer-check__row">';
                block += '<span>' + (sid || '—') + '</span>';
                block += '<span title="' + hero.replace(/"/g, '&quot;') + '">' + shortHero + '</span>';
                if (it.result_hero_text && !it.hero_ok) {
                    block += '<span class="rewrite-scene-writer-check__no" title="' + String(it.result_hero_text).replace(/"/g, '&quot;') + '">≠ hero</span>';
                }
                block += '</div>';
            });
            block += '</div>';
            return block;
        }

        html += renderIssueList('Нет в Result', data.missing, 'scenes-stock-finder-check__missing');
        html += renderIssueList('Лишние в Result', data.extra, 'scenes-stock-finder-check__extra');
        html += renderIssueList('Расхождение hero_text / scene_id', data.mismatches, 'scenes-stock-finder-check__mismatch');

        const rows = (data.matched || []).filter(function (r) { return r.ok; });
        if (rows.length && !s.ok) {
            html += '<div class="rewrite-scene-writer-check__divider"></div>';
            const colCount = 3;
            const perCol = Math.ceil(rows.length / colCount);
            html += '<div class="rewrite-scene-writer-check__blocks-grid">';
            for (let c = 0; c < colCount; c += 1) {
                const start = c * perCol;
                const end = Math.min(start + perCol, rows.length);
                html += '<div class="rewrite-scene-writer-check__blocks-col">';
                for (let i = start; i < end; i += 1) {
                    const r = rows[i] || {};
                    html += '<div class="rewrite-scene-writer-check__block-item">';
                    html += '<span>' + String(r.scene_id || '—') + '</span>';
                    html += '<span class="rewrite-scene-writer-check__ok">OK</span>';
                    html += '</div>';
                }
                html += '</div>';
            }
            html += '</div>';
        }

        if (s.parse_error) {
            html += '<div class="rewrite-scene-writer-check__hint">' + String(s.parse_error) + '</div>';
        }

        els.finderCheckWrap.innerHTML = html;
        updateBoardSyncHint();
    }

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function normalizeQueriesList(value) {
        if (!Array.isArray(value)) return [];
        const out = [];
        value.forEach(function (q) {
            const s = String(q || '').trim();
            if (s && out.indexOf(s) < 0) out.push(s);
        });
        return out;
    }

    function buildBoardFromResult(resultRaw, existingBoard) {
        const parsed = parseSceneJsonl(resultRaw);
        const prevById = {};
        (existingBoard || []).forEach(function (row) {
            if (row && row.scene_id) prevById[String(row.scene_id)] = row;
        });
        const out = [];
        parsed.scenes.forEach(function (row) {
            const sid = String(row.scene_id || '').trim();
            const hero = sceneHeroText(row);
            const base = {
                scene_id: sid,
                hero_text: hero,
                text: String(row.text || hero).trim(),
                visual_intent: String(row.visual_intent || '').trim(),
                queries: normalizeQueriesList(row.queries),
                search: { status: 'idle', error: '', items: [], searched_at: '', source: '' },
            };
            const old = prevById[sid];
            if (old && sceneHeroText(old) === hero) {
                if (String(old.visual_intent || '').trim()) base.visual_intent = String(old.visual_intent).trim();
                const oldQ = normalizeQueriesList(old.queries);
                if (oldQ.length) base.queries = oldQ;
            }
            if (old && old.search && typeof old.search === 'object') {
                base.search = {
                    status: String(old.search.status || 'idle'),
                    error: String(old.search.error || ''),
                    items: Array.isArray(old.search.items) ? old.search.items : [],
                    searched_at: String(old.search.searched_at || ''),
                    source: String(old.search.source || ''),
                };
            }
            out.push(base);
        });
        return out;
    }

    function loadInitialBoard() {
        const node = document.getElementById('sst-board-initial');
        if (node && node.textContent) {
            try {
                const raw = JSON.parse(node.textContent);
                if (Array.isArray(raw) && raw.length) {
                    boardScenes = raw;
                    return;
                }
            } catch (e) { /* ignore */ }
        }
        const resultRaw = els.resultTa ? els.resultTa.value : '';
        boardScenes = buildBoardFromResult(resultRaw, []);
    }

    function readBoardSceneFromDom(card) {
        if (!card) return null;
        const sid = String(card.getAttribute('data-scene-id') || '').trim();
        if (!sid) return null;
        const intentTa = card.querySelector('[data-sst-intent]');
        const queries = [];
        card.querySelectorAll('[data-sst-query-input]').forEach(function (inp) {
            const q = String(inp.value || '').trim();
            if (q) queries.push(q);
        });
        const prev = boardScenes.find(function (s) { return String(s.scene_id) === sid; }) || {};
        const search = prev.search && typeof prev.search === 'object' ? prev.search : { status: 'idle', error: '', items: [], searched_at: '', source: '' };
        return {
            scene_id: sid,
            hero_text: String(card.getAttribute('data-hero-text') || prev.hero_text || '').trim(),
            text: String(prev.text || '').trim(),
            visual_intent: intentTa ? String(intentTa.value || '').trim() : String(prev.visual_intent || '').trim(),
            queries: queries,
            search: search,
        };
    }

    function collectBoardFromDom() {
        if (!els.boardWrap) return boardScenes.slice();
        const out = [];
        els.boardWrap.querySelectorAll('[data-sst-board-scene]').forEach(function (card) {
            const row = readBoardSceneFromDom(card);
            if (row) out.push(row);
        });
        return out.length ? out : boardScenes.slice();
    }

    function scheduleBoardSave(delayMs) {
        if (!API_OK) return;
        if (boardSaveTimer) clearTimeout(boardSaveTimer);
        boardSaveTimer = setTimeout(function () {
            boardSaveTimer = null;
            boardScenes = collectBoardFromDom();
            savePrefs({ board: boardScenes }).catch(function () { /* ignore */ });
        }, delayMs == null ? 400 : delayMs);
    }

    function activatePexelsVideoPreview(btn) {
        if (!btn) return;
        const src = String(btn.getAttribute('data-video-src') || '').trim();
        const poster = String(btn.getAttribute('data-video-poster') || '').trim();
        const placeholder = btn.closest('.slot-placeholder');
        if (!placeholder || !src) return;

        if (els.boardWrap) {
            els.boardWrap.querySelectorAll('video.slot-image').forEach(function (video) {
                try { video.pause(); } catch (e) { /* ignore */ }
            });
        }

        const video = document.createElement('video');
        video.className = 'slot-image';
        video.src = src;
        video.controls = true;
        video.playsInline = true;
        video.preload = 'auto';
        if (poster) video.poster = poster;
        placeholder.replaceChildren(video);
        video.play().catch(function () { /* ignore */ });
    }

    function pexelsItemPlaybackUrl(it) {
        const local = String(it && it.local_url || '').trim();
        const remote = String(it && it.media_url || '').trim();
        if (local.indexOf('/scenes-stock/media/') === 0) return local;
        return remote || local;
    }

    function renderSearchResults(search) {
        const s = search || {};
        const status = String(s.status || 'idle');
        const items = Array.isArray(s.items) ? s.items : [];
        if (status === 'searching') {
            return '<span class="slot-status slot-status-with-spinner">Поиск…</span>';
        }
        if (status === 'error') {
            return '<span class="slot-status">' + escapeHtml(s.error || 'Ошибка поиска') + '</span>';
        }
        if (!items.length) {
            return '<span class="slot-status">' + (status === 'done' ? 'Ничего не найдено' : 'Pending') + '</span>';
        }
        let html = '<div class="pexels-slots-grid">';
        items.forEach(function (it, idx) {
            const thumb = String(it.thumbnail_url || '').trim();
            const playUrl = pexelsItemPlaybackUrl(it);
            const kw = String(it.found_by_query || it.found_by_keyword || '').trim();
            const w = Number(it.width || 0);
            const h = Number(it.height || 0);
            const isVideo = String(it.type || '') === 'video';
            html += '<div class="scene-slot pexels-slot-item" data-pexels-index="' + String(idx) + '">';
            html += '<div class="slot-placeholder" data-status="done">';
            if (isVideo && playUrl) {
                html += '<button type="button" class="pexels-slot-preview" data-sst-video-play data-video-src="' + escapeHtml(playUrl) + '" data-video-poster="' + escapeHtml(thumb) + '" title="Воспроизвести" aria-label="Воспроизвести видео">';
                if (thumb) {
                    html += '<img src="' + escapeHtml(thumb) + '" alt="" class="slot-image pexels-slot-thumb" loading="lazy" decoding="async">';
                } else {
                    html += '<span class="pexels-slot-thumb-fallback" aria-hidden="true">Video</span>';
                }
                html += '<span class="pexels-slot-play" aria-hidden="true">▶</span>';
                html += '</button>';
            } else if (playUrl) {
                html += '<img src="' + escapeHtml(playUrl) + '" alt="" class="slot-image" loading="lazy" decoding="async">';
            }
            html += '</div>';
            html += '<div class="pexels-item-meta">';
            html += '<span class="pexels-meta-left">' + (isVideo ? 'Video' : 'Photo') + '</span>';
            html += '<span class="pexels-meta-center">' + escapeHtml(kw || '—') + '</span>';
            html += '<span class="pexels-meta-right">' + String(w || 0) + 'x' + String(h || 0) + '</span>';
            html += '</div>';
            html += '</div>';
        });
        html += '</div>';
        return html;
    }

    function expectedBoardFromResult() {
        const resultRaw = els.resultTa ? els.resultTa.value : '';
        return buildBoardFromResult(resultRaw, []);
    }

    function boardSceneIdsKey(scenes) {
        return (scenes || []).map(function (s) { return String(s.scene_id || '').trim(); }).filter(Boolean).join('|');
    }

    function boardNeedsSyncFromResult() {
        const expected = expectedBoardFromResult();
        if (!expected.length) return false;
        return boardSceneIdsKey(expected) !== boardSceneIdsKey(boardScenes);
    }

    function updateBoardSyncHint() {
        if (!els.boardSyncHint) return;
        const expected = expectedBoardFromResult();
        const resultCount = expected.length;
        const boardCount = boardScenes.length;
        if (!resultCount) {
            els.boardSyncHint.textContent = '';
            els.boardSyncHint.className = 'scenes-stock-board-sync-hint';
            return;
        }
        if (boardNeedsSyncFromResult()) {
            els.boardSyncHint.textContent = 'Result: ' + formatNumRu(resultCount) + ' сцен · в блоке: ' + formatNumRu(boardCount) + ' — нажмите «Обновить сцены»';
            els.boardSyncHint.className = 'scenes-stock-board-sync-hint rewrite-scene-writer-check__no';
        } else {
            els.boardSyncHint.textContent = 'Сцены синхронизированы с Result (' + formatNumRu(boardCount) + ')';
            els.boardSyncHint.className = 'scenes-stock-board-sync-hint rewrite-scene-writer-check__ok';
        }
    }

    function renderStockBoard() {
        if (!els.boardWrap) return;
        if (!boardScenes.length) {
            const resultRaw = els.resultTa ? els.resultTa.value : '';
            if (String(resultRaw || '').trim()) {
                boardScenes = buildBoardFromResult(resultRaw, boardScenes);
            }
        }
        if (!boardScenes.length) {
            els.boardWrap.innerHTML = '<p class="scenes-stock-board-empty rewrite-scene-writer-check__wait">Сначала получите Result от Finder Agent.</p>';
            updateBoardSyncHint();
            return;
        }

        let html = '';
        boardScenes.forEach(function (scene) {
            const sid = String(scene.scene_id || '').trim();
            const hero = String(scene.hero_text || '').trim();
            const intent = String(scene.visual_intent || '').trim();
            const queries = normalizeQueriesList(scene.queries);
            const search = scene.search || {};
            const status = boardSearching[sid] ? 'searching' : String(search.status || 'idle');
            const searchView = boardSearching[sid]
                ? { status: 'searching', items: [], error: '' }
                : Object.assign({}, search, { status: status });

            html += '<article class="scene-card scenes-stock-board-scene" data-sst-board-scene data-scene-id="' + escapeHtml(sid) + '" data-hero-text="' + escapeHtml(hero) + '">';
            html += '<header class="scenes-stock-board-scene__head slot-header">';
            html += '<div class="scenes-stock-board-scene__title">';
            html += '<div class="scene-card-title-row"><span class="scene-id">' + escapeHtml(sid) + '</span></div>';
            if (hero) {
                html += '<div class="scene-text scene-text--en scenes-stock-board-hero" title="' + escapeHtml(hero) + '">' + escapeHtml(hero) + '</div>';
            }
            html += '</div>';
            html += '<button type="button" class="btn-regenerate scenes-stock-board-search' + (boardSearching[sid] ? ' btn-regenerate--busy' : '') + '" data-sst-board-search title="Поиск в Pexels" aria-label="Поиск в Pexels"' + (boardSearching[sid] ? ' disabled' : '') + '>↻</button>';
            html += '</header>';

            html += '<div class="scenes-stock-board-field">';
            html += '<label class="scenes-stock-board-label">Visual intent</label>';
            html += '<textarea class="rewrite-source-textarea scenes-stock-intent-input" rows="2" data-sst-intent placeholder="Visual intent…">' + escapeHtml(intent) + '</textarea>';
            html += '</div>';

            html += '<div class="scenes-stock-board-field scenes-stock-board-field--queries">';
            html += '<label class="scenes-stock-board-label">Queries</label>';
            html += '<div class="scenes-stock-query-list" data-sst-query-list>';
            if (!queries.length) {
                html += '<div class="scenes-stock-query-row"><input type="text" class="scenes-stock-query-input" data-sst-query-input value="" placeholder="Поисковый запрос…"><button type="button" class="scenes-stock-query-remove" data-sst-query-remove title="Удалить запрос" aria-label="Удалить">×</button></div>';
            } else {
                queries.forEach(function (q) {
                    html += '<div class="scenes-stock-query-row"><input type="text" class="scenes-stock-query-input" data-sst-query-input value="' + escapeHtml(q) + '" placeholder="Поисковый запрос…"><button type="button" class="scenes-stock-query-remove" data-sst-query-remove title="Удалить запрос" aria-label="Удалить">×</button></div>';
                });
            }
            html += '</div>';
            html += '<button type="button" class="scenes-stock-query-add" data-sst-query-add>+ запрос</button>';
            html += '</div>';

            html += '<div class="scene-slot scene-slot--pexels scenes-stock-board-results">';
            html += '<div class="slot-placeholder slot-placeholder--pexels scenes-stock-board-results-slot" data-status="' + escapeHtml(status === 'done' ? 'done' : (status === 'error' ? 'error' : 'pending')) + '">';
            html += renderSearchResults(searchView);
            html += '</div></div>';
            html += '</article>';
        });
        els.boardWrap.innerHTML = html;
        updateBoardSyncHint();
    }

    function syncBoardFromResultRaw() {
        const resultRaw = els.resultTa ? els.resultTa.value : '';
        boardScenes = buildBoardFromResult(resultRaw, boardScenes);
        renderStockBoard();
    }

    async function refreshBoardFromResult() {
        if (!API_OK) return;
        syncBoardFromResultRaw();
        if (els.boardSyncBtn) els.boardSyncBtn.disabled = true;
        try {
            await savePrefs({ board: boardScenes, result: els.resultTa ? els.resultTa.value : '' });
            updateBoardSyncHint();
        } catch (e) {
            alert(String(e.message || e));
        } finally {
            if (els.boardSyncBtn) els.boardSyncBtn.disabled = false;
        }
    }

    function applyBoardFromServer(board) {
        if (Array.isArray(board)) {
            boardScenes = board;
            renderStockBoard();
        }
    }

    async function runBoardSearch(sceneId) {
        if (!API_OK || boardSearching[sceneId]) return;
        boardScenes = collectBoardFromDom();
        const scene = boardScenes.find(function (s) { return String(s.scene_id) === String(sceneId); });
        if (!scene) return;
        const queries = normalizeQueriesList(scene.queries);
        if (!queries.length) {
            alert('Добавьте хотя бы один query-запрос.');
            return;
        }

        boardSearching[sceneId] = true;
        renderStockBoard();
        try {
            const r = await fetch('/scenes-stock/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    scene_id: sceneId,
                    source: els.source ? els.source.value : '',
                    visual_intent: scene.visual_intent,
                    queries: queries,
                    board: boardScenes,
                    pexels_api_key: els.pexelsKeyInput ? String(els.pexelsKeyInput.value || '').trim() : '',
                }),
            });
            const data = await r.json().catch(function () { return {}; });
            if (data.board) applyBoardFromServer(data.board);
            if (!r.ok || data.ok === false) {
                throw new Error((data && data.error) || 'search_failed');
            }
            await savePrefs({ board: boardScenes });
        } catch (e) {
            alert(String(e.message || e));
        } finally {
            delete boardSearching[sceneId];
            renderStockBoard();
        }
    }

    function setPexelsKeyBadge(hasKey) {
        if (!els.pexelsKeyBadge) return;
        const yes = !!hasKey;
        els.pexelsKeyBadge.classList.toggle('badge-yes', yes);
        els.pexelsKeyBadge.classList.toggle('badge-no', !yes);
        els.pexelsKeyBadge.textContent = 'Pexels API: ' + (yes ? 'YES' : 'NO');
    }

    async function savePexelsApiKey() {
        if (!els.pexelsKeyInput) return;
        const key = String(els.pexelsKeyInput.value || '').trim();
        if (!key) {
            alert('Введите Pexels API key.');
            els.pexelsKeyInput.focus();
            return;
        }
        if (els.pexelsKeySave) els.pexelsKeySave.disabled = true;
        try {
            await savePrefs({ pexels_api_key: key });
            document.body.setAttribute('data-pexels-key-set', '1');
            setPexelsKeyBadge(true);
            if (els.pexelsKeySave) {
                els.pexelsKeySave.textContent = '✓';
                setTimeout(function () {
                    if (els.pexelsKeySave) els.pexelsKeySave.textContent = 'OK';
                }, 1200);
            }
        } catch (e) {
            alert(String(e.message || e));
        } finally {
            if (els.pexelsKeySave) els.pexelsKeySave.disabled = false;
        }
    }

    function collectPayload(extra) {
        return Object.assign({
            model: els.model ? els.model.value : '',
            source: els.source ? els.source.value : '',
            system_prompt: els.systemTa ? els.systemTa.value : '',
            user_prompt: els.userTa ? els.userTa.value : '',
            scene: els.sceneTa ? els.sceneTa.value : '',
            result: els.resultTa ? els.resultTa.value : '',
            board: boardScenes,
        }, extra || {});
    }

    function parseContentDispositionFilename(cd) {
        if (!cd) return null;
        var q = cd.match(/filename="((?:\\.|[^"\\])*)"/);
        if (q) return q[1].replace(/\\"/g, '"');
        var u = cd.match(/filename=([^;\s]+)/);
        if (u) return u[1].trim().replace(/^["']|["']$/g, '');
        return null;
    }

    async function downloadExport(btn) {
        if (!API_OK) return;
        if (btn) btn.disabled = true;
        var fnameDefault = 'scenes_stock_finder_openai_request.json';
        try {
            var r = await fetch('/scenes-stock/api/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(collectPayload({ agent: 'finder' })),
            });
            var fname = fnameDefault;
            var cd = r.headers.get('Content-Disposition');
            if (cd) {
                var fromHdr = parseContentDispositionFilename(cd);
                if (fromHdr) fname = fromHdr;
            }
            if (!r.ok) {
                var errData = await r.json().catch(function () { return {}; });
                alert((errData && errData.error) || errData.message || 'Не удалось сформировать запрос');
                return;
            }
            var blob = await r.blob();
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = fname;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (e) {
            alert(String(e));
        } finally {
            if (btn) btn.disabled = false;
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

    async function savePrefs(extra) {
        const r = await fetch('/scenes-stock/api/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(collectPayload(extra)),
        });
        const data = await r.json().catch(function () { return {}; });
        if (!r.ok || !data.ok) {
            throw new Error((data && data.error) || 'Ошибка сохранения');
        }
        if (data.prefs && Array.isArray(data.prefs.board)) {
            applyBoardFromServer(data.prefs.board);
        }
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
            if (running && !isError) els.resultTa.classList.remove('rewrite-stage-result--error');
        }

        if (running && !isError) renderFinderCheck({ busy: true });
        else if (!isError) renderFinderCheck();

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
        renderFinderCheck();
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

    document.addEventListener('click', closeAllModePanels);

    bindLockToggle(els.systemToggle, els.systemWrap, els.systemTa, function () {
        setBadgeYesNo(els.systemBadge, els.systemTa.value, 'System Prompt');
    }, { collapseClass: 'rewrite-stage-card--prompt-collapsed' });

    bindLockToggle(els.userToggle, els.userWrap, els.userTa, function () {
        setBadgeYesNo(els.userBadge, els.userTa.value, 'User Prompt');
    }, { collapseClass: 'rewrite-stage-card--stage-user-prompt-collapsed' });

    bindLockToggle(els.sceneToggle, null, els.sceneTa, function () {
        setSceneBadge();
        renderFinderCheck();
    }, { alwaysVisible: true });

    bindLockToggle(els.resultToggle, null, els.resultTa, function () {
        setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
        updateResultCounts();
        renderFinderCheck();
        syncBoardFromResultRaw();
        scheduleBoardSave(0);
    }, { alwaysVisible: true });

    bindModeDropdown(root.querySelector('[data-sst-model-field]'));
    root.querySelectorAll('.rewrite-mode-dropdown-field').forEach(bindModeDropdown);

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

    if (els.exportBtn) {
        els.exportBtn.addEventListener('click', function () {
            downloadExport(els.exportBtn);
        });
    }

    if (els.runBtn) {
        els.runBtn.addEventListener('click', async function () {
            if (!API_OK || generating) return;
            let hadError = false;
            runStartedAt = Date.now();
            abortController = new AbortController();
            pushStageStatus('Finder Agent…');
            renderFinderCheck({ busy: true });
            statusTimer = setInterval(function () {
                pushStageStatus('Finder Agent…');
            }, 1000);
            try {
                await savePrefs();
                const r = await fetch('/scenes-stock/api/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(collectPayload({ agent: 'finder' })),
                    signal: abortController.signal,
                });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    showStageError((data && data.error) || 'generation_failed', data && data.raw);
                    hadError = true;
                    return;
                }
                if (els.resultTa) {
                    els.resultTa.value = data.result || '';
                    setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
                    updateResultCounts();
                }
                renderFinderCheck();
                syncBoardFromResultRaw();
                scheduleBoardSave(0);
                clearStageStatus();
            } catch (e) {
                if (e && e.name === 'AbortError') {
                    clearStageStatus();
                    return;
                }
                hadError = true;
                showStageError(String(e.message || e));
            } finally {
                if (statusTimer) {
                    clearInterval(statusTimer);
                    statusTimer = null;
                }
                abortController = null;
                if (!hadError && generating) clearStageStatus();
            }
        });
    }

    if (els.cancelBtn) {
        els.cancelBtn.addEventListener('click', function () {
            if (abortController) abortController.abort();
        });
    }

    if (els.pexelsKeySave && els.pexelsKeyInput) {
        els.pexelsKeySave.addEventListener('click', function () {
            savePexelsApiKey().catch(function () { /* ignore */ });
        });
        els.pexelsKeyInput.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter') {
                ev.preventDefault();
                savePexelsApiKey().catch(function () { /* ignore */ });
            }
        });
    }

    if (els.boardSyncBtn) {
        els.boardSyncBtn.addEventListener('click', function () {
            refreshBoardFromResult().catch(function () { /* ignore */ });
        });
    }

    if (els.boardWrap) {
        els.boardWrap.addEventListener('click', function (ev) {
            const searchBtn = ev.target.closest('[data-sst-board-search]');
            if (searchBtn) {
                const card = searchBtn.closest('[data-sst-board-scene]');
                const sid = card && card.getAttribute('data-scene-id');
                if (sid) runBoardSearch(sid);
                return;
            }
            const addBtn = ev.target.closest('[data-sst-query-add]');
            if (addBtn) {
                const list = addBtn.closest('[data-sst-board-scene]')?.querySelector('[data-sst-query-list]');
                if (list) {
                    const row = document.createElement('div');
                    row.className = 'scenes-stock-query-row';
                    row.innerHTML = '<input type="text" class="scenes-stock-query-input" data-sst-query-input value="" placeholder="Поисковый запрос…"><button type="button" class="scenes-stock-query-remove" data-sst-query-remove title="Удалить запрос" aria-label="Удалить">×</button>';
                    list.appendChild(row);
                    const inp = row.querySelector('input');
                    if (inp) inp.focus();
                }
                scheduleBoardSave(0);
                return;
            }
            const removeBtn = ev.target.closest('[data-sst-query-remove]');
            if (removeBtn) {
                const row = removeBtn.closest('.scenes-stock-query-row');
                const list = removeBtn.closest('[data-sst-query-list]');
                if (row && list) {
                    if (list.querySelectorAll('.scenes-stock-query-row').length > 1) {
                        row.remove();
                    } else {
                        const inp = row.querySelector('[data-sst-query-input]');
                        if (inp) inp.value = '';
                    }
                    scheduleBoardSave(0);
                }
                return;
            }
            const videoBtn = ev.target.closest('[data-sst-video-play]');
            if (videoBtn) {
                activatePexelsVideoPreview(videoBtn);
            }
        });

        els.boardWrap.addEventListener('input', function (ev) {
            if (ev.target.matches('[data-sst-intent], [data-sst-query-input]')) {
                scheduleBoardSave(400);
            }
        });
    }

    if (els.systemTa) setBadgeYesNo(els.systemBadge, els.systemTa.value, 'System Prompt');
    if (els.userTa) setBadgeYesNo(els.userBadge, els.userTa.value, 'User Prompt');
    setSceneBadge();
    setBadgeYesNo(els.resultBadge, els.resultTa ? els.resultTa.value : '', 'Result');
    updateResultCounts();
    renderFinderCheck();
    loadInitialBoard();
    renderStockBoard();
    setPexelsKeyBadge(
        PEXELS_KEY_SET || (els.pexelsKeyInput && !!(els.pexelsKeyInput.value || '').trim())
    );
    syncResultWrapIdleStatus();
})();
