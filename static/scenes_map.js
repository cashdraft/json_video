(function () {
    'use strict';

    const API_OK = document.body.getAttribute('data-api-key') === '1';
    const root = document.querySelector('[data-scenes-map-agent="macromap"]');
    if (!root) return;

    const els = {
        model: document.getElementById('sm-model'),
        videoDynamics: document.getElementById('sm-video-dynamics'),
        sceneTypes: document.getElementById('sm-scene-types'),
        elementsLabel: root.querySelector('[data-sm-elements-label]'),
        elementsPanel: root.querySelector('.scenes-map-elements-panel'),
        elementsBtn: root.querySelector('[data-sm-elements-dropdown] .rewrite-anim-dropdown__btn'),
        elementsChecks: root.querySelectorAll('[data-sm-elements-checkbox]'),
        systemToggle: root.querySelector('[data-sm-system-toggle]'),
        systemWrap: root.querySelector('[data-sm-system-wrap]'),
        systemTa: root.querySelector('[data-sm-system-prompt]'),
        systemBadge: root.querySelector('[data-sm-system-badge]'),
        userToggle: root.querySelector('[data-sm-user-toggle]'),
        userWrap: root.querySelector('[data-sm-user-wrap]'),
        userTa: root.querySelector('[data-sm-user-prompt]'),
        userBadge: root.querySelector('[data-sm-user-badge]'),
        inboxToggle: root.querySelector('[data-sm-inbox-toggle]'),
        inboxTa: root.querySelector('[data-sm-inbox]'),
        inboxBadge: root.querySelector('[data-sm-inbox-badge]'),
        resultToggle: root.querySelector('[data-sm-result-toggle]'),
        resultWrap: root.querySelector('[data-sm-result-wrap]'),
        resultTa: root.querySelector('[data-sm-result]'),
        resultBadge: root.querySelector('[data-sm-result-badge]'),
        resultCounts: root.querySelector('[data-sm-result-counts]'),
        resultCopy: root.querySelector('[data-sm-result-copy]'),
        runBtn: root.querySelector('[data-sm-run]'),
        statusRow: root.querySelector('[data-sm-status-row]'),
        statusText: root.querySelector('[data-sm-status-text]'),
        cancelBtn: root.querySelector('[data-sm-cancel-btn]'),
        checkWrap: root.querySelector('[data-sm-macromap-check]'),
        resultAsToggle: root.querySelector('[data-sm-result-as-toggle]'),
        resultAsTa: root.querySelector('[data-sm-result-as]'),
        resultAsBadge: root.querySelector('[data-sm-result-as-badge]'),
        resultAsCounts: root.querySelector('[data-sm-result-as-counts]'),
        resultAsCopy: root.querySelector('[data-sm-result-as-copy]'),
        resultAsCheckWrap: root.querySelector('[data-sm-result-as-check]'),
    };

    const MACRO_BLOCK_TYPES = {
        hook: 1, hook_expansion: 1, problem_setup: 1, context: 1, concept_explanation: 1,
        proof: 1, example: 1, escalation: 1, turning_point: 1, solution: 1, warning: 1,
        recap: 1, final_punch: 1, bridge: 1,
    };
    const MACRO_IMPORTANCE = { high: 1, medium: 1, low: 1 };
    const MACRO_REQUIRED_BLOCK_FIELDS = [
        'block_id', 'macro_block_type', 'title', 'start_text', 'end_text', 'goal', 'summary', 'importance',
    ];
    const MACRO_REQUIRED_GLOBAL_FIELDS = [
        'video_core_problem', 'main_promise', 'main_turning_point', 'final_takeaway',
    ];
    const MACRO_RECOMMENDED_MIN = 6;
    const MACRO_RECOMMENDED_MAX = 14;

    let saveTimer = null;
    let generating = false;
    let statusTimer = null;
    let runStartedAt = 0;
    let abortController = null;

    function formatNumRu(n) {
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }

    function formatNumRuSigned(n) {
        const v = Number(n);
        if (!isFinite(v)) return '0';
        return (v > 0 ? '+' : '') + formatNumRu(v);
    }

    function editorCheckDeltaClass(delta) {
        const v = Number(delta);
        if (!isFinite(v) || v === 0) return 'rewrite-editor-check-delta rewrite-editor-check-delta--zero';
        if (v > 0) return 'rewrite-editor-check-delta rewrite-editor-check-delta--pos';
        return 'rewrite-editor-check-delta rewrite-editor-check-delta--neg';
    }

    function editorCheckDeltaHtml(delta) {
        return '<span class="' + editorCheckDeltaClass(delta) + '">' + formatNumRuSigned(delta) + '</span>';
    }

    function stripMarkdownJsonFence(raw) {
        let text = String(raw || '').trim();
        if (text.indexOf('```') === 0) {
            text = text.replace(/^```(?:json)?\s*/i, '');
            text = text.replace(/\s*```$/, '');
        }
        return text.trim();
    }

    function extractJsonObject(raw) {
        const text = stripMarkdownJsonFence(raw);
        if (!text) return '';
        const first = text.indexOf('{');
        const last = text.lastIndexOf('}');
        if (first !== -1 && last !== -1 && last > first) return text.slice(first, last + 1).trim();
        return text;
    }

    function parseMacroMapPayload(raw) {
        const candidate = extractJsonObject(raw);
        if (!candidate) return { payload: null, error: 'Пустой результат' };
        try {
            const obj = JSON.parse(candidate);
            if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
                return { payload: null, error: 'Ожидается JSON-объект' };
            }
            return { payload: obj, error: '' };
        } catch (e) {
            return { payload: null, error: 'Невалидный JSON' };
        }
    }

    function findAnchor(text, anchor, startAt) {
        const needle = String(anchor || '').trim();
        if (!needle || !text) return null;
        const idx = text.indexOf(needle, Math.max(0, startAt || 0));
        return idx >= 0 ? idx : null;
    }

    function extractBlockSpans(inboxText, blocks) {
        const inbox = String(inboxText || '');
        const rows = [];
        let searchFrom = 0;

        blocks.forEach(function (block, i) {
            const startAnchor = String(block.start_text || '').trim();
            const endAnchor = String(block.end_text || '').trim();
            const startIdx = findAnchor(inbox, startAnchor, searchFrom);
            let endIdx = null;
            let endExclusive = null;
            let textSlice = '';
            let boundaryOk = false;

            if (startIdx != null && endAnchor) {
                endIdx = findAnchor(inbox, endAnchor, startIdx);
                if (endIdx != null && endIdx >= startIdx) {
                    endExclusive = endIdx + endAnchor.length;
                    textSlice = inbox.slice(startIdx, endExclusive);
                    boundaryOk = true;
                    searchFrom = endExclusive;
                }
            }

            rows.push({
                index: i + 1,
                block_id: String(block.block_id || ''),
                macro_block_type: String(block.macro_block_type || ''),
                title: String(block.title || ''),
                start_idx: startIdx,
                end_exclusive: endExclusive,
                text: textSlice,
                chars: textSlice.length,
                boundary_ok: boundaryOk,
            });
        });
        return rows;
    }

    function extractBlockSpansFromText(inboxText, blocks) {
        const inbox = String(inboxText || '');
        const rows = [];
        let searchFrom = 0;

        blocks.forEach(function (block, i) {
            const textSlice = String(block.text || '');
            let startIdx = null;
            let endExclusive = null;
            let boundaryOk = false;

            if (textSlice) {
                startIdx = inbox.indexOf(textSlice, searchFrom);
                if (startIdx >= 0) {
                    endExclusive = startIdx + textSlice.length;
                    boundaryOk = true;
                    searchFrom = endExclusive;
                }
            }

            rows.push({
                index: i + 1,
                block_id: String(block.block_id || ''),
                macro_block_type: String(block.macro_block_type || ''),
                title: String(block.title || ''),
                start_idx: startIdx,
                end_exclusive: endExclusive,
                text: textSlice,
                chars: textSlice.length,
                boundary_ok: boundaryOk,
            });
        });
        return rows;
    }

    function buildResultAs(resultRaw, inboxRaw) {
        const parsed = parseMacroMapPayload(resultRaw);
        if (!parsed.payload) {
            return { text: '', spans: [], error: parsed.error || 'Ошибка разбора' };
        }
        const blocks = (Array.isArray(parsed.payload.macro_map) ? parsed.payload.macro_map : [])
            .filter(function (b) { return b && typeof b === 'object'; });
        const spans = extractBlockSpans(inboxRaw, blocks);
        const outBlocks = blocks.map(function (block, i) {
            const out = {};
            Object.keys(block).forEach(function (key) {
                if (key !== 'start_text' && key !== 'end_text') out[key] = block[key];
            });
            out.text = spans[i] ? spans[i].text : '';
            return out;
        });
        const obj = {
            macro_map: outBlocks,
            global_structure_summary: parsed.payload.global_structure_summary || {},
        };
        return { text: JSON.stringify(obj, null, 2), spans: spans, error: '' };
    }

    function quoteSnippet(text, maxLen) {
        maxLen = maxLen == null ? 140 : maxLen;
        const s = String(text || '').replace(/\s+/g, ' ').trim();
        if (s.length <= maxLen) return s;
        return s.slice(0, maxLen - 1) + '…';
    }

    function analyzeAssemblyGaps(inboxText, spans) {
        const inbox = String(inboxText || '');
        const valid = spans.filter(function (s) {
            return s.boundary_ok && s.start_idx != null && s.end_exclusive != null;
        }).sort(function (a, b) { return a.start_idx - b.start_idx; });

        const boundaryWhitespace = [];
        const missingFragments = [];
        const overlaps = [];
        let boundaryWhitespaceChars = 0;
        let missingFragmentChars = 0;

        function appendGap(gapStart, gapEnd, meta) {
            if (gapEnd <= gapStart) return;
            const gapText = inbox.slice(gapStart, gapEnd);
            const entry = Object.assign({
                chars: gapText.length,
                quote: quoteSnippet(gapText),
                start_idx: gapStart,
                end_idx: gapEnd,
            }, meta || {});
            if (gapText.trim() === '') {
                boundaryWhitespaceChars += gapText.length;
                boundaryWhitespace.push(entry);
            } else {
                missingFragmentChars += gapText.length;
                missingFragments.push(entry);
            }
        }

        if (!valid.length) {
            if (inbox) appendGap(0, inbox.length, { position: 'all', after_block_id: '', before_block_id: '' });
            return {
                boundary_whitespace_chars: boundaryWhitespaceChars,
                missing_fragment_chars: missingFragmentChars,
                boundary_whitespace: boundaryWhitespace,
                missing_fragments: missingFragments,
                overlaps: overlaps,
            };
        }

        if (valid[0].start_idx > 0) {
            appendGap(0, valid[0].start_idx, {
                position: 'before_first',
                after_block_id: '',
                before_block_id: valid[0].block_id || '',
            });
        }

        for (let i = 0; i < valid.length - 1; i += 1) {
            const prev = valid[i];
            const nxt = valid[i + 1];
            const prevEnd = prev.end_exclusive;
            const nextStart = nxt.start_idx;
            if (nextStart < prevEnd) {
                overlaps.push({
                    after_block_id: prev.block_id || '',
                    before_block_id: nxt.block_id || '',
                    overlap_chars: prevEnd - nextStart,
                    quote: quoteSnippet(inbox.slice(nextStart, prevEnd)),
                });
                continue;
            }
            if (nextStart > prevEnd) {
                appendGap(prevEnd, nextStart, {
                    position: 'between_blocks',
                    after_block_id: prev.block_id || '',
                    before_block_id: nxt.block_id || '',
                });
            }
        }

        const lastEnd = valid[valid.length - 1].end_exclusive;
        if (lastEnd < inbox.length) {
            appendGap(lastEnd, inbox.length, {
                position: 'after_last',
                after_block_id: valid[valid.length - 1].block_id || '',
                before_block_id: '',
            });
        }

        return {
            boundary_whitespace_chars: boundaryWhitespaceChars,
            missing_fragment_chars: missingFragmentChars,
            boundary_whitespace: boundaryWhitespace,
            missing_fragments: missingFragments,
            overlaps: overlaps,
        };
    }

    function buildPerBlockGapIndex(gaps) {
        const byBlock = {};
        function ensure(id) {
            const key = String(id || '').trim();
            if (!key) return null;
            if (!byBlock[key]) {
                byBlock[key] = {
                    after_ws: 0,
                    after_miss: 0,
                    after_quote: '',
                    after_to: '',
                    before_ws: 0,
                    before_miss: 0,
                    before_quote: '',
                    before_from: '',
                };
            }
            return byBlock[key];
        }

        (gaps.boundary_whitespace || []).forEach(function (g) {
            if (g.after_block_id) {
                const b = ensure(g.after_block_id);
                if (!b) return;
                b.after_ws += Number(g.chars || 0);
                if (g.before_block_id) b.after_to = g.before_block_id;
            } else if (g.before_block_id) {
                const b = ensure(g.before_block_id);
                if (!b) return;
                b.before_ws += Number(g.chars || 0);
            }
        });

        (gaps.missing_fragments || []).forEach(function (g) {
            if (g.after_block_id) {
                const b = ensure(g.after_block_id);
                if (!b) return;
                b.after_miss += Number(g.chars || 0);
                b.after_quote = g.quote || b.after_quote;
                if (g.before_block_id) b.after_to = g.before_block_id;
            } else if (g.before_block_id) {
                const b = ensure(g.before_block_id);
                if (!b) return;
                b.before_miss += Number(g.chars || 0);
                b.before_quote = g.quote || b.before_quote;
                if (g.after_block_id) b.before_from = g.after_block_id;
            }
        });

        (gaps.overlaps || []).forEach(function (g) {
            if (g.after_block_id) {
                const b = ensure(g.after_block_id);
                if (!b) return;
                b.overlap_chars = Number(g.overlap_chars || 0);
                b.overlap_to = g.before_block_id || '';
                b.overlap_quote = g.quote || '';
            }
        });

        return byBlock;
    }

    function blockAssemblyGapHtml(gapInfo) {
        if (!gapInfo) return '';
        const parts = [];
        const miss = Number(gapInfo.after_miss || 0) + Number(gapInfo.before_miss || 0);
        const ws = Number(gapInfo.after_ws || 0) + Number(gapInfo.before_ws || 0);
        if (miss > 0) {
            let label = '−' + formatNumRu(miss) + ' симв.';
            if (gapInfo.after_miss && gapInfo.after_to) {
                label += ' → ' + gapInfo.after_to;
            } else if (gapInfo.before_miss && gapInfo.before_from) {
                label += ' ← ' + gapInfo.before_from;
            }
            parts.push('<span class="scenes-map-block-gap scenes-map-block-gap--miss">' + label + '</span>');
            if (gapInfo.after_quote || gapInfo.before_quote) {
                const q = gapInfo.after_quote || gapInfo.before_quote;
                parts.push('<span class="scenes-map-block-gap-quote">«' + String(q).replace(/</g, '&lt;').replace(/>/g, '&gt;') + '»</span>');
            }
        } else if (ws > 0) {
            parts.push('<span class="scenes-map-block-gap scenes-map-block-gap--ws">−' + formatNumRu(ws) + ' пробел</span>');
        }
        if (gapInfo.overlap_chars) {
            parts.push('<span class="scenes-map-block-gap scenes-map-block-gap--overlap">+' + formatNumRu(gapInfo.overlap_chars) + ' пересеч.</span>');
        }
        return parts.join('');
    }

    function blockAssemblyStatusHtml(gapInfo, boundaryOk) {
        if (gapInfo) {
            const miss = Number(gapInfo.after_miss || 0) + Number(gapInfo.before_miss || 0);
            const ws = Number(gapInfo.after_ws || 0) + Number(gapInfo.before_ws || 0);
            if (miss > 0) {
                return '<span class="rewrite-scene-writer-check__no scenes-map-block-status scenes-map-block-status--miss">−' + formatNumRu(miss) + '</span>';
            }
            if (ws > 0) {
                return '<span class="scenes-map-block-status scenes-map-block-status--ws">−' + formatNumRu(ws) + '␠</span>';
            }
            if (gapInfo.overlap_chars) {
                return '<span class="rewrite-scene-writer-check__no scenes-map-block-status scenes-map-block-status--overlap">+' + formatNumRu(gapInfo.overlap_chars) + '</span>';
            }
        }
        if (boundaryOk) {
            return '<span class="rewrite-scene-writer-check__ok scenes-map-block-status">OK</span>';
        }
        return '<span class="rewrite-scene-writer-check__no scenes-map-block-status">NO</span>';
    }

    function validateResultAsAssembly(resultRaw, inboxRaw) {
        const inboxText = String(inboxRaw || '');
        const inputChars = inboxText.length;
        const empty = {
            ok: false,
            input_chars: inputChars,
            output_chars: 0,
            delta_chars: -inputChars,
            blocks: 0,
            boundaries_ok: 0,
            order_ok: false,
            overlap_ok: false,
            join_ok: false,
            parse_error: '',
        };
        const parsed = parseMacroMapPayload(resultRaw);
        if (!parsed.payload) {
            return {
                summary: Object.assign({}, empty, { parse_error: parsed.error || 'Ошибка разбора Result' }),
                blocks_info: [],
            };
        }
        const blocks = (Array.isArray(parsed.payload.macro_map) ? parsed.payload.macro_map : [])
            .filter(function (b) { return b && typeof b === 'object'; });
        const spans = extractBlockSpans(inboxText, blocks);
        if (!spans.length) {
            return { summary: Object.assign({}, empty, { parse_error: 'Нет блоков' }), blocks_info: [] };
        }

        const joined = spans.map(function (s) { return s.text; }).join('');
        const outputChars = joined.length;
        const joinOk = joined === inboxText;

        let orderOk = true;
        let overlapOk = true;
        let prevEnd = null;
        spans.forEach(function (span) {
            if (!span.boundary_ok || span.start_idx == null || span.end_exclusive == null) {
                orderOk = false;
                overlapOk = false;
                return;
            }
            if (prevEnd != null && span.start_idx < prevEnd) {
                overlapOk = false;
                orderOk = false;
            }
            prevEnd = span.end_exclusive;
        });

        const boundariesOk = spans.filter(function (s) { return s.boundary_ok; }).length;
        const gaps = analyzeAssemblyGaps(inboxText, spans);
        const allOk = boundariesOk === spans.length && joinOk && orderOk && overlapOk && spans.length > 0
            && gaps.missing_fragment_chars === 0 && !gaps.overlaps.length;

        return {
            summary: {
                ok: allOk,
                input_chars: inputChars,
                output_chars: outputChars,
                delta_chars: outputChars - inputChars,
                blocks: spans.length,
                boundaries_ok: boundariesOk,
                order_ok: orderOk,
                overlap_ok: overlapOk,
                join_ok: joinOk,
                boundary_whitespace_chars: gaps.boundary_whitespace_chars,
                missing_fragment_chars: gaps.missing_fragment_chars,
                parse_error: '',
            },
            blocks_info: spans.map(function (s) {
                return {
                    index: s.index,
                    block_id: s.block_id,
                    macro_block_type: s.macro_block_type,
                    chars: s.chars,
                    boundary_ok: s.boundary_ok,
                    ok: s.boundary_ok,
                };
            }),
            gaps: gaps,
        };
    }

    function validateResultAsFromPayload(resultAsRaw, inboxRaw) {
        const inboxText = String(inboxRaw || '');
        const inputChars = inboxText.length;
        const empty = {
            ok: false,
            input_chars: inputChars,
            output_chars: 0,
            delta_chars: -inputChars,
            blocks: 0,
            boundaries_ok: 0,
            order_ok: false,
            overlap_ok: false,
            join_ok: false,
            parse_error: '',
        };
        const parsed = parseMacroMapPayload(resultAsRaw);
        if (!parsed.payload) {
            return {
                summary: Object.assign({}, empty, { parse_error: parsed.error || 'Ошибка разбора Result AS' }),
                blocks_info: [],
            };
        }
        const blocks = (Array.isArray(parsed.payload.macro_map) ? parsed.payload.macro_map : [])
            .filter(function (b) { return b && typeof b === 'object'; });
        const spans = extractBlockSpansFromText(inboxText, blocks);
        if (!spans.length) {
            return { summary: Object.assign({}, empty, { parse_error: 'Нет блоков' }), blocks_info: [] };
        }

        const joined = spans.map(function (s) { return s.text; }).join('');
        const outputChars = joined.length;
        const joinOk = joined === inboxText;

        let orderOk = true;
        let overlapOk = true;
        let prevEnd = null;
        spans.forEach(function (span) {
            if (!span.boundary_ok || span.start_idx == null || span.end_exclusive == null) {
                orderOk = false;
                overlapOk = false;
                return;
            }
            if (prevEnd != null && span.start_idx < prevEnd) {
                overlapOk = false;
                orderOk = false;
            }
            prevEnd = span.end_exclusive;
        });

        const boundariesOk = spans.filter(function (s) { return s.boundary_ok; }).length;
        const gaps = analyzeAssemblyGaps(inboxText, spans);
        const allOk = boundariesOk === spans.length && joinOk && orderOk && overlapOk && spans.length > 0
            && gaps.missing_fragment_chars === 0 && !gaps.overlaps.length;

        return {
            summary: {
                ok: allOk,
                input_chars: inputChars,
                output_chars: outputChars,
                delta_chars: outputChars - inputChars,
                blocks: spans.length,
                boundaries_ok: boundariesOk,
                order_ok: orderOk,
                overlap_ok: overlapOk,
                join_ok: joinOk,
                boundary_whitespace_chars: gaps.boundary_whitespace_chars,
                missing_fragment_chars: gaps.missing_fragment_chars,
                parse_error: '',
            },
            blocks_info: spans.map(function (s) {
                return {
                    index: s.index,
                    block_id: s.block_id,
                    macro_block_type: s.macro_block_type,
                    chars: s.chars,
                    boundary_ok: s.boundary_ok,
                    ok: s.boundary_ok,
                };
            }),
            gaps: gaps,
        };
    }

    function updateResultAsCounts() {
        if (!els.resultAsCounts || !els.resultAsTa) return;
        const text = els.resultAsTa.value || '';
        const words = text.trim() ? text.trim().split(/\s+/).length : 0;
        els.resultAsCounts.textContent = text.trim()
            ? formatNumRu(String(text.length)) + ' симв. · ' + formatNumRu(String(words)) + ' сл.'
            : '';
    }

    function refreshResultAs() {
        const resultRaw = els.resultTa ? String(els.resultTa.value || '') : '';
        const inboxRaw = els.inboxTa ? String(els.inboxTa.value || '') : '';
        const built = buildResultAs(resultRaw, inboxRaw);
        if (els.resultAsTa) els.resultAsTa.value = built.text || '';
        setBadgeYesNo(els.resultAsBadge, els.resultAsTa ? els.resultAsTa.value : '', 'Result AS');
        emitMacromapResultAs();
        updateResultAsCounts();
        renderResultAsCheck();
    }

    function renderResultAsCheck(opts) {
        if (!els.resultAsCheckWrap) return;
        opts = opts || {};

        if (opts.busy) {
            els.resultAsCheckWrap.innerHTML = '<div class="rewrite-scene-writer-check__head"><strong>Проверка Result AS</strong> <span class="rewrite-scene-writer-check__wait">в процессе...</span></div>';
            return;
        }

        const resultRaw = els.resultTa ? String(els.resultTa.value || '') : '';
        const inboxRaw = els.inboxTa ? String(els.inboxTa.value || '') : '';
        const resultAsRaw = els.resultAsTa ? String(els.resultAsTa.value || '').trim() : '';
        const trimmed = resultRaw.trim();
        const isErr = /^\s*Ошибка:/.test(resultRaw)
            || !!(els.resultTa && els.resultTa.classList.contains('rewrite-stage-result--error'));
        const isPending = !trimmed.length || /^pending$/i.test(trimmed);

        if (!resultAsRaw && (isErr || isPending)) {
            els.resultAsCheckWrap.innerHTML = '<div class="rewrite-scene-writer-check__head"><strong>Проверка Result AS</strong> <span class="rewrite-scene-writer-check__wait">ожидание данных</span></div>';
            return;
        }

        const check = resultAsRaw
            ? validateResultAsFromPayload(resultAsRaw, inboxRaw)
            : validateResultAsAssembly(resultRaw, inboxRaw);
        const s = check.summary || {};
        const rows = check.blocks_info || [];
        const okClass = s.ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no';
        const delta = Number(s.delta_chars || 0);

        let html = '';
        html += '<div class="rewrite-scene-writer-check__head"><strong>Проверка Result AS</strong> <span class="' + okClass + '">' + (s.ok ? 'OK' : 'NO') + '</span></div>';
        html += '<div class="rewrite-scene-writer-check__hint rewrite-editor-check-line">';
        html += '<span class="rewrite-editor-check-line__left">IN: <strong>' + formatNumRu(s.input_chars || 0) + '</strong></span>';
        html += '<span class="rewrite-editor-check-line__sep">|</span>';
        html += '<span class="rewrite-editor-check-line__center">OUT: <strong>' + formatNumRu(s.output_chars || 0) + '</strong> (' + editorCheckDeltaHtml(delta) + ')</span>';
        html += '<span class="rewrite-editor-check-line__sep">|</span>';
        html += '<span class="rewrite-editor-check-line__right">Блоков: <strong>' + formatNumRu(s.blocks || 0) + '</strong></span>';
        html += '</div>';
        html += '<div class="rewrite-scene-writer-check__hint">';
        html += 'Склейка = Inbox: <span class="' + (s.join_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.join_ok ? 'OK' : 'NO') + '</span> | ';
        html += 'Порядок: <span class="' + (s.order_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.order_ok ? 'OK' : 'NO') + '</span> | ';
        html += 'Без пересечений: <span class="' + (s.overlap_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.overlap_ok ? 'OK' : 'NO') + '</span> | ';
        html += 'Границы: <strong>' + formatNumRu(s.boundaries_ok || 0) + '/' + formatNumRu(s.blocks || 0) + '</strong>';
        html += '</div>';

        const gaps = check.gaps || {};
        const wsChars = Number(s.boundary_whitespace_chars || gaps.boundary_whitespace_chars || 0);
        const missChars = Number(s.missing_fragment_chars || gaps.missing_fragment_chars || 0);
        if (wsChars > 0 || missChars > 0 || (gaps.overlaps && gaps.overlaps.length)) {
            html += '<div class="rewrite-scene-writer-check__hint scenes-map-gap-summary">';
            html += 'Пробелы на стыках: <strong>' + formatNumRu(wsChars) + '</strong> симв.';
            html += ' | Пропуски в разметке: <strong class="' + (missChars ? 'rewrite-scene-writer-check__no' : 'rewrite-scene-writer-check__ok') + '">' + formatNumRu(missChars) + '</strong> симв.';
            html += '</div>';
        }

        function renderGapList(title, items, cls) {
            if (!items || !items.length) return '';
            let block = '<div class="rewrite-scene-writer-check__hint scenes-map-gap-list ' + cls + '"><strong>' + title + '</strong>';
            items.forEach(function (g) {
                let where = '';
                if (g.after_block_id && g.before_block_id) {
                    where = g.after_block_id + ' → ' + g.before_block_id;
                } else if (g.after_block_id) {
                    where = 'после ' + g.after_block_id;
                } else if (g.before_block_id) {
                    where = 'до ' + g.before_block_id;
                } else {
                    where = String(g.position || '—');
                }
                block += '<div class="scenes-map-gap-item"><span class="scenes-map-gap-where">' + where + '</span>';
                block += ' <span class="scenes-map-gap-chars">' + formatNumRu(g.chars || g.overlap_chars || 0) + ' симв.</span>';
                if (g.quote) {
                    block += '<div class="scenes-map-gap-quote">«' + String(g.quote).replace(/</g, '&lt;').replace(/>/g, '&gt;') + '»</div>';
                }
                block += '</div>';
            });
            block += '</div>';
            return block;
        }

        html += renderGapList('Пробелы на стыках якорей', gaps.boundary_whitespace, 'scenes-map-gap-list--ws');
        html += renderGapList('Пропущенные фрагменты', gaps.missing_fragments, 'scenes-map-gap-list--miss');
        if (gaps.overlaps && gaps.overlaps.length) {
            html += renderGapList('Пересечения блоков', gaps.overlaps.map(function (o) {
                return {
                    after_block_id: o.after_block_id,
                    before_block_id: o.before_block_id,
                    chars: o.overlap_chars,
                    quote: o.quote,
                };
            }), 'scenes-map-gap-list--overlap');
        }

        if (rows.length) {
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
                    html += '<span>' + String(r.block_id || ('block_' + String(r.index || (i + 1)))) + '</span>';
                    html += '<span>' + String(r.macro_block_type || '—') + '</span>';
                    html += '<span>симв: ' + formatNumRu(r.chars || 0) + '</span>';
                    html += '<span class="' + (r.boundary_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (r.ok ? 'OK' : 'NO') + '</span>';
                    html += '</div>';
                }
                html += '</div>';
            }
            html += '</div>';
        }

        if (s.parse_error) {
            html += '<div class="rewrite-scene-writer-check__hint">' + String(s.parse_error) + '</div>';
        }

        els.resultAsCheckWrap.innerHTML = html;
    }

    function blockSchemaOk(block) {
        const issues = [];
        MACRO_REQUIRED_BLOCK_FIELDS.forEach(function (field) {
            const val = block[field];
            if (val == null || !String(val).trim()) issues.push(field);
        });
        const mtype = String(block.macro_block_type || '').trim();
        if (mtype && !MACRO_BLOCK_TYPES[mtype]) issues.push('macro_block_type');
        const imp = String(block.importance || '').trim().toLowerCase();
        if (imp && !MACRO_IMPORTANCE[imp]) issues.push('importance');
        return { ok: issues.length === 0, issues: issues };
    }

    function validateMacroMap(resultRaw, inboxRaw) {
        const inboxText = String(inboxRaw || '');
        const inputChars = inboxText.trim().length;
        const parsed = parseMacroMapPayload(resultRaw);
        if (!parsed.payload) {
            return {
                summary: {
                    ok: false,
                    json_ok: false,
                    blocks: 0,
                    boundaries_ok: 0,
                    schema_ok: 0,
                    global_ok: false,
                    input_chars: inputChars,
                    recommended_blocks_ok: false,
                    parse_error: parsed.error || 'Ошибка разбора',
                },
                blocks_info: [],
            };
        }

        const payload = parsed.payload;
        const macroMap = Array.isArray(payload.macro_map) ? payload.macro_map : [];
        const blocks = macroMap.filter(function (b) { return b && typeof b === 'object'; });
        const globalSummary = payload.global_structure_summary;
        const globalOk = !!(globalSummary && typeof globalSummary === 'object' && !Array.isArray(globalSummary)
            && MACRO_REQUIRED_GLOBAL_FIELDS.every(function (field) {
                return String(globalSummary[field] || '').trim();
            }));

        let boundariesOk = 0;
        let schemaOk = 0;
        let searchFrom = 0;
        const blocksInfo = blocks.map(function (block, i) {
            const schema = blockSchemaOk(block);
            if (schema.ok) schemaOk += 1;

            const startAnchor = String(block.start_text || '').trim();
            const endAnchor = String(block.end_text || '').trim();
            const startIdx = findAnchor(inboxText, startAnchor, searchFrom);
            let endIdx = null;
            if (startIdx != null) endIdx = findAnchor(inboxText, endAnchor, startIdx);
            const boundaryOk = startIdx != null && endIdx != null && endIdx >= startIdx;
            if (boundaryOk) {
                boundariesOk += 1;
                searchFrom = endIdx + Math.max(1, endAnchor.length);
            }

            return {
                index: i + 1,
                block_id: String(block.block_id || ''),
                macro_block_type: String(block.macro_block_type || ''),
                title: String(block.title || ''),
                schema_ok: schema.ok,
                boundary_ok: boundaryOk,
                ok: schema.ok && boundaryOk,
                issues: schema.issues,
            };
        });

        const blockCount = blocks.length;
        const recommendedOk = blockCount >= MACRO_RECOMMENDED_MIN && blockCount <= MACRO_RECOMMENDED_MAX;
        const allOk = blockCount > 0
            && schemaOk === blockCount
            && boundariesOk === blockCount
            && globalOk;

        return {
            summary: {
                ok: allOk,
                json_ok: true,
                blocks: blockCount,
                boundaries_ok: boundariesOk,
                schema_ok: schemaOk,
                global_ok: globalOk,
                input_chars: inputChars,
                recommended_blocks_ok: recommendedOk,
                parse_error: '',
            },
            blocks_info: blocksInfo,
        };
    }

    function renderMacroMapCheck(opts) {
        if (!els.checkWrap) return;
        opts = opts || {};

        if (opts.busy) {
            els.checkWrap.innerHTML = '<div class="rewrite-scene-writer-check__head"><strong>Проверка MacroMap Agent</strong> <span class="rewrite-scene-writer-check__wait">в процессе...</span></div>';
            return;
        }

        const resultRaw = els.resultTa ? String(els.resultTa.value || '') : '';
        const inboxRaw = els.inboxTa ? String(els.inboxTa.value || '') : '';
        const trimmed = resultRaw.trim();
        const isErr = /^\s*Ошибка:/.test(resultRaw)
            || !!(els.resultTa && els.resultTa.classList.contains('rewrite-stage-result--error'));
        const isPending = !trimmed.length || /^pending$/i.test(trimmed);

        if (isErr || isPending) {
            els.checkWrap.innerHTML = '<div class="rewrite-scene-writer-check__head"><strong>Проверка MacroMap Agent</strong> <span class="rewrite-scene-writer-check__wait">ожидание данных</span></div>';
            return;
        }

        const check = validateMacroMap(resultRaw, inboxRaw);
        const s = check.summary || {};
        const rows = check.blocks_info || [];
        const okClass = s.ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no';
        let headLabel = s.ok ? 'OK' : 'NO';
        if (!s.json_ok) headLabel = 'NO';

        let html = '';
        html += '<div class="rewrite-scene-writer-check__head"><strong>Проверка MacroMap Agent</strong> <span class="' + okClass + '">' + headLabel + '</span></div>';
        html += '<div class="rewrite-scene-writer-check__hint rewrite-editor-check-line">';
        html += '<span class="rewrite-editor-check-line__left">IN: <strong>' + formatNumRu(s.input_chars || 0) + '</strong></span>';
        html += '<span class="rewrite-editor-check-line__sep">|</span>';
        html += '<span class="rewrite-editor-check-line__center">Блоков: <strong>' + formatNumRu(s.blocks || 0) + '</strong></span>';
        html += '<span class="rewrite-editor-check-line__sep">|</span>';
        html += '<span class="rewrite-editor-check-line__right">Границы: <strong>' + formatNumRu(s.boundaries_ok || 0) + '/' + formatNumRu(s.blocks || 0) + '</strong></span>';
        html += '</div>';
        html += '<div class="rewrite-scene-writer-check__hint">';
        html += 'Схема: <strong>' + formatNumRu(s.schema_ok || 0) + '/' + formatNumRu(s.blocks || 0) + '</strong> | ';
        html += 'Global summary: <span class="' + (s.global_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.global_ok ? 'OK' : 'NO') + '</span> | ';
        html += 'Диапазон 6–14: <span class="' + (s.recommended_blocks_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (s.recommended_blocks_ok ? 'OK' : 'NO') + '</span>';
        html += '</div>';
        if (s.parse_error) {
            html += '<div class="rewrite-scene-writer-check__hint">' + String(s.parse_error) + '</div>';
        }

        if (rows.length) {
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
                    html += '<span>' + String(r.block_id || ('block_' + String(r.index || (i + 1)))) + '</span>';
                    html += '<span>' + String(r.macro_block_type || '—') + '</span>';
                    html += '<span class="' + (r.boundary_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (r.boundary_ok ? 'границы OK' : 'границы NO') + '</span>';
                    html += '<span class="' + (r.schema_ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (r.schema_ok ? 'схема OK' : 'схема NO') + '</span>';
                    html += '<span class="' + (r.ok ? 'rewrite-scene-writer-check__ok' : 'rewrite-scene-writer-check__no') + '">' + (r.ok ? 'OK' : 'NO') + '</span>';
                    html += '</div>';
                }
                html += '</div>';
            }
            html += '</div>';
        }

        els.checkWrap.innerHTML = html;
    }

    function updateResultCounts() {
        if (!els.resultCounts || !els.resultTa) return;
        const text = els.resultTa.value || '';
        const words = text.trim() ? text.trim().split(/\s+/).length : 0;
        els.resultCounts.textContent = text.trim()
            ? formatNumRu(String(text.length)) + ' симв. · ' + formatNumRu(String(words)) + ' сл.'
            : '';
    }

    function emitMacromapResultAs() {
        if (!els.resultAsTa) return;
        document.dispatchEvent(new CustomEvent('scenes-map-macromap-result-as', {
            bubbles: true,
            detail: { result_as: els.resultAsTa.value || '' },
        }));
    }

    function emitMacromapResult() {
        if (!els.resultTa) return;
        document.dispatchEvent(new CustomEvent('scenes-map-macromap-result', {
            bubbles: true,
            detail: { result: els.resultTa.value || '' },
        }));
    }

    function setBadgeYesNo(badge, hasText, prefix) {
        if (!badge) return;
        const yes = !!(hasText && String(hasText).trim());
        badge.classList.remove('badge-yes', 'badge-no');
        badge.classList.add(yes ? 'badge-yes' : 'badge-no');
        if (prefix) badge.textContent = prefix + ': ' + (yes ? 'YES' : 'NO');
    }

    function setInboxBadge() {
        if (!els.inboxBadge || !els.inboxTa) return;
        const yes = !!(els.inboxTa.value || '').trim();
        els.inboxBadge.classList.toggle('badge-yes', yes);
        els.inboxBadge.classList.toggle('badge-no', !yes);
    }

    function collectPayload(extra) {
        const elements = [];
        els.elementsChecks.forEach(function (cb) {
            if (cb.checked) elements.push(cb.value);
        });
        return Object.assign({
            model: els.model ? els.model.value : '',
            video_dynamics_mode: els.videoDynamics ? els.videoDynamics.value : '',
            scene_types_mode: els.sceneTypes ? els.sceneTypes.value : '',
            elements_used: elements,
            system_prompt: els.systemTa ? els.systemTa.value : '',
            user_prompt: els.userTa ? els.userTa.value : '',
            inbox: els.inboxTa ? els.inboxTa.value : '',
            result: els.resultTa ? els.resultTa.value : '',
            result_as: els.resultAsTa ? els.resultAsTa.value : '',
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

    async function downloadScenesMapExport(agent, collectFn, btn) {
        if (!API_OK) return;
        if (btn) btn.disabled = true;
        var fnameDefault = agent === 'scenemap'
            ? 'scenes_map_scenemap_openai_request.json'
            : 'scenes_map_macromap_openai_request.json';
        try {
            var r = await fetch('/scenes-map/api/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({ agent: agent }, collectFn())),
            });
            var fname = fnameDefault;
            var cd = r.headers.get('Content-Disposition');
            if (cd) {
                var fromHdr = parseContentDispositionFilename(cd);
                if (fromHdr) fname = fromHdr;
            }
            if (/\.txt$/i.test(fname)) {
                fname = fname.replace(/\.txt$/i, '.json');
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

    window.scenesMapDownloadExport = downloadScenesMapExport;

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
        renderMacroMapCheck();
        renderResultAsCheck();
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
            if (running && !isError) {
                els.resultTa.classList.remove('rewrite-stage-result--error');
            }
        }

        generating = running;
        if (els.runBtn) els.runBtn.disabled = running || !API_OK;
        if (running && !isError) {
            renderMacroMapCheck({ busy: true });
            renderResultAsCheck({ busy: true });
        }
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

    function showStageError(text) {
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
            els.resultTa.value = 'Ошибка: ' + text;
            els.resultTa.classList.remove('rewrite-stage-result--busy');
            els.resultTa.classList.add('rewrite-stage-result--error');
        }
        if (els.resultAsTa) els.resultAsTa.value = '';
        updateResultCounts();
        renderMacroMapCheck();
        setBadgeYesNo(els.resultAsBadge, '', 'Result AS');
        updateResultAsCounts();
        renderResultAsCheck();
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

        root.querySelector('[data-sm-elements-select-all]')?.addEventListener('click', function (e) {
            e.preventDefault();
            els.elementsChecks.forEach(function (cb) { cb.checked = true; });
            updateElementsLabel();
            scheduleSave(0);
        });

        root.querySelector('[data-sm-elements-clear-all]')?.addEventListener('click', function (e) {
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

    document.addEventListener('click', function () {
        closeAllModePanels();
    });

    bindLockToggle(els.systemToggle, els.systemWrap, els.systemTa, function () {
        setBadgeYesNo(els.systemBadge, els.systemTa.value, 'System Prompt');
    }, { collapseClass: 'rewrite-stage-card--prompt-collapsed' });

    bindLockToggle(els.userToggle, els.userWrap, els.userTa, function () {
        setBadgeYesNo(els.userBadge, els.userTa.value, 'User Prompt');
    }, { collapseClass: 'rewrite-stage-card--stage-user-prompt-collapsed' });

    bindLockToggle(els.inboxToggle, null, els.inboxTa, function () {
        setInboxBadge();
        renderMacroMapCheck();
        refreshResultAs();
    }, { alwaysVisible: true });

    bindLockToggle(els.resultToggle, null, els.resultTa, function () {
        setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
        updateResultCounts();
        renderMacroMapCheck();
        refreshResultAs();
    }, { alwaysVisible: true });

    bindLockToggle(els.resultAsToggle, null, els.resultAsTa, function () {
        setBadgeYesNo(els.resultAsBadge, els.resultAsTa ? els.resultAsTa.value : '', 'Result AS');
        updateResultAsCounts();
        emitMacromapResultAs();
        renderResultAsCheck();
    }, { alwaysVisible: true });

    const modelField = root.querySelector('[data-sm-model-field]');
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

    if (els.resultAsCopy && els.resultAsTa) {
        els.resultAsCopy.addEventListener('click', function () {
            const text = els.resultAsTa.value || '';
            if (!text.trim()) return;
            navigator.clipboard.writeText(text).catch(function () { /* ignore */ });
        });
    }

    if (els.resultAsTa) {
        els.resultAsTa.addEventListener('input', function () {
            updateResultAsCounts();
        });
    }

    const exportBtn = document.querySelector('[data-sm-export]');
    if (exportBtn) {
        exportBtn.addEventListener('click', function () {
            downloadScenesMapExport('macromap', collectPayload, exportBtn);
        });
    }

    if (els.resultTa) {
        els.resultTa.addEventListener('input', function () {
            updateResultCounts();
            renderMacroMapCheck();
            refreshResultAs();
            emitMacromapResult();
        });
        updateResultCounts();
    }

    if (els.inboxTa) {
        els.inboxTa.addEventListener('input', function () {
            renderMacroMapCheck();
            refreshResultAs();
        });
    }

    if (els.runBtn) {
        els.runBtn.addEventListener('click', async function () {
            if (!API_OK || generating) return;
            let hadError = false;
            runStartedAt = Date.now();
            abortController = new AbortController();
            pushStageStatus('MacroMap Agent…');
            statusTimer = setInterval(function () {
                pushStageStatus('MacroMap Agent…');
            }, 1000);
            try {
                await savePrefs();
                const r = await fetch('/scenes-map/api/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ agent: 'macromap' }),
                    signal: abortController.signal,
                });
                const data = await r.json().catch(function () { return {}; });
                if (!r.ok || !data.ok) {
                    throw new Error((data && data.error) || 'Ошибка генерации');
                }
                if (els.resultTa) {
                    els.resultTa.value = data.result || '';
                    setBadgeYesNo(els.resultBadge, els.resultTa.value, 'Result');
                    updateResultCounts();
                    renderMacroMapCheck();
                    emitMacromapResult();
                }
                if (els.resultAsTa) {
                    els.resultAsTa.value = data.result_as || '';
                    if (!els.resultAsTa.value.trim()) refreshResultAs();
                    else emitMacromapResultAs();
                    setBadgeYesNo(els.resultAsBadge, els.resultAsTa.value, 'Result AS');
                    updateResultAsCounts();
                    renderResultAsCheck();
                } else {
                    refreshResultAs();
                }
                if (API_OK) scheduleSave(0);
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

    setInboxBadge();
    syncResultWrapIdleStatus();
    renderMacroMapCheck();
    emitMacromapResult();
    if (els.resultAsTa && (els.resultAsTa.value || '').trim()) {
        emitMacromapResultAs();
        updateResultAsCounts();
        renderResultAsCheck();
    } else if (els.resultAsTa && !(els.resultAsTa.value || '').trim()) {
        refreshResultAs();
    } else {
        updateResultAsCounts();
        renderResultAsCheck();
    }
})();
