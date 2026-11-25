# Print Bay and Manufacturing as Code

Print Bay is part of Observatory at `/print-bay`, linked from its sidebar and the native Waveshare `/hmi` header. It uses the existing FastAPI authentication, static assets, Three.js/OrbitControls, OctoPrint backend and Compose stack. No additional web/controller service is required. The native Mac worker runs CAD and Orca; the optional Pi agent copies source snapshots and staged artifacts over the authenticated build-node API into its own local filesystem and SQLite registry. SQLite is never shared over LAN.

## Manifest contract

The authoritative v1alpha1 JSON schema is [`schemas/manufacturing.schema.json`](../../../schemas/manufacturing.schema.json). The concrete [display-mount example](../../../examples/manufacturing/manufacturing.yaml) maps the actual FreeCAD objects and 3MF resource IDs. Copy the manifest beside the CAD document and its source 3MF. Configure `NEPTUNE_MANUFACTURING_PROJECTS` to that directory or a directory containing project directories. Project names are unique manifest metadata, independent of directory names.

Paths are relative and confined to the project, including symlink resolution. Unknown fields, duplicate mappings, disabled validation, `autoPrint: true`, and disabling approval are rejected. `parameters` maps `Object.Property` to native FreeCAD property values; existing expressions cannot be silently replaced. `protectedParameters` asserts unchanged numeric design dimensions. CadQuery source receives a `PARAMETERS` dictionary and must publish named Workplanes/Shapes matching `cadObject`; declare imported source files under `dependencies`. Treat CAD scripts/manifests as trusted code reviewed in Git, never as untrusted uploads.

Each object explicitly declares its 3MF `meshPath`, `meshObjectId`, `projectObjectId`, and relevant `parameterKeys`. These are not inferred from filenames or labels. Plate names resolve through `Metadata/model_settings.config` to Orca's numeric `--slice` index; exact plate membership must match. Embedded machine, process, filament and object settings remain authoritative. External profile override files are fingerprinted but deliberately rejected at slicing until typed machine/process/filament precedence is implemented; embed effective profiles in the 3MF.

## Run on the Mac

```sh
cd integrations/mks-wifi
uv sync --locked --extra web --extra dev
export NEPTUNE_MANUFACTURING_PROJECTS='/path/to/projects'
export NEPTUNE_MANUFACTURING_STORAGE='/path/to/manufacturing'
export FREECAD_CMD='/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd'
export ORCASLICER_PATH='/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer'
uv run neptune-observatory --host 127.0.0.1 --port 8000
# In another terminal with the same environment:
uv run neptune-manufacturing
```

Use `--once` to process one queued job; `--watch` additionally applies each manifest's `autoBuild`, `autoSlice`, and `autoStage` policies. Polling watches content rather than mtimes. Slicer version changes invalidate the plate cache. Both CLI paths are configurable, checked at execution and never invented MCP commands. Missing CAD/slicer runtimes fail jobs while read-only monitoring continues. `CADQUERY_PYTHON` selects an interpreter with CadQuery installed. Native CAD saves a build copy, never the authoritative source document.

For LAN access, bind Observatory to the Mac's LAN interface and configure its existing `NEPTUNE_OBSERVATORY_TOKEN`. The agent must have that build-node token; keep it in private environment configuration. Use a trusted LAN or TLS reverse proxy. The token authorizes source snapshots containing CAD source and job submission.

## Run on the Pi

Build the existing Observatory image and apply the optional root `compose.manufacturing.yaml` overlay. Set `NEPTUNE_BUILD_NODE_URL` to the Mac's Observatory origin and `NEPTUNE_BUILD_NODE_TOKEN` to its token. The `manufacturing-agent` profile enables a small polling cache process, with no FreeCAD or Orca dependency. Observatory forwards build jobs to the Mac; the agent downloads fresh staged revisions, verifies every file checksum and mirrors desired source inputs. Offline, previously synchronized revisions remain available against the last synchronized desired state. New upstream changes cannot be discovered while offline; no automatic upload or print occurs. On reconnection, changed sources or invalidated slicer fingerprints make cached older revisions stale.

The agent's project directory is exclusively a disposable source mirror. Do not point it at an authoritative design workspace. The API and agent share a local Docker volume, not a network SQLite database. Cached G-code is independent of Mac availability. `NEPTUNE_MANUFACTURING_INVENTORY` optionally lists OS-path-separated read-only scan roots, limited to four levels and 250 candidates; discovery never imports or slices files.

For SD export, use `NEPTUNE_SD_MOUNT=/Volumes/NEPTUNE` on macOS. In Docker, add an explicit bind mount of the card at `/mnt/NEPTUNE`; it must be a real mount and its basename must match the declared volume, so configure a container path ending in `/NEPTUNE`. The application never creates a missing mountpoint, overwrites differing files, or deletes existing card contents. An identical file is a successful no-op. If the output name already contains different G-code, retain the card file and use an explicitly chosen alternate export filename (download through the registry), or manage the existing file yourself.

## API and state transitions

OpenAPI is available at `/docs`; the following routes use Observatory authentication. Mutation routes also enforce its existing same-origin policy.

