/** Анимация overlay-текста по timing + resolved_animation (секунды). */

export type OverlayTiming = {
  start_sec?: number;
  end_sec?: number;
  in_duration_sec?: number;
  out_duration_sec?: number;
};

export type ResolvedAnimPhase = {
  animation?: string;
  duration_sec?: number;
};

export type OverlayAnimState = {
  visible: boolean;
  opacity: number;
  translateX: number;
  translateY: number;
  scale: number;
};

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const easeOut = (t: number) => 1 - (1 - t) * (1 - t);

function normAnim(name: string | undefined): string {
  return String(name || "fade-in")
    .trim()
    .toLowerCase()
    .replace(/_/g, "-");
}

function applyInAnim(
  anim: string,
  p: number,
  state: OverlayAnimState,
): OverlayAnimState {
  const t = easeOut(clamp01(p));
  const next = { ...state, visible: true };
  switch (anim) {
    case "fly-up":
    case "fade-up":
      next.opacity = t;
      next.translateY = (1 - t) * 28;
      break;
    case "scale-in":
    case "scale-fade":
    case "scale-pop":
    case "big-word-pop":
      next.opacity = anim === "scale-fade" ? t : 1;
      next.scale = 0.86 + 0.14 * t;
      break;
    case "fade-in":
    case "fade-only":
    case "none":
    default:
      next.opacity = anim === "none" ? 1 : t;
      break;
  }
  return next;
}

function applyOutAnim(
  anim: string,
  p: number,
  state: OverlayAnimState,
): OverlayAnimState {
  const t = easeOut(clamp01(p));
  const next = { ...state, visible: t < 1 };
  switch (anim) {
    case "fly-up":
    case "fade-up":
      next.opacity = 1 - t;
      next.translateY = t * 12;
      break;
    case "scale-in":
    case "scale-fade":
      next.opacity = 1 - t;
      next.scale = 1 - 0.06 * t;
      break;
    default:
      next.opacity = 1 - t;
      break;
  }
  return next;
}

export function overlayAnimAtTimeSec(
  timeSec: number,
  timing: OverlayTiming | undefined,
  resolvedAnimation:
    | { in?: ResolvedAnimPhase; out?: ResolvedAnimPhase }
    | undefined,
): OverlayAnimState {
  const start = Number(timing?.start_sec ?? 0);
  const end = Number(timing?.end_sec ?? 0);
  const inDur = Math.max(0, Number(timing?.in_duration_sec ?? 0.45));
  const outDur = Math.max(0, Number(timing?.out_duration_sec ?? 0.3));
  const hidden: OverlayAnimState = {
    visible: false,
    opacity: 0,
    translateX: 0,
    translateY: 0,
    scale: 1,
  };

  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return hidden;
  }
  if (timeSec < start || timeSec > end) {
    return hidden;
  }

  const inAnim = normAnim(resolvedAnimation?.in?.animation);
  const outAnim = normAnim(resolvedAnimation?.out?.animation || "fade-out");
  const inEnd = start + inDur;
  const outStart = end - outDur;

  if (inDur > 0 && timeSec < inEnd) {
    return applyInAnim(inAnim, (timeSec - start) / inDur, hidden);
  }
  if (outDur > 0 && timeSec > outStart) {
    return applyOutAnim(outAnim, (timeSec - outStart) / outDur, {
      ...hidden,
      visible: true,
      opacity: 1,
    });
  }

  return { visible: true, opacity: 1, translateX: 0, translateY: 0, scale: 1 };
}

export function overlayAnimAtFrame(
  frame: number,
  fps: number,
  timing: OverlayTiming | undefined,
  resolvedAnimation:
    | { in?: ResolvedAnimPhase; out?: ResolvedAnimPhase }
    | undefined,
): OverlayAnimState {
  const fpsSafe = Math.max(1, fps || 30);
  return overlayAnimAtTimeSec(frame / fpsSafe, timing, resolvedAnimation);
}
