import React, { useLayoutEffect, useRef } from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { z } from "zod";
import {
  AnimTrack,
  applyTracksToSvg,
} from "./applyAnims";

const trackSchema = z.object({
  id: z.string(),
  anim: z.string(),
  start: z.number().optional(),
  end: z.number().optional(),
});

const animationSchema = z.object({
  fps: z.number().optional(),
  duration_sec: z.number().optional(),
  tracks: z.array(trackSchema),
});

export const laterInfographicPropsSchema = z.object({
  schema: z.string().optional(),
  fps: z.number(),
  width: z.number(),
  height: z.number(),
  duration_sec: z.number().optional(),
  duration_frames: z.number().optional(),
  svg: z.string(),
  animation: animationSchema,
});

export type LaterInfographicProps = z.infer<typeof laterInfographicPropsSchema>;

export const LaterInfographic: React.FC<LaterInfographicProps> = ({
  svg,
  animation,
  width,
  height,
}) => {
  const frame = useCurrentFrame();
  const hostRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const svgEl = host.querySelector("svg");
    if (!(svgEl instanceof SVGSVGElement)) return;
    const tracks = (animation?.tracks ?? []) as AnimTrack[];
    applyTracksToSvg(svgEl, tracks, frame);
  }, [frame, svg, animation]);

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#05060d",
        width,
        height,
      }}
    >
      <div
        ref={hostRef}
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    </AbsoluteFill>
  );
};