| Endpoint | Meaning |
|---|---|
| `GET /api/manufacturing/projects` | Discover valid manifests and validation errors |
| `GET /api/manufacturing/inventory` | List unimported CAD/Orca candidates |
| `GET /api/manufacturing/projects/{name}/snapshot` | Declared source inputs as a ZIP for the deployment cache |
| `POST /api/manufacturing/projects/{name}/jobs` | Queue `{plate, action}`; never run expensive work in the request |
| `GET /api/manufacturing/jobs` | Queued/running/succeeded/failed build results |
| `GET /api/manufacturing/artifacts?project=…` | Revisions, freshness, checksums and provenance |
| `GET /api/manufacturing/artifacts/{id}/files/{filename}` | G-code, geometry, project, logs and metadata |
| `POST …/artifacts/{id}/stage` | Verify and mark a revision staged |
| `POST …/artifacts/{id}/deploy` | Explicit upload only; `{confirmation: "UPLOAD <id>"}` |
| `POST …/artifacts/{id}/export-sd` | Explicit SD copy; `{confirmation: "EXPORT <id>"}` |

Job actions are `build`, `slice`, `build-slice`, `slice-stage`, `build-slice-stage`, and `reconcile`. `slice`/`slice-stage` require current validated geometry; they never silently initiate a CAD build. The watcher runs building first when authorized by `autoBuild`. Physical deployment is never a job action.

Every slice generates a deterministic 320×320 PNG from the transformed plate meshes and embeds it as comments in G-code, because the installed headless Orca CLI did not emit its GUI PNG previews. This leaves printer instructions intact and is idempotent during cache reuse.

Artifacts include immutable G-code, reconciled 3MF, selected-object STL previews, BREP validation results, SHA-256 sidecar, source parameters, manifest, input identities and slicer version. Versions are monotonically allocated per project. `build.outputDirectory` is relative to the registry storage root. The geometry cache records checksums for native build output; artifact checksums are verified again before staging/deployment. Geometry-equivalent edits reuse G-code while creating a new provenance revision, preserving historical receipts. Plate fingerprints include selected meshes, relevant CAD parameters, membership, transformations, effective object settings/modifiers, embedded printer/process/filament settings and slicer version. Other plate geometry is excluded.

Reconciliation replaces mapped normal-part meshes only, applying the original source offsets and retaining transforms. All other ZIP entries are byte-checked against the source, preserving plate locks, membership, supports/settings, modifier geometry and untouched models. Facet painting on changed topology, shared mesh resources, nested/inline component layouts, ambiguous mappings and mismatched plates stop with an explicit error. This release does not promise arbitrary 3MF format coverage or topology-aware painting migration. Exporting a new standalone STL plate through MCP would lose project context, so this adapter invokes the verified installed CLI directly on the preserved project.

Upload additionally requires `NEPTUNE_MANUFACTURING_UPLOAD_ENABLED=true`, explicit per-artifact confirmation, configured OctoPrint, fresh idle telemetry, and Observatory's existing serialized OctoPrint control lock. It submits `select=false` and `print=false`. Upload acknowledgment is not physical completion; ambiguous uploads are never automatically retried. Starting a print remains a separate explicit operator action in OctoPrint. No endpoint in this feature starts a print.

A worker crash leaves its claimed job `running`; it is never automatically retried. Review logs and submit a new explicit build job. Source changes mid-build/slice, validation errors, checksum corruption and missing mounts prevent registration/deployment.

## Verification

Run `uv run --extra web --extra dev pytest -q`, `ruff check src tests`, `ruff format --check src tests`, and the existing Node checks. Manufacturing tests cover cache invalidation, preserved 3MF contents, unsupported painting, stale/corrupt artifact refusal, job outcomes, authenticated API behavior, SD collision preservation, non-printing OctoPrint uploads, and Pi offline caching/reconnection invalidation. The real integration evidence and UI preview are linked from the root README.

## Existing Neptune TFT thumbnails

On 2026-11-25, all 19 non-hidden G-code files read from the mounted NEPTUNE card contained valid 320×320 PNG thumbnails. None contained the legacy `simage`/`gimage` TFT encoding. This explains why the slicer preview setting can be enabled while the current printer display still shows no thumbnail. [Elegoo links the compatible converter](https://wiki.elegoo.com/en/machines/neptune/maintenance/neptune-4-series/cura-prusa-orca-slice-print-file-thumbnail-plugins); the [postprocessor documentation](https://github.com/Molodos/ElegooNeptuneThumbnails-Prusa#installation) separately requires PNG thumbnail generation and post-processing, and explicitly supports Neptune 2. Firmware has not been changed and TFT display behavior has not been physically verified.

Observatory's existing PNG/JPEG extractor reads these previews without firmware changes. Its `.GCO` indexing omission is corrected here, including uppercase short SD filenames and exclusion of AppleDouble `._` files. With the mounted card configured as its local source, the updated index found all 19 previews read-only. The manufacturing provenance additionally records embedded image dimensions and whether legacy TFT markers are present. Marker presence is reported as unverified, not proof of a working TFT preview. No existing SD files were rewritten and no converter was silently installed. A tested target-specific postprocessor remains necessary before promising stock-TFT thumbnail compatibility; interactive Waveshare/Observatory previews use the actual STL geometry independently.
