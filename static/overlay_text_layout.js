(function (global) {
    'use strict';

    function clamp(n, min, max) {
        return Math.min(max, Math.max(min, n));
    }

    function cssFont(typography, fontSizePx) {
        var weight = typography.font_weight != null ? typography.font_weight : 800;
        var family = typography.font_family || 'Inter, sans-serif';
        return String(weight) + ' ' + fontSizePx + 'px ' + family;
    }

    function measureLineWidthPx(text, typography, fontSizePx) {
        var sample = String(text || '');
        if (!sample) return 0;
        try {
            var canvas = document.createElement('canvas');
            var ctx = canvas.getContext('2d');
            if (ctx) {
                ctx.font = cssFont(typography, fontSizePx);
                var spacing = typography.letter_spacing_px != null ? typography.letter_spacing_px : 0;
                var w = ctx.measureText(sample).width;
                if (spacing !== 0 && sample.length > 1) {
                    w += spacing * (sample.length - 1);
                }
                return w;
            }
        } catch (e) { /* ignore */ }
        return sample.length * fontSizePx * 0.58;
    }

    function softenPanelBackground(bg) {
        var s = String(bg || '');
        if (s.indexOf('0.82') < 0) return s || 'rgba(0,0,0,0.58)';
        return s.replace(/0\.82/g, '0.58');
    }

    function normalizePanel(panel) {
        var blur = panel.blur_px != null ? panel.blur_px : 0;
        return {
            enabled: panel.enabled,
            background: softenPanelBackground(panel.background),
            blur_px: blur > 0 ? Math.min(blur, 12) : 0,
            radius_px: panel.radius_px,
            padding_px: panel.padding_px,
        };
    }

    function computeOverlayLayout(opts) {
        var frameWidth = opts.frameWidth;
        var frameHeight = opts.frameHeight;
        var rawLines = opts.lines || [];
        var box = opts.box || {};
        var typography = opts.typography || {};
        var rawPanel = opts.panel || {};
        var anchor = String(opts.anchor || 'bottom_left').toLowerCase();

        var lines = rawLines.map(function (l) { return String(l || '').trim(); }).filter(Boolean);
        var panel = normalizePanel(rawPanel);
        var panelEnabled = panel.enabled !== false;

        var xPct = clamp(Number(box.x_pct != null ? box.x_pct : 6), 0, 100);
        var yPct = clamp(Number(box.y_pct != null ? box.y_pct : 70), 0, 100);
        var wPct = clamp(Number(box.w_pct != null ? box.w_pct : 40), 1, 100);

        var maxContainerWidthPx = (frameWidth * wPct) / 100;
        var pad = panelEnabled ? clamp(Number(panel.padding_px != null ? panel.padding_px : 24), 0, 120) : 0;

        var fontSize = clamp(Number(typography.font_size_px != null ? typography.font_size_px : 52), 12, 220);
        var lineHeight = clamp(Number(typography.line_height != null ? typography.line_height : 1.04), 0.8, 2);
        var textAlign = String(typography.text_align || 'left');

        function measureAll(fs) {
            return lines.map(function (line) { return measureLineWidthPx(line, typography, fs); });
        }

        var lineWidths = measureAll(fontSize);
        var maxLineWidth = lineWidths.length ? Math.max.apply(null, lineWidths) : 0;
        var innerMax = Math.max(1, maxContainerWidthPx - pad * 2);

        if (maxLineWidth > innerMax && maxLineWidth > 0) {
            fontSize = Math.max(12, Math.floor(fontSize * (innerMax / maxLineWidth) * 100) / 100);
            lineWidths = measureAll(fontSize);
            maxLineWidth = lineWidths.length ? Math.max.apply(null, lineWidths) : 0;
        }

        var contentWidthPx = maxLineWidth;
        var lineBlockPx = fontSize * lineHeight;
        var contentHeightPx = lines.length * lineBlockPx;
        var panelWidthPx = Math.min(maxContainerWidthPx, contentWidthPx + pad * 2);
        var panelHeightPx = contentHeightPx + pad * 2;

        var leftPx = (frameWidth * xPct) / 100;
        var topPx = (frameHeight * yPct) / 100;
        var hPctAdvisory = Number(box.h_pct);
        if (Number.isFinite(hPctAdvisory) && hPctAdvisory > 0) {
            var advisoryBottomPx = (frameHeight * (yPct + hPctAdvisory)) / 100;
            if (anchor.indexOf('bottom') >= 0) {
                topPx = advisoryBottomPx - panelHeightPx;
            }
        } else if (anchor.indexOf('bottom') >= 0) {
            topPx = topPx - panelHeightPx;
        }

        topPx = clamp(topPx, 0, Math.max(0, frameHeight - panelHeightPx));
        var clampedLeft = clamp(leftPx, 0, Math.max(0, frameWidth - panelWidthPx));

        return {
            lines: lines,
            effectiveFontSizePx: fontSize,
            lineHeight: lineHeight,
            panelWidthPx: panelWidthPx,
            panelHeightPx: panelHeightPx,
            leftPx: clampedLeft,
            topPx: topPx,
            pad: pad,
            panel: panel,
            panelEnabled: panelEnabled,
            textAlign: textAlign,
            typography: typography,
        };
    }

    global.OverlayTextLayout = {
        computeOverlayLayout: computeOverlayLayout,
        measureLineWidthPx: measureLineWidthPx,
    };
})(window);
