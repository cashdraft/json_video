/** Layout overlay: модельные line breaks, без auto-wrap; panel = текст + padding. */

import type { CSSProperties } from "react";

export type OverlayBox = {
  x_pct?: number;
  y_pct?: number;
  w_pct?: number;
  h_pct?: number;
};

export type OverlayTypography = {
  font_family?: string;
  font_weight?: number;
  font_size_px?: number;
  line_height?: number;
  letter_spacing_px?: number;
  text_transform?: string;
  text_align?: string;
};

export type OverlayPanel = {
  enabled?: boolean;
  background?: string;
  blur_px?: number;
  radius_px?: number;
  padding_px?: number;
};

export type ComputedOverlayLayout = {
  containerStyle: CSSProperties;
  panelStyle: CSSProperties;
  lineStyle: CSSProperties;
  lines: string[];
  contentWidthPx: number;
  contentHeightPx: number;
  panelWidthPx: number;
  panelHeightPx: number;
  effectiveFontSizePx: number;
};

function clamp(n: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, n));
}

function cssFont(typography: OverlayTypography, fontSizePx: number): string {
  const weight = typography.font_weight ?? 800;
  const family = typography.font_family || "Inter, sans-serif";
  const letterSpacing = typography.letter_spacing_px ?? 0;
  return `${weight} ${fontSizePx}px ${family}`.trim() + (letterSpacing ? "" : "");
}

/** Canvas measureText; fallback для кириллицы ~0.58em ширины glyph. */
export function measureLineWidthPx(
  text: string,
  typography: OverlayTypography,
  fontSizePx: number,
): number {
  const sample = String(text || "");
  if (!sample) return 0;
  const font = cssFont(typography, fontSizePx);
  if (typeof document !== "undefined") {
    try {
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      if (ctx) {
        ctx.font = font;
        const spacing = typography.letter_spacing_px ?? 0;
        const m = ctx.measureText(sample);
        const extra =
          spacing !== 0 && sample.length > 1
            ? spacing * (sample.length - 1)
            : 0;
        return m.width + extra;
      }
    } catch {
      /* fallback below */
    }
  }
  return sample.length * fontSizePx * 0.58;
}

function normalizePanel(panel: OverlayPanel): OverlayPanel {
  const blur = panel.blur_px ?? 0;
  return {
    ...panel,
    blur_px: blur > 0 ? Math.min(blur, 12) : 0,
    background: softenPanelBackground(panel.background),
  };
}

/** Слишком тёмный gradient → чуть легче (renderer-side). */
function softenPanelBackground(bg: string | undefined): string {
  const s = String(bg || "");
  if (!s.includes("0.82")) return s || "rgba(0,0,0,0.58)";
  return s.replace(/0\.82/g, "0.58");
}

export function computeOverlayLayout(opts: {
  frameWidth: number;
  frameHeight: number;
  lines: string[];
  box: OverlayBox;
  typography: OverlayTypography;
  panel: OverlayPanel;
  anchor?: string;
}): ComputedOverlayLayout {
  const {
    frameWidth,
    frameHeight,
    lines: rawLines,
    box,
    typography,
    panel: rawPanel,
    anchor = "bottom_left",
  } = opts;

  const lines = rawLines.map((l) => String(l ?? "").trim()).filter(Boolean);
  const panel = normalizePanel(rawPanel);
  const panelEnabled = panel.enabled !== false;

  const xPct = clamp(Number(box.x_pct ?? 6), 0, 100);
  const yPct = clamp(Number(box.y_pct ?? 70), 0, 100);
  const wPct = clamp(Number(box.w_pct ?? 40), 1, 100);

  const maxContainerWidthPx = (frameWidth * wPct) / 100;
  const pad = panelEnabled ? clamp(Number(panel.padding_px ?? 24), 0, 120) : 0;

  let fontSize = clamp(Number(typography.font_size_px ?? 52), 12, 220);
  const lineHeight = clamp(Number(typography.line_height ?? 1.04), 0.8, 2);
  const textAlign = String(typography.text_align || "left");

  const measureAll = (fs: number) =>
    lines.map((line) => measureLineWidthPx(line, typography, fs));

  let lineWidths = measureAll(fontSize);
  let maxLineWidth = lineWidths.length ? Math.max(...lineWidths) : 0;
  const innerMax = Math.max(1, maxContainerWidthPx - pad * 2);

  if (maxLineWidth > innerMax && maxLineWidth > 0) {
    const scale = innerMax / maxLineWidth;
    fontSize = Math.max(12, Math.floor(fontSize * scale * 100) / 100);
    lineWidths = measureAll(fontSize);
    maxLineWidth = lineWidths.length ? Math.max(...lineWidths) : 0;
  }

  const contentWidthPx = maxLineWidth;
  const lineBlockPx = fontSize * lineHeight;
  const contentHeightPx = lines.length * lineBlockPx;
  const panelWidthPx = Math.min(maxContainerWidthPx, contentWidthPx + pad * 2);
  const panelHeightPx = contentHeightPx + pad * 2;

  const leftPx = (frameWidth * xPct) / 100;
  let topPx = (frameHeight * yPct) / 100;

  const anchorNorm = String(anchor || "bottom_left").toLowerCase();
  const hPctAdvisory = Number(box.h_pct);
  if (Number.isFinite(hPctAdvisory) && hPctAdvisory > 0) {
    const advisoryBottomPx = (frameHeight * (yPct + hPctAdvisory)) / 100;
    if (anchorNorm.includes("bottom")) {
      topPx = advisoryBottomPx - panelHeightPx;
    }
  } else if (anchorNorm.includes("bottom")) {
    topPx = topPx - panelHeightPx;
  }

  topPx = clamp(topPx, 0, Math.max(0, frameHeight - panelHeightPx));
  const clampedLeft = clamp(leftPx, 0, Math.max(0, frameWidth - panelWidthPx));

  const textTransform = String(typography.text_transform || "none");
  const letterSpacing = typography.letter_spacing_px ?? 0;

  return {
    lines,
    contentWidthPx,
    contentHeightPx,
    panelWidthPx,
    panelHeightPx,
    effectiveFontSizePx: fontSize,
    containerStyle: {
      position: "absolute",
      left: clampedLeft,
      top: topPx,
      width: panelWidthPx,
      height: panelHeightPx,
      boxSizing: "border-box",
      pointerEvents: "none",
      display: "flex",
      flexDirection: "column",
      justifyContent: "flex-end",
      alignItems:
        textAlign === "center"
          ? "center"
          : textAlign === "right"
            ? "flex-end"
            : "flex-start",
    },
    panelStyle: {
      boxSizing: "border-box",
      width: "100%",
      padding: panelEnabled ? pad : 0,
      borderRadius: panelEnabled ? (panel.radius_px ?? 0) : 0,
      background: panelEnabled ? (panel.background || "rgba(0,0,0,0.58)") : "transparent",
      backdropFilter:
        panelEnabled && (panel.blur_px ?? 0) > 0
          ? `blur(${panel.blur_px}px)`
          : undefined,
      WebkitBackdropFilter:
        panelEnabled && (panel.blur_px ?? 0) > 0
          ? `blur(${panel.blur_px}px)`
          : undefined,
      overflow: "hidden",
    },
    lineStyle: {
      display: "block",
      whiteSpace: "nowrap",
      fontSize,
      lineHeight,
      letterSpacing,
      textTransform: textTransform as CSSProperties["textTransform"],
      textAlign: textAlign as CSSProperties["textAlign"],
      width: "100%",
      overflow: "hidden",
      textOverflow: "clip",
    },
  };
}
