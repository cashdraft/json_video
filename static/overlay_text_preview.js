(function (global) {
    'use strict';

    function stripJsonFence(text) {
        var s = String(text || '').trim();
        if (!s) return '';
        var m = s.match(/```(?:json)?\s*([\s\S]*?)```/i);
        if (m) return m[1].trim();
        return s;
    }

    function parseOverlayResult(raw) {
        var body = stripJsonFence(raw);
        if (!body) return { error: 'Result пустой.' };
        var start = body.indexOf('{');
        var end = body.lastIndexOf('}');
        if (start < 0 || end <= start) return { error: 'В Result нет JSON.' };
        try {
            var data = JSON.parse(body.slice(start, end + 1));
            if (!data || typeof data !== 'object') return { error: 'Result не объект.' };
            if (!data.overlay || typeof data.overlay !== 'object') return { error: 'Нет overlay.' };
            return { data: data };
        } catch (e) {
            return { error: 'JSON: ' + String(e.message || e) };
        }
    }

    function asNum(v, fallback) {
        var n = Number(v);
        return Number.isFinite(n) ? n : fallback;
    }

    function normAnim(name) {
        return String(name || 'fade-in').trim().toLowerCase().replace(/_/g, '-');
    }

    function clamp01(v) {
        return Math.min(1, Math.max(0, v));
    }

    function easeOut(t) {
        return 1 - (1 - t) * (1 - t);
    }

    function applyInAnim(anim, p, state) {
        var t = easeOut(clamp01(p));
        var next = Object.assign({}, state, { visible: true });
        switch (anim) {
            case 'fly-up':
            case 'fade-up':
                next.opacity = t;
                next.translateY = (1 - t) * 28;
                break;
            case 'scale-in':
            case 'scale-fade':
            case 'scale-pop':
            case 'big-word-pop':
                next.opacity = anim === 'scale-fade' ? t : 1;
                next.scale = 0.86 + 0.14 * t;
                break;
            default:
                next.opacity = anim === 'none' ? 1 : t;
        }
        return next;
    }

    function applyOutAnim(anim, p, state) {
        var t = easeOut(clamp01(p));
        var next = Object.assign({}, state, { visible: t < 1 });
        switch (anim) {
            case 'fly-up':
            case 'fade-up':
                next.opacity = 1 - t;
                next.translateY = t * 12;
                break;
            case 'scale-in':
            case 'scale-fade':
                next.opacity = 1 - t;
                next.scale = 1 - 0.06 * t;
                break;
            default:
                next.opacity = 1 - t;
        }
        return next;
    }

    function overlayAnimAtTimeSec(timeSec, timing, resolvedAnimation) {
        var hidden = { visible: false, opacity: 0, translateX: 0, translateY: 0, scale: 1 };
        var start = asNum(timing && timing.start_sec, 0);
        var end = asNum(timing && timing.end_sec, 0);
        var inDur = Math.max(0, asNum(timing && timing.in_duration_sec, 0.45));
        var outDur = Math.max(0, asNum(timing && timing.out_duration_sec, 0.3));
        if (end <= start || timeSec < start || timeSec > end) return hidden;

        var inAnim = normAnim(resolvedAnimation && resolvedAnimation.in && resolvedAnimation.in.animation);
        var outAnim = normAnim(
            (resolvedAnimation && resolvedAnimation.out && resolvedAnimation.out.animation) || 'fade-out'
        );
        var inEnd = start + inDur;
        var outStart = end - outDur;

        if (inDur > 0 && timeSec < inEnd) {
            return applyInAnim(inAnim, (timeSec - start) / inDur, hidden);
        }
        if (outDur > 0 && timeSec > outStart) {
            return applyOutAnim(outAnim, (timeSec - outStart) / outDur, Object.assign({}, hidden, {
                visible: true,
                opacity: 1,
            }));
        }
        return { visible: true, opacity: 1, translateX: 0, translateY: 0, scale: 1 };
    }

    function createPreview(root, getImageUrl, getResultText, getRemotionPayload) {
        var bg = root.querySelector('[data-ot-preview-bg]');
        var overlayEl = root.querySelector('[data-ot-preview-overlay]');
        var panelEl = root.querySelector('[data-ot-preview-panel]');
        var linesEl = root.querySelector('[data-ot-preview-lines]');
        var stageEl = root.querySelector('[data-ot-preview-stage]');
        var statusEl = root.querySelector('[data-ot-preview-status]');
        var playBtn = root.querySelector('[data-ot-preview-play]');
        var restartBtn = root.querySelector('[data-ot-preview-restart]');
        var scrub = root.querySelector('[data-ot-preview-scrub]');
        var timeEl = root.querySelector('[data-ot-preview-time]');
        var studioBtn = root.querySelector('[data-ot-preview-studio]');
        var propsTa = root.querySelector('[data-ot-remotion-props]');
        var propsCopyBtn = root.querySelector('[data-ot-remotion-props-copy]');

        var playing = false;
        var rafId = 0;
        var timeSec = 0;
        var durationSec = 5;
        var fps = 30;
        var parsed = null;
        var lastTick = 0;

        function setStatus(text, kind) {
            if (!statusEl) return;
            statusEl.textContent = text || '';
            statusEl.classList.remove(
                'overlay-text-remotion__status--ok',
                'overlay-text-remotion__status--err',
                'overlay-text-remotion__status--run'
            );
            if (kind === 'ok') statusEl.classList.add('overlay-text-remotion__status--ok');
            if (kind === 'err') statusEl.classList.add('overlay-text-remotion__status--err');
            if (kind === 'run') statusEl.classList.add('overlay-text-remotion__status--run');
        }

        function formatTime(sec) {
            return asNum(sec, 0).toFixed(2) + 's';
        }

        function syncTimeUi() {
            if (scrub) {
                scrub.max = String(Math.max(0.01, durationSec));
                scrub.value = String(Math.min(durationSec, Math.max(0, timeSec)));
            }
            if (timeEl) {
                timeEl.textContent = formatTime(timeSec) + ' / ' + formatTime(durationSec);
            }
        }

        function applyFrame() {
            if (!parsed || !overlayEl || !panelEl || !linesEl || !stageEl) return;
            var img = getImageUrl ? getImageUrl() : '';
            if (bg) {
                if (img) {
                    bg.src = img;
                    bg.hidden = false;
                }
            }
            var ov = parsed.overlay || {};
            var lines = Array.isArray(ov.final_text_lines)
                ? ov.final_text_lines.map(function (x) { return String(x || '').trim(); }).filter(Boolean)
                : [];
            var style = ov.resolved_style || {};
            var typography = style.typography || {};
            var textStyle = style.text || {};
            var panel = style.panel || {};
            var anim = overlayAnimAtTimeSec(timeSec, ov.timing || {}, ov.resolved_animation || {});

            if (!anim.visible || !lines.length) {
                overlayEl.hidden = true;
                overlayEl.style.opacity = '0';
                return;
            }

            var frameW = stageEl.clientWidth || stageEl.offsetWidth || 960;
            var frameH = stageEl.clientHeight || stageEl.offsetHeight || Math.round(frameW * 9 / 16);
            var layout = window.OverlayTextLayout
                ? window.OverlayTextLayout.computeOverlayLayout({
                    frameWidth: frameW,
                    frameHeight: frameH,
                    lines: lines,
                    box: ov.box || {},
                    typography: typography,
                    panel: panel,
                    anchor: ov.anchor || 'bottom_left',
                })
                : null;

            if (!layout) return;

            overlayEl.hidden = false;
            overlayEl.style.left = layout.leftPx + 'px';
            overlayEl.style.top = layout.topPx + 'px';
            overlayEl.style.width = layout.panelWidthPx + 'px';
            overlayEl.style.height = layout.panelHeightPx + 'px';
            overlayEl.style.opacity = String(anim.opacity);
            overlayEl.style.transform = 'translate(' + anim.translateX + 'px,' + anim.translateY + 'px) scale(' + anim.scale + ')';

            panelEl.style.boxSizing = 'border-box';
            panelEl.style.width = '100%';
            panelEl.style.height = '100%';
            panelEl.style.padding = layout.panelEnabled ? layout.pad + 'px' : '0';
            panelEl.style.borderRadius = layout.panelEnabled && layout.panel.radius_px
                ? layout.panel.radius_px + 'px'
                : '0';
            panelEl.style.background = layout.panelEnabled
                ? String(layout.panel.background || 'rgba(0,0,0,0.58)')
                : 'transparent';
            panelEl.style.backdropFilter = layout.panelEnabled && layout.panel.blur_px > 0
                ? 'blur(' + layout.panel.blur_px + 'px)'
                : 'none';
            panelEl.style.overflow = 'hidden';
            panelEl.style.color = String(textStyle.color || '#fff');
            panelEl.style.fontFamily = String(typography.font_family || 'Inter, sans-serif');
            panelEl.style.fontWeight = String(asNum(typography.font_weight, 800));
            panelEl.style.textShadow = textStyle.shadow
                ? '0 2px 18px rgba(0,0,0,' + asNum(textStyle.shadow_opacity, 0.45) + ')'
                : 'none';

            linesEl.textContent = '';
            layout.lines.forEach(function (line) {
                var row = document.createElement('div');
                row.className = 'overlay-text-remotion__line';
                row.textContent = line;
                row.style.display = 'block';
                row.style.whiteSpace = 'nowrap';
                row.style.overflow = 'hidden';
                row.style.fontSize = layout.effectiveFontSizePx + 'px';
                row.style.lineHeight = String(layout.lineHeight);
                row.style.letterSpacing = (typography.letter_spacing_px != null ? typography.letter_spacing_px : 0) + 'px';
                row.style.textTransform = String(typography.text_transform || 'none');
                row.style.textAlign = String(typography.text_align || 'left');
                linesEl.appendChild(row);
            });
        }

        function tick(now) {
            if (!playing) return;
            if (!lastTick) lastTick = now;
            var dt = (now - lastTick) / 1000;
            lastTick = now;
            timeSec += dt;
            if (timeSec >= durationSec) {
                timeSec = durationSec;
                playing = false;
                if (playBtn) playBtn.textContent = '▶';
                setStatus('Готово', 'ok');
            }
            syncTimeUi();
            applyFrame();
            if (playing) rafId = requestAnimationFrame(tick);
        }

        function remotionPayload() {
            if (typeof getRemotionPayload === 'function') {
                return getRemotionPayload() || {};
            }
            return {
                result: getResultText ? getResultText() : '',
                image_url: getImageUrl ? getImageUrl() : '',
            };
        }

        function refreshRemotionProps() {
            if (!propsTa) return;
            fetch('/overlay-text/api/remotion-props/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(remotionPayload()),
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.ok || !data.props) {
                    propsTa.value = '';
                    propsTa.placeholder = (data && data.error) || 'Не удалось собрать props.json';
                    return;
                }
                propsTa.placeholder = '';
                propsTa.value = JSON.stringify(data.props, null, 2);
            }).catch(function (e) {
                propsTa.value = '';
                propsTa.placeholder = String(e.message || e);
            });
        }

        function loadFromInputs() {
            var img = getImageUrl ? getImageUrl() : '';
            var raw = getResultText ? getResultText() : '';
            if (!String(raw || '').trim()) {
                parsed = null;
                setStatus('Заполните Result и загрузите фото.', 'err');
                if (overlayEl) overlayEl.hidden = true;
                syncTimeUi();
                return false;
            }
            if (!String(img || '').trim()) {
                parsed = null;
                setStatus('Загрузите фото.', 'err');
                if (overlayEl) overlayEl.hidden = true;
                syncTimeUi();
                return false;
            }
            if (bg) {
                if (img) {
                    bg.src = img;
                    bg.hidden = false;
                } else {
                    bg.removeAttribute('src');
                    bg.hidden = true;
                }
            }
            var res = parseOverlayResult(raw);
            if (res.error) {
                parsed = null;
                setStatus(res.error, 'err');
                if (overlayEl) overlayEl.hidden = true;
                if (propsTa) {
                    propsTa.value = '';
                    propsTa.placeholder = res.error;
                }
                return false;
            }
            parsed = res.data;
            fps = asNum(parsed.fps, 30);
            durationSec = Math.max(0.5, asNum(parsed.scene_duration_sec, 5));
            timeSec = 0;
            playing = false;
            if (playBtn) playBtn.textContent = '▶';
            syncTimeUi();
            applyFrame();
            setStatus('Готово · ' + formatTime(durationSec) + ' · ' + fps + ' fps', 'ok');
            refreshRemotionProps();
            return true;
        }

        function playPause() {
            if (!parsed) return;
            playing = !playing;
            if (playBtn) playBtn.textContent = playing ? '⏸' : '▶';
            if (playing) {
                lastTick = 0;
                cancelAnimationFrame(rafId);
                rafId = requestAnimationFrame(tick);
                setStatus('Воспроизведение…', 'run');
            } else {
                cancelAnimationFrame(rafId);
                setStatus('Пауза · ' + formatTime(timeSec), 'ok');
            }
        }

        function restart() {
            if (!parsed) return;
            timeSec = 0;
            syncTimeUi();
            applyFrame();
            if (!playing) setStatus('Готово', 'ok');
        }

        playBtn?.addEventListener('click', playPause);
        restartBtn?.addEventListener('click', restart);
        scrub?.addEventListener('input', function () {
            if (!parsed) return;
            playing = false;
            if (playBtn) playBtn.textContent = '▶';
            timeSec = asNum(scrub.value, 0);
            syncTimeUi();
            applyFrame();
            cancelAnimationFrame(rafId);
        });
        studioBtn?.addEventListener('click', function () {
            fetch('/overlay-text/api/remotion-props', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(remotionPayload()),
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.ok) throw new Error(data.error || 'props_failed');
                if (propsTa && data.props) {
                    propsTa.value = JSON.stringify(data.props, null, 2);
                }
                window.open('/overlay-text/remotion/studio', '_blank', 'noopener');
            }).catch(function (e) {
                alert(String(e.message || e));
            });
        });

        propsCopyBtn?.addEventListener('click', function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (!propsTa || !(propsTa.value || '').trim()) return;
            navigator.clipboard.writeText(propsTa.value).catch(function () { /* ignore */ });
        });

        return {
            refresh: loadFromInputs,
            stop: function () {
                playing = false;
                cancelAnimationFrame(rafId);
            },
        };
    }

    function bootFromDom() {
        var remotionRoot = document.querySelector('[data-overlay-text-remotion]');
        if (!remotionRoot) return null;
        var resultTa = document.querySelector('[data-ot-result]');
        var photoPreview = document.querySelector('[data-ot-photo-preview]');
        var durationInput = document.querySelector('[data-ot-duration]');
        return createPreview(
            remotionRoot,
            function () {
                if (!photoPreview) return '';
                return photoPreview.getAttribute('src') || photoPreview.src || '';
            },
            function () {
                return resultTa ? resultTa.value : '';
            },
            function () {
                return {
                    result: resultTa ? resultTa.value : '',
                    image_url: photoPreview ? (photoPreview.getAttribute('src') || photoPreview.src || '') : '',
                    image_preview_url: photoPreview ? (photoPreview.getAttribute('src') || photoPreview.src || '') : '',
                    duration_sec: durationInput ? durationInput.value : '',
                };
            }
        );
    }

    var defaultCtrl = null;

    function scheduleRefresh() {
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                if (defaultCtrl && typeof defaultCtrl.refresh === 'function') {
                    defaultCtrl.refresh();
                }
            });
        });
    }

    function initPreview() {
        if (!defaultCtrl) {
            defaultCtrl = bootFromDom();
        }
        scheduleRefresh();
    }

    function refreshOverlayTextPreview() {
        if (!defaultCtrl) {
            initPreview();
            return;
        }
        scheduleRefresh();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPreview);
    } else {
        initPreview();
    }
    window.addEventListener('load', scheduleRefresh);
    window.addEventListener('resize', scheduleRefresh);

    global.OverlayTextPreview = {
        createPreview: createPreview,
        refresh: refreshOverlayTextPreview,
        remount: function () {
            defaultCtrl = bootFromDom();
            scheduleRefresh();
        },
    };
})(window);
