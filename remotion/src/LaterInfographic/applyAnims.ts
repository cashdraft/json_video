/** Кубики anim из later_anim_dictionary (кадры start/end). */

export type AnimTrack = {
  id: string;
  anim: string;
  start?: number;
  end?: number;
};

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));

const easeOut = (t: number) => 1 - (1 - t) * (1 - t);

export function trackProgress(
  frame: number,
  start: number,
  end: number,
): number | null {
  if (end <= start) {
    return frame >= start ? 1 : null;
  }
  if (frame < start) return null;
  if (frame >= end) return 1;
  return easeOut((frame - start) / (end - start));
}

function parseNumberText(raw: string): number | null {
  const cleaned = raw.replace(/[^\d.,\-+]/g, "").replace(",", ".");
  const n = parseFloat(cleaned);
  return Number.isFinite(n) ? n : null;
}

function strokeTarget(el: SVGElement): SVGGeometryElement | null {
  if (
    el instanceof SVGLineElement ||
    el instanceof SVGPathElement ||
    el instanceof SVGPolylineElement
  ) {
    return el;
  }
  const inner = el.querySelector("line, path, polyline");
  if (inner instanceof SVGLineElement || inner instanceof SVGPathElement) {
    return inner;
  }
  return null;
}

function lineLength(line: SVGLineElement): number {
  const x1 = line.x1.baseVal.value;
  const y1 = line.y1.baseVal.value;
  const x2 = line.x2.baseVal.value;
  const y2 = line.y2.baseVal.value;
  return Math.hypot(x2 - x1, y2 - y1) || 1;
}

function pathLength(geom: SVGGeometryElement): number {
  try {
    if ("getTotalLength" in geom && typeof geom.getTotalLength === "function") {
      const len = geom.getTotalLength();
      if (len > 0) return len;
    }
  } catch {
    /* ignore */
  }
  if (geom instanceof SVGLineElement) return lineLength(geom);
  return 100;
}

function countUpTarget(el: Element): SVGTextElement | null {
  if (el instanceof SVGTextElement) return el;
  const t = el.querySelector("text");
  return t instanceof SVGTextElement ? t : null;
}

function resetElement(el: SVGElement) {
  el.style.opacity = "";
  el.style.transform = "";
  el.style.transformOrigin = "";
  el.style.clipPath = "";
  el.style.visibility = "";
  const stroke = strokeTarget(el);
  if (stroke) {
    stroke.style.strokeDasharray = "";
    stroke.style.strokeDashoffset = "";
  }
}

export function applyAnimToElement(
  el: SVGElement,
  anim: string,
  progress: number | null,
) {
  resetElement(el);

  if (progress === null) {
    el.style.visibility = "hidden";
    el.style.opacity = "0";
    return;
  }

  const t = clamp01(progress);
  const animName = (anim || "none").trim();

  switch (animName) {
    case "none": {
      el.style.visibility = "visible";
      el.style.opacity = "1";
      break;
    }
    case "fade-in": {
      el.style.visibility = "visible";
      el.style.opacity = String(t);
      break;
    }
    case "fade-out": {
      el.style.visibility = "visible";
      el.style.opacity = String(1 - t);
      break;
    }
    case "fly-up": {
      const dy = 80 * (1 - t);
      el.style.visibility = "visible";
      el.style.opacity = String(t);
      el.style.transform = `translateY(${dy}px)`;
      break;
    }
    case "grow-y": {
      el.style.visibility = "visible";
      el.style.opacity = "1";
      el.style.transformOrigin = "center bottom";
      el.style.transform = `scaleY(${t})`;
      break;
    }
    case "grow-x": {
      el.style.visibility = "visible";
      el.style.opacity = "1";
      el.style.transformOrigin = "left center";
      el.style.transform = `scaleX(${t})`;
      break;
    }
    case "scale-in": {
      el.style.visibility = "visible";
      el.style.opacity = String(Math.min(1, t * 1.2));
      el.style.transformOrigin = "center center";
      el.style.transform = `scale(${t})`;
      break;
    }
    case "draw-path": {
      el.style.visibility = "visible";
      el.style.opacity = "1";
      const stroke = strokeTarget(el);
      if (stroke) {
        const len = pathLength(stroke);
        const dash = len * (1 - t);
        stroke.style.strokeDasharray = String(len);
        stroke.style.strokeDashoffset = String(dash);
      } else {
        el.style.transformOrigin = "left center";
        el.style.transform = `scaleX(${t})`;
      }
      break;
    }
    case "count-up": {
      el.style.visibility = "visible";
      el.style.opacity = "1";
      const textEl = countUpTarget(el);
      if (textEl) {
        const finalRaw = textEl.getAttribute("data-final-value") ?? textEl.textContent ?? "";
        if (!textEl.getAttribute("data-final-value")) {
          textEl.setAttribute("data-final-value", finalRaw);
        }
        const target = parseNumberText(finalRaw);
        if (target !== null) {
          const shown = Math.round(target * t);
          const prefix = finalRaw.match(/^[^\d\-+]*/)?.[0] ?? "";
          const suffix = finalRaw.match(/[^\d.,]*$/)?.[0] ?? "";
          textEl.textContent = `${prefix}${shown}${suffix}`.trim() || String(shown);
        }
      }
      break;
    }
    default: {
      el.style.visibility = "visible";
      el.style.opacity = "1";
      break;
    }
  }
}

export function applyTracksToSvg(
  svgRoot: SVGSVGElement,
  tracks: AnimTrack[],
  frame: number,
) {
  const trackedIds = new Set(tracks.map((tr) => tr.id).filter(Boolean));

  for (const tr of tracks) {
    const id = tr.id?.trim();
    if (!id) continue;
    const node = svgRoot.querySelector(`#${CSS.escape(id)}`);
    if (!(node instanceof SVGElement)) continue;
    const start = typeof tr.start === "number" ? tr.start : 0;
    const end = typeof tr.end === "number" ? tr.end : start + 30;
    const progress = trackProgress(frame, start, end);
    applyAnimToElement(node, tr.anim || "none", progress);
  }

  svgRoot.querySelectorAll("[id]").forEach((node) => {
    if (!(node instanceof SVGElement)) return;
    const id = node.id;
    if (!id || trackedIds.has(id)) return;
    node.style.visibility = "visible";
    node.style.opacity = "1";
  });
}
