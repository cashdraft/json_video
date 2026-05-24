import { z } from "zod";

export const overlayCaptionPropsSchema = z.object({
  schema: z.string().optional(),
  fps: z.number(),
  width: z.number(),
  height: z.number(),
  duration_sec: z.number(),
  duration_frames: z.number().optional(),
  image_src: z.string(),
  overlay: z.record(z.string(), z.unknown()),
});

export type OverlayCaptionProps = z.infer<typeof overlayCaptionPropsSchema>;

export const defaultOverlayCaptionProps: OverlayCaptionProps = {
  schema: "overlay_caption_props@1",
  fps: 30,
  width: 1920,
  height: 1080,
  duration_sec: 5,
  duration_frames: 150,
  image_src: "overlay-text/frame.jpg",
  overlay: {
    enabled: true,
    final_text_lines: ["Sample overlay"],
    anchor: "bottom_left",
    box: { x_pct: 6, y_pct: 70, w_pct: 40, h_pct: 18 },
    resolved_style: {
      typography: {
        font_family: "Inter, sans-serif",
        font_weight: 800,
        font_size_px: 52,
        line_height: 1.04,
        letter_spacing_px: -1,
        text_transform: "none",
        text_align: "left",
      },
      text: { color: "#FFFFFF", shadow: false },
      panel: {
        enabled: true,
        type: "gradient",
        background:
          "linear-gradient(90deg, rgba(0,0,0,0.82), rgba(0,0,0,0.18))",
        blur_px: 18,
        radius_px: 28,
        padding_px: 34,
      },
    },
    resolved_animation: {
      in: { animation: "fly-up", duration_sec: 0.55 },
      out: { animation: "fade-out", duration_sec: 0.3 },
    },
    timing: {
      start_sec: 0.4,
      end_sec: 4.8,
      in_duration_sec: 0.55,
      out_duration_sec: 0.3,
    },
  },
};
