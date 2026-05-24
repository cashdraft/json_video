import React, { useMemo } from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import { z } from "zod";
import { defaultOverlayCaptionProps, overlayCaptionPropsSchema } from "./defaultProps";
import { overlayAnimAtFrame } from "./overlayAnim";
import { computeOverlayLayout } from "./overlayLayout";

export { overlayCaptionPropsSchema };
export type { OverlayCaptionProps } from "./defaultProps";

type OverlayData = Record<string, unknown>;

function asRecord(v: unknown): OverlayData {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as OverlayData) : {};
}

function asNumber(v: unknown, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function asStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => String(x ?? "").trim()).filter(Boolean);
}

export const OverlayCaption: React.FC<z.infer<typeof overlayCaptionPropsSchema>> = ({
  fps,
  width,
  height,
  image_src,
  overlay,
}) => {
  const frame = useCurrentFrame();
  const ov = asRecord(overlay);
  const lines = asStringArray(ov.final_text_lines);
  const box = asRecord(ov.box);
  const style = asRecord(ov.resolved_style);
  const typography = asRecord(style.typography);
  const textStyle = asRecord(style.text);
  const panel = asRecord(style.panel);
  const timing = asRecord(ov.timing);
  const resolvedAnimation = asRecord(ov.resolved_animation);
  const anchor = String(ov.anchor || "bottom_left");

  const layout = useMemo(
    () =>
      computeOverlayLayout({
        frameWidth: width,
        frameHeight: height,
        lines,
        box: {
          x_pct: asNumber(box.x_pct, 6),
          y_pct: asNumber(box.y_pct, 70),
          w_pct: asNumber(box.w_pct, 40),
          h_pct: asNumber(box.h_pct, 0),
        },
        typography: {
          font_family: String(typography.font_family || "Inter, sans-serif"),
          font_weight: asNumber(typography.font_weight, 800),
          font_size_px: asNumber(typography.font_size_px, 52),
          line_height: asNumber(typography.line_height, 1.04),
          letter_spacing_px: asNumber(typography.letter_spacing_px, 0),
          text_transform: String(typography.text_transform || "none"),
          text_align: String(typography.text_align || "left"),
        },
        panel: {
          enabled: panel.enabled !== false,
          background: String(panel.background || ""),
          blur_px: asNumber(panel.blur_px, 0),
          radius_px: asNumber(panel.radius_px, 0),
          padding_px: asNumber(panel.padding_px, 24),
        },
        anchor,
      }),
    [width, height, lines, box, typography, panel, anchor],
  );

  const anim = overlayAnimAtFrame(
    frame,
    fps,
    {
      start_sec: asNumber(timing.start_sec, 0),
      end_sec: asNumber(timing.end_sec, 0),
      in_duration_sec: asNumber(timing.in_duration_sec, 0.45),
      out_duration_sec: asNumber(timing.out_duration_sec, 0.3),
    },
    {
      in: asRecord(resolvedAnimation.in) as { animation?: string; duration_sec?: number },
      out: asRecord(resolvedAnimation.out) as { animation?: string; duration_sec?: number },
    },
  );

  const color = String(textStyle.color || "#FFFFFF");
  const shadow = textStyle.shadow === true;
  const shadowOpacity = asNumber(textStyle.shadow_opacity, 0.45);
  const fontFamily = String(typography.font_family || "Inter, sans-serif");
  const fontWeight = asNumber(typography.font_weight, 800);
  const textShadow = shadow
    ? `0 2px 18px rgba(0,0,0,${shadowOpacity})`
    : undefined;

  return (
    <AbsoluteFill style={{ width, height, backgroundColor: "#000" }}>
      <Img
        src={staticFile(image_src)}
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
      />
      {anim.visible && layout.lines.length > 0 ? (
        <div
          style={{
            ...layout.containerStyle,
            opacity: anim.opacity,
            transform: `translate(${anim.translateX}px, ${anim.translateY}px) scale(${anim.scale})`,
            transformOrigin: "left bottom",
          }}
        >
          <div
            style={{
              ...layout.panelStyle,
              color,
              fontFamily,
              fontWeight,
              textShadow,
            }}
          >
            {layout.lines.map((line, i) => (
              <div
                key={`${i}-${line}`}
                style={{
                  ...layout.lineStyle,
                  fontSize: layout.effectiveFontSizePx,
                }}
              >
                {line}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

export const defaultProps = defaultOverlayCaptionProps;
