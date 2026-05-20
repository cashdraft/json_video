import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  Sequence,
  Video,
  interpolate,
  prefetch,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { z } from "zod";

const sceneMediaSchema = z
  .object({
    kind: z.enum(["image", "video"]).nullable().optional(),
    source: z.string().nullable().optional(),
    src: z.string(),
    local_path: z.string().optional(),
  })
  .nullable();

const sceneSchema = z.object({
  scene_id: z.string(),
  text: z.string().optional(),
  text_ru: z.string().optional(),
  start_ms: z.number(),
  end_ms: z.number(),
  duration_ms: z.number().optional(),
  media: sceneMediaSchema.optional(),
  low_confidence: z.boolean().optional(),
});

const montageSchema = z
  .object({
    zoom_scale: z.number().optional(),
    zoom_pct: z.number().optional(),
    zoom_mode: z.enum(["alternate", "all_in", "all_out", "random"]).optional(),
    zoom_smooth: z.boolean().optional(),
    zoom_ref_seconds: z.number().optional(),
    fade_in_pct: z.number().optional(),
    prefer_video: z.boolean().optional(),
  })
  .optional();

export const jobMontagePropsSchema = z.object({
  schema: z.string().optional(),
  job_id: z.string().optional(),
  project_name: z.string().optional(),
  fps: z.number(),
  width: z.number(),
  height: z.number(),
  aspect_ratio: z.string().optional(),
  total_duration_ms: z.number(),
  static_prefix: z.string().optional(),
  audio: z
    .object({
      src: z.string().nullable().optional(),
      duration_ms: z.number().optional(),
    })
    .nullable()
    .optional(),
  montage: montageSchema,
  scenes: z.array(sceneSchema),
});

export type JobMontageProps = z.infer<typeof jobMontagePropsSchema>;

const msToFrames = (ms: number, fps: number) => Math.max(1, Math.round((ms / 1000) * fps));

function montagePeakScale(montage: JobMontageProps["montage"]): number {
  const m = montage;
  if (!m) return 1;
  const zs = m.zoom_scale;
  if (zs != null && Number.isFinite(zs)) {
    return Math.min(1.5, Math.max(1, zs));
  }
  const zp = m.zoom_pct ?? 0;
  const pct = Math.max(0, Math.min(100, Math.round(zp)));
  return 1 + (pct / 100) * 0.5;
}

type ZoomDirection = "in" | "out";
type ZoomMode = "alternate" | "all_in" | "all_out" | "random";

function hashStringToInt(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

// детерминированный PRNG, чтобы random-режим давал одинаковый рендер при одинаковом seed
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pickZoomDirection(
  mode: ZoomMode,
  idx: number,
  randomSeedKey: string,
): ZoomDirection {
  if (mode === "all_in") return "in";
  if (mode === "all_out") return "out";
  if (mode === "alternate") return idx % 2 === 0 ? "in" : "out";
  const rng = mulberry32(hashStringToInt(`${randomSeedKey}#${idx}`));
  return rng() < 0.5 ? "in" : "out";
}

const KenBurnsWrap: React.FC<{
  peakScale: number;
  durationInFrames: number;
  direction: ZoomDirection;
  children: React.ReactNode;
}> = ({ peakScale, durationInFrames, direction, children }) => {
  const frame = useCurrentFrame();
  const df = Math.max(1, durationInFrames);
  if (peakScale <= 1.0001 || df < 2) {
    return <>{children}</>;
  }
  const last = df - 1;
  const start = direction === "in" ? 1 : peakScale;
  const end = direction === "in" ? peakScale : 1;
  const scale = interpolate(
    frame,
    [0, last],
    [start, end],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );
  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      <div
        style={{
          width: "100%",
          height: "100%",
          transform: `scale(${scale})`,
          transformOrigin: "center center",
        }}
      >
        {children}
      </div>
    </AbsoluteFill>
  );
};

