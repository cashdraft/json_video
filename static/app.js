/**
 * JSON Video Generator - Frontend logic
 */

document.addEventListener('DOMContentLoaded', () => {
  const textarea = document.getElementById('json_input');
  const loadExampleBtn = document.getElementById('load-example');
  const clearBtn = document.getElementById('clear-btn');

  const EXAMPLE_JSON = `{"scene_id":"scene_263"}
{"text":"It's about becoming intentional with your money instead of letting it slip away."}
{"start":{"prompt":"Create one single slide in 16:9 aspect ratio. Use the attached character reference EXACTLY for Naomi – The Math Girl (face, skin tone, hair texture, body proportions, outfit, accessories). Do NOT redesign her. She must remain fully recognizable. Use the attached design references ONLY for overall visual style, lighting direction, composition energy and infographic boldness — not for copying layout 1:1. STYLE 2D digital illustration, high-end modern YouTube finance explainer style. Semi-realistic cartoon proportions. Clean bold lineart Strong confident outlines Smooth cel shading (2–3 shadow layers) Soft but directional studio lighting No photorealistic skin texture No painterly strokes No noise Crisp, sharp, thumbnail-ready VISUAL ENERGY Bold Clear Trusted High contrast Large headline blocks Strong color separation Clear infographic hierarchy Minimal micro-details COLOR SYSTEM Base: white or very light grey background Structure: dark grey / black Accent colors strictly limited to: Yellow — headline emphasis Red — loss / warning Green — gain / growth COMPOSITION RULES 16:9 horizontal frame Naomi placed slightly left or right of center Infographic elements dominant in upper and middle area IMPORTANT: Leave bottom 25% of the frame visually clean No text No graphics No important information This area is reserved for subtitles No clutter Clean balanced spacing TEXT RULES Typography must be: large bold clean undistorted No perspective distortion No small unreadable text No paragraph blocks Only short punchy phrases DO NOT Make it photorealistic Make it anime chibi Place text near bottom Overcrowd layout Add background noise Naomi guiding money streams into a structured plan instead of leaks"}}
{"end":{"prompt":null}}
{"video":{"prompt":"Money icons begin leaking from scattered spending holes, Naomi redirects glowing streams into organized investment and savings channels, green growth arrows appear, slight infographic animation."}}

{"scene_id":"scene_264"}
{"text":"On things that don't actually matter to you."}
{"start":{"prompt":"Create one single slide in 16:9 aspect ratio. Use the attached character reference EXACTLY for Naomi – The Math Girl ... Naomi crossing out random impulse purchases on a spending board"}}
{"end":{"prompt":null}}
{"video":{"prompt":null}}

{"scene_id":"scene_265"}
{"text":"When you have clear financial goals and understand the power of compound interest."}
{"start":{"prompt":"Create one single slide in 16:9 aspect ratio. Use the attached character reference EXACTLY for Naomi – The Math Girl ... Naomi pointing to a long-term wealth growth curve labeled COMPOUNDING"}}
{"end":{"prompt":null}}
{"video":{"prompt":null}}`;

  if (loadExampleBtn) {
    loadExampleBtn.addEventListener('click', () => {
      textarea.value = EXAMPLE_JSON;
      textarea.focus();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      textarea.value = '';
      textarea.focus();
    });
  }
});
