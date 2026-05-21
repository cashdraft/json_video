export const defaultLaterInfographicProps = {
  schema: "later_infographic_props@1",
  fps: 30,
  width: 1920,
  height: 1080,
  duration_sec: 5,
  duration_frames: 150,
  svg: `<svg viewBox="0 0 1920 1080" xmlns="http://www.w3.org/2000/svg">
  <rect width="1920" height="1080" fill="#05060d"/>
  <g id="demo-title"><text x="960" y="540" fill="#fff" font-size="64" text-anchor="middle">Later Infographic</text></g>
</svg>`,
  animation: {
    fps: 30,
    duration_sec: 5,
    tracks: [
      { id: "demo-title", anim: "fade-in", start: 0, end: 45 },
    ],
  },
};