// Параллельно тянет все картинки в blob: URL ещё до того, как scrubber попадёт в сцену.
// На предпросмотре в Studio это убирает «провалы» 100–500 мс на каждой смене сцены.
function useScenesImagePrefetch(
  scenes: JobMontageProps["scenes"],
): Record<string, string> {
  const [resolved, setResolved] = React.useState<Record<string, string>>({});
  const sources = React.useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const s of scenes) {
      const m = s.media;
      if (m && m.kind === "image" && m.src && !seen.has(m.src)) {
        seen.add(m.src);
        out.push(m.src);
      }
    }
    return out;
  }, [scenes]);

  React.useEffect(() => {
    let cancelled = false;
    const handles: Array<{ free: () => void }> = [];
    (async () => {
      const map: Record<string, string> = {};
      await Promise.all(
        sources.map(async (src) => {
          try {
            const h = prefetch(staticFile(src), { method: "blob-url" });
            handles.push(h);
            const blobUrl = await h.waitUntilDone();
            if (typeof blobUrl === "string" && blobUrl) {
              map[src] = blobUrl;
            }
          } catch {
            // если prefetch упал — оставляем оригинальный URL (Img сам подтянет)
          }
        }),
      );
      if (!cancelled) setResolved(map);
    })();
    return () => {
      cancelled = true;
      for (const h of handles) {
        try {
          h.free();
        } catch {
          /* ignore */
        }
      }
    };
  }, [sources]);

  return resolved;
}

/** Предзагрузка озвучки в blob: URL — быстрее появляется дорожка в Studio. */
function useAudioPrefetch(src: string | null | undefined): string | undefined {
  const [resolved, setResolved] = React.useState<string | undefined>();
  React.useEffect(() => {
    if (!src) return;
    let cancelled = false;
    let handle: { free: () => void } | null = null;
    (async () => {
      try {
        const h = prefetch(staticFile(src), { method: "blob-url" });
        handle = h;
        const blobUrl = await h.waitUntilDone();
        if (!cancelled && typeof blobUrl === "string" && blobUrl) {
          setResolved(blobUrl);
        }
      } catch {
        /* fallback: прямой staticFile */
      }
    })();
    return () => {
      cancelled = true;
      try {
        handle?.free();
      } catch {
        /* ignore */
      }
    };
  }, [src]);
  return resolved;
}

// Если включён «плавный зум» — для коротких сцен уменьшаем пик так,
// чтобы скорость зума совпадала с «эталоном»: peak_ref достигается за zoom_ref_seconds.
// Для сцен ≥ zoom_ref_seconds peak остаётся полным (peak_ref).
function effectivePeakForScene(
  peakRef: number,
  durationSec: number,
  smoothEnabled: boolean,
  refSeconds: number | undefined,
): number {
  if (!smoothEnabled || peakRef <= 1.0001) return peakRef;
  const ref = Math.max(0.1, Number(refSeconds) || 5);
  if (durationSec >= ref) return peakRef;
  const k = Math.max(0, Math.min(1, durationSec / ref));
  return 1 + (peakRef - 1) * k;
}

/** Непрозрачность в начале сцены: 0→1 за первые fade_in_pct% длительности (остальное — 1). Линейно. */
function sceneFadeInOpacity(
  frame: number,
  durationInFrames: number,
  fadeInPct: number,
): number {
  if (fadeInPct <= 0 || durationInFrames < 1) return 1;
  const pct = Math.max(0, Math.min(100, Math.round(fadeInPct)));
  const fadeFrames = Math.min(
    durationInFrames,
    Math.max(1, Math.round((pct / 100) * durationInFrames)),
  );
  if (fadeFrames <= 1) return 1;
  return Math.min(1, frame / (fadeFrames - 1));
}

