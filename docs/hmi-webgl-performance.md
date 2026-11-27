# Waveshare HMI: Three.js performance and image sharpness

## Diagnosed rendering issues

The previous studio scene capped ordinary operation at 10 FPS, used an internal
pixel ratio of 0.85 in balanced mode and added a 1.2px CSS blur to the
full-screen ambient canvas. The compositor additionally blurred high-contrast
edges in a fragment shader. The G-code model inspector rendered continuously
at requestAnimationFrame rate, even when no orbit/zoom interaction occurred.

## Implementation

- Balanced studio now targets **24 FPS at 1.0x native pixel density**; cinematic
  targets **30 FPS at 1.2x**, and low uses 20 FPS at 0.75x.
- Adaptive scale adjustment now works in ordinary, not just cinematic, mode.
  The renderer progressively reduces framebuffer resolution under sustained
  frame pressure rather than assuming the Pi 4 can always keep up.
- Removed the separate CSS blur and the per-pixel high-contrast edge diffusion;
  preserved saturation and the cinematic grade.
- Throttled shadow map regeneration to approximately 5.6 times/second, while
  keeping model animation and responsive UI updates independent.
- When the G-code inspector is open, the *background studio* renders at only
  2 FPS, reserving GPU resources for the interactive view.
- G-code inspector uses one-pass grouping for instancing, 10-sided filament
  cylinders instead of 6, bounded 1x–1.5x supersampling according to segment
  count, and only renders while interacting or while damping settles.
- Inspector disposal cancels scheduled animation, releases controls, GPU
  geometry, materials, render context, and observers; document visibility
  resumes invalidated renders.

## Validate on the Raspberry Pi 4

Use the existing Observatory deployment instructions and an idle printer.

1. Open the HMI at 1280x400 and select **Settings -> Balanced**.
2. Inspect the renderer diagnostics: frame rate, scale and draw calls. Balanced
   starts at 1.0x, but may reduce under GPU pressure. Compare stable FPS for
   at least 60 seconds with previous deployment.
3. Switch to cinematic idle mode; inspect crisp architectural edges and
   the thermal/fabrication variations, watching temperature and browser load.
4. Open a locally available G-code file's interactive Three.js inspector.
   Observe contour smoothness, touch orbit, top/front/iso controls and resize.
   Leave it untouched: GPU redraws should cease after damping settles.
5. Verify Print Bay preview, background HMI, native touch controls,
   opening/closing the inspector repeatedly, and no printer API writes.

The physical 1280x400 panel cannot show more than 1280x400 distinct pixels.
Supersampling improves antialiasing of edges; it does not increase the display's
native physical resolution. If performance regresses, select **Low** in HMI
Settings and compare diagnostics.

CI: `node scripts/test-hmi-three-performance.cjs` checks native studio
pixel density, lack of CSS blur, bounded inspector supersampling,
idle redraw suspension, view interactions, disposal, and no printer writes.
