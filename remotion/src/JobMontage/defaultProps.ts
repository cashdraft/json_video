import type { JobMontageProps } from "./JobMontage";

export const defaultJobMontageProps: JobMontageProps = {
  schema: "job_montage_props@1",
  job_id: "",
  project_name: "",
  fps: 30,
  width: 1920,
  height: 1080,
  aspect_ratio: "16:9",
  total_duration_ms: 5000,
  static_prefix: "",
  audio: { src: null, duration_ms: 0 },
  montage: {
    zoom_scale: 1,
    zoom_mode: "alternate",
    zoom_smooth: false,
    zoom_ref_seconds: 5,
    fade_in_pct: 0,
    prefer_video: false,
  },
  scenes: [
    {
      scene_id: "scene_001",
      text: "Placeholder scene",
      text_ru: "Запустите подготовку из приложения — здесь подставятся сцены.",
      start_ms: 0,
      end_ms: 5000,
      duration_ms: 5000,
      media: null,
      low_confidence: false,
    },
  ],
};
