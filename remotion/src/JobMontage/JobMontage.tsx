import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  Sequence,
  Video,
  staticFile,
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

const audioSchema = z
  .object({
    src: z.string().nullable().optional(),
    duration_ms: z.number().optional(),
  })
  .nullable()
  .optional();

const montageSchema = z
  .object({
    zoom_pct: z.number().optional(),
    fade_in_pct: z.number().optional(),
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
  audio: audioSchema,
  montage: montageSchema,
  scenes: z.array(sceneSchema),
});

export type JobMontageProps = z.infer<typeof jobMontagePropsSchema>;

const msToFrames = (ms: number, fps: number) => Math.max(1, Math.round((ms / 1000) * fps));

export const JobMontage: React.FC<JobMontageProps> = ({ scenes, audio }) => {
  const { fps } = useVideoConfig();
  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      {scenes.map((scene, idx) => {
        const fromFrame = msToFrames(scene.start_ms, fps);
        const lengthFrames = Math.max(
          1,
          msToFrames(Math.max(0, scene.end_ms - scene.start_ms) || 33, fps),
        );
        return (
          <Sequence
            key={`${scene.scene_id || idx}-${idx}`}
            from={fromFrame}
            durationInFrames={lengthFrames}
            name={scene.scene_id || `scene_${idx + 1}`}
          >
            <SceneRender scene={scene} />
          </Sequence>
        );
      })}
      {audio?.src ? <Audio src={staticFile(audio.src)} /> : null}
    </AbsoluteFill>
  );
};

const SceneRender: React.FC<{ scene: JobMontageProps["scenes"][number] }> = ({ scene }) => {
  const media = scene.media;
  if (media && media.src) {
    const url = staticFile(media.src);
    if (media.kind === "video") {
      return (
        <AbsoluteFill style={{ backgroundColor: "#000" }}>
          <Video
            src={url}
            muted
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        </AbsoluteFill>
      );
    }
    return (
      <AbsoluteFill style={{ backgroundColor: "#000" }}>
        <Img
          src={url}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      </AbsoluteFill>
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
      }}
    >
      <div>{scene.text_ru || scene.text || scene.scene_id}</div>
    </AbsoluteFill>
  );
};