export const JobMontage: React.FC<JobMontageProps> = ({
  scenes,
  audio,
  montage,
  job_id,
}) => {
  const { fps } = useVideoConfig();
  const peakRef = montagePeakScale(montage);
  const mode: ZoomMode = (montage?.zoom_mode ?? "alternate") as ZoomMode;
  const smoothEnabled = Boolean(montage?.zoom_smooth);
  const refSeconds = Number.isFinite(montage?.zoom_ref_seconds)
    ? (montage?.zoom_ref_seconds as number)
    : 5;
  const fadeInPct = Math.max(
    0,
    Math.min(100, Math.round(Number(montage?.fade_in_pct ?? 0))),
  );
  const seedKey = String(job_id || "job") + "::" + String(scenes.length);
  const prefetchedByRel = useScenesImagePrefetch(scenes);
  const prefetchedAudioSrc = useAudioPrefetch(audio?.src ?? null);
  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      {scenes.map((scene, idx) => {
        const fromFrame = msToFrames(scene.start_ms, fps);
        const lengthFrames = Math.max(
          1,
          msToFrames(Math.max(0, scene.end_ms - scene.start_ms) || 33, fps),
        );
        const sceneDurSec = Math.max(0.001, lengthFrames / Math.max(1, fps));
        const scenePeak = effectivePeakForScene(
          peakRef,
          sceneDurSec,
          smoothEnabled,
          refSeconds,
        );
        const direction = pickZoomDirection(mode, idx, seedKey);
        const m = scene.media;
        const resolvedImgSrc =
          m && m.kind === "image" && m.src ? prefetchedByRel[m.src] : undefined;
        return (
          <Sequence
            key={`${scene.scene_id || idx}-${idx}`}
            from={fromFrame}
            durationInFrames={lengthFrames}
            name={scene.scene_id || `scene_${idx + 1}`}
          >
            <SceneRender
              scene={scene}
              peakScale={scenePeak}
              durationInFrames={lengthFrames}
              direction={direction}
              resolvedImgSrc={resolvedImgSrc}
              fadeInPct={fadeInPct}
            />
          </Sequence>
        );
      })}
      {audio?.src ? (
        <Audio src={prefetchedAudioSrc || staticFile(audio.src)} />
      ) : null}
    </AbsoluteFill>
  );
};

const SceneRender: React.FC<{
  scene: JobMontageProps["scenes"][number];
  peakScale: number;
  durationInFrames: number;
  direction: ZoomDirection;
  resolvedImgSrc?: string;
  fadeInPct: number;
}> = ({
  scene,
  peakScale,
  durationInFrames,
  direction,
  resolvedImgSrc,
  fadeInPct,
}) => {
  const frame = useCurrentFrame();
  const fadeOpacity = sceneFadeInOpacity(frame, durationInFrames, fadeInPct);
  const media = scene.media;
  if (media && media.src) {
    const url = staticFile(media.src);
    if (media.kind === "video") {
      return (
        <KenBurnsWrap
          peakScale={peakScale}
          durationInFrames={durationInFrames}
          direction={direction}
        >
          <AbsoluteFill style={{ backgroundColor: "#000", opacity: fadeOpacity }}>
            <Video
              src={url}
              muted
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          </AbsoluteFill>
        </KenBurnsWrap>
      );
    }
    return (
      <KenBurnsWrap
        peakScale={peakScale}
        durationInFrames={durationInFrames}
        direction={direction}
      >
        <AbsoluteFill style={{ backgroundColor: "#000", opacity: fadeOpacity }}>
          <Img
            src={resolvedImgSrc || url}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        </AbsoluteFill>
      </KenBurnsWrap>
    );
  }
  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#111",
        alignItems: "center",
        justifyContent: "center",
        padding: 64,
        color: "#bbb",
        fontFamily: "system-ui, sans-serif",
        fontSize: 36,
        textAlign: "center",
        opacity: fadeOpacity,
      }}
    >
      <div>{scene.text_ru || scene.text || scene.scene_id}</div>
    </AbsoluteFill>
  );
};
