# G-code-derived 3D fabrication gallery

## What changed

The existing `/hmi` **Fabrication gallery** page on the Waveshare 7.9-inch (1280 × 400) display
now prefers generated shaded **3D depictions of deposited G-code extrusion** instead
of embedded TFT/OrcaSlicer thumbnails. The gallery remains a horizontally
swipeable row. Tapping a card opens a Three.js inspector with orbit,
zoom, isometric, top and front views.

These are toolpath-derived approximations of deposited filament, **not**
original CAD solids or simulated photographs. The renderer can depict
perimeters, infill and supports when present in G-code.

## Data and execution

1. Existing read-only `LocalGcodeIndexer` indexes `.gcode` files under
   `/mnt/neptune-source`.
2. Authenticated `GET /api/hmi/model-preview?path=...` reconstructs and renders
   a shaded SVG geometry preview for each local file.
3. Authenticated `GET /api/hmi/model-geometry?path=...` provides capped
   sampled extrusion segments to the bundled Three.js inspector.
4. Cached geometry JSON is kept in the existing named `/data` volume, keyed
   by relative path, source size, mtime, ctime and renderer version.
   Changing the file invalidates the preview.
5. Missing source data, no extrusion, or render errors fall back to existing
   cached slicer thumbnails when present.

The renderer does not connect to the MKS controller, issue machine commands,
or write to source folders. It uses read-only, no-follow file descriptors
and rejects traversal. Limits include a 50 MiB source size, 1.5 million lines,
7,500 sampled extrusion segments, two simultaneous render operations, and an
80 MiB persistent JSON cache. Sampling spans the whole G-code instead of
stopping at an arbitrary layer. Cache failures do not prevent rendering.

### Geometric limitations

- No original STL, STEP, 3MF mesh or exact extrusion physics is reconstructed.
- G0/G1 extrusion, G90/G91, M82/M83, G92, G20/G21, and XY I/J
  G2/G3 arcs are processed.
- R-based arcs are approximated by a chord and explicitly reported.
- Nonstandard commands or unusual slicer semantics can yield an incomplete
  representation; verify the real print with the slicer.
- Printer M20 SD listings supply filenames, not raw toolpaths. Without a
  corresponding locally mounted G-code, true geometry reconstruction is
  impossible; the old thumbnail may still be displayed.
- This is an on-demand cached renderer within the existing Observatory app,
  not a separate Blender worker. The MacBook does not need to remain online.

## Docker Compose

The original Observatory at `/` and new HMI at `/hmi` stay in the
**same** Docker container. Existing `/data` persistence is retained.

For a standalone Observatory Compose deployment, point to an existing
absolute host directory containing sliced G-code exports:

```sh
export NEPTUNE_GCODE_HOST_DIR="/absolute/path/to/OrcaSlicer/exports"
docker compose -f integrations/mks-wifi/compose.yaml \
  -f integrations/mks-wifi/compose.fabrication.yaml up -d --build
```

Open `http://<docker-host>:8000/hmi` and swipe to **Fabrication gallery**.
The overlay adds only a read-only bind mount; it does not change printer
transport or controls. `compose.hmi.yaml` can additionally publish port 8001.

For the **root OctoPrint-combined deployment**, `make deploy` builds the existing
Observatory and OctoPrint services. Observatory receives the existing OctoPrint data
volume read-only at `/mnt/octoprint` and indexes only `/mnt/octoprint/uploads`.
The existing volume identity and contents are preserved. No additional mount,
frontend port, printer connection or application instance is required. Files that
exist only on the physical printer SD still need local bytes for reconstruction.

Use the same authenticated origin for `/hmi` and `/api/hmi/*`. Never place
the Observatory token in preview URLs.

## Validation

```sh
cd integrations/mks-wifi
uv run --extra dev --extra web pytest -q tests/test_gcode_fabrication.py tests/test_observatory.py
cd ../..
node --check integrations/mks-wifi/src/mks_wifi_bridge/static/hmi.js
```

CI also checks the 1280 × 400 Chromium viewport, horizontal scroll,
the selected-model inspector, and absence of printer command writes.
The screenshot artifact uses **simulated fixture models**, not production
printer telemetry or real user CAD.

## Recovered integration

The unmerged `feat/gcode-3d-fabrication-gallery` delta was reconciled against the
commissioned `c585943` baseline; the HMI branch had already been squash-merged as
`2ba7b8a` (PR #26). The controller and Print Bay from PR #28 are retained rather
than replaced. Source geometry in Print Bay uses actual exported STL; this gallery
uses bounded G-code extrusion reconstruction. The model inspector now resolves
bundled Three.js imports explicitly. Model orbit and pinch do not navigate panels;
carousel swipes do not accidentally select a model. See the [integrated HMI guide](waveshare-hmi.md)
for actual 1280×400 fixture screenshots and deployment acceptance limits.
