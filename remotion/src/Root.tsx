import { Composition, staticFile } from "remotion";
import { HelloWorld, myCompSchema } from "./HelloWorld";
import { Logo, myCompSchema2 } from "./HelloWorld/Logo";
import { JobMontage, jobMontagePropsSchema } from "./JobMontage/JobMontage";
import { defaultJobMontageProps } from "./JobMontage/defaultProps";

const FPS = defaultJobMontageProps.fps;
const defaultDurationFrames = Math.max(
  1,
  Math.round((defaultJobMontageProps.total_duration_ms / 1000) * FPS),
);

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="JobMontage"
        component={JobMontage}
        durationInFrames={defaultDurationFrames}
        fps={FPS}
        width={defaultJobMontageProps.width}
        height={defaultJobMontageProps.height}
        schema={jobMontagePropsSchema}
        defaultProps={defaultJobMontageProps}
        calculateMetadata={async ({ props, defaultProps, isRendering }) => {
          try {
            const merged = { ...defaultProps, ...props };
            let resolved = merged;

            const isAbortError = (e: unknown) =>
              e instanceof Error && e.name === "AbortError";

            if (!isRendering && typeof window !== "undefined") {
              const jobId = new URLSearchParams(window.location.search).get("job");
              if (jobId) {
                try {
                  const url = staticFile(`jobs/${jobId}/props.json`);
                  const res = await fetch(url);
                  if (res.ok) {
                    const json: unknown = await res.json();
                    const parsed = jobMontagePropsSchema.safeParse(json);
                    if (parsed.success) {
                      resolved = parsed.data;
                    } else {
                      console.warn("JobMontage props.json validation failed", parsed.error.flatten());
                    }
                  } else {
                    console.warn("JobMontage props.json HTTP", res.status, url);
                  }
                } catch (err) {
                  if (isAbortError(err)) {
                    resolved = merged;
                  } else {
                    console.warn("JobMontage props load error", err);
                  }
                }
              }
            }

            const fps = Math.max(1, resolved.fps || FPS);
            const totalMs = Math.max(1, resolved.total_duration_ms || 1000);
            return {
              durationInFrames: Math.max(1, Math.round((totalMs / 1000) * fps)),
              fps,
              width: resolved.width || 1920,
              height: resolved.height || 1080,
              props: resolved,
            };
          } catch (e) {
            console.warn("JobMontage calculateMetadata failed", e);
            const merged = { ...defaultProps, ...props };
            const fps = Math.max(1, merged.fps || FPS);
            const totalMs = Math.max(1, merged.total_duration_ms || 1000);
            return {
              durationInFrames: Math.max(1, Math.round((totalMs / 1000) * fps)),
              fps,
              width: merged.width || 1920,
              height: merged.height || 1080,
              props: merged,
            };
          }
        }}
      />

      <Composition
        id="HelloWorld"
        component={HelloWorld}
        durationInFrames={150}
        fps={30}
        width={1920}
        height={1080}
        schema={myCompSchema}
        defaultProps={{
          titleText: "Welcome to Remotion",
          titleColor: "#000000",
          logoColor1: "#91EAE4",
          logoColor2: "#86A8E7",
        }}
      />

      <Composition
        id="OnlyLogo"
        component={Logo}
        durationInFrames={150}
        fps={30}
        width={1920}
        height={1080}
        schema={myCompSchema2}
        defaultProps={{
          logoColor1: "#91dAE2" as const,
          logoColor2: "#86A8E7" as const,
        }}
      />
    </>
  );
};
