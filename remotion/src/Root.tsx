import { Composition, staticFile } from "remotion";
import { HelloWorld, myCompSchema } from "./HelloWorld";
import { Logo, myCompSchema2 } from "./HelloWorld/Logo";
import { JobMontage, jobMontagePropsSchema } from "./JobMontage/JobMontage";
import { defaultJobMontageProps } from "./JobMontage/defaultProps";

declare global {
  interface Window {
    /** Если задан — откуда грузить props.json в Studio (например http://72.56.116.130:5000). */
    __JSON_VIDEO_API_ORIGIN__?: string;
  }
}

const FPS = defaultJobMontageProps.fps;
const defaultDurationFrames = Math.max(
  1,
  Math.round((defaultJobMontageProps.total_duration_ms / 1000) * FPS),
);

/** URL props.json на Flask (Studio на :3000 не отдаёт public/ как сырой JSON — только SPA). */
function montagePropsFlaskUrl(jobId: string): string | null {
  if (typeof window === "undefined") return null;
  const custom = (window.__JSON_VIDEO_API_ORIGIN__ || "").trim().replace(/\/+$/, "");
  if (custom) {
    return `${custom}/job/${encodeURIComponent(jobId)}/montage/file/props.json`;
  }
  const { protocol, hostname, port } = window.location;
  if (port === "3000") {
    return `${protocol}//${hostname}:5000/job/${encodeURIComponent(jobId)}/montage/file/props.json`;
  }
  return null;
}

async function fetchJobMontagePropsJson(jobId: string): Promise<unknown | null> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 25_000);

  const tryFetch = async (url: string): Promise<unknown | null> => {
    const res = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
    if (!res.ok) return null;
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    if (!ct.includes("application/json")) {
      return null;
    }
    return (await res.json()) as unknown;
  };

  try {
    const staticUrl = staticFile(`jobs/${jobId}/props.json`);
    const fromStatic = await tryFetch(staticUrl);
    if (fromStatic) return fromStatic;

    const flaskUrl = montagePropsFlaskUrl(jobId);
    if (flaskUrl) {
      const fromFlask = await tryFetch(flaskUrl);
      if (fromFlask) return fromFlask;
    }
    return null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

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
                  const json = await fetchJobMontagePropsJson(jobId);
                  if (json) {
                    const parsed = jobMontagePropsSchema.safeParse(json);
                    if (parsed.success) {
                      resolved = parsed.data;
                    } else {
                      console.warn("JobMontage props.json validation failed", parsed.error.flatten());
                    }
                  } else {
                    console.warn(
                      "JobMontage: не удалось загрузить props.json (ни staticFile, ни Flask). job=",
                      jobId,
                    );
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
