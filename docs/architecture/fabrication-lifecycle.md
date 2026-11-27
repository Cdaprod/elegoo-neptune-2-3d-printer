# Persistent fabrication lifecycle — implementation checkpoint

Schema version 1 adds content-addressed `fabrication_artifacts`, provenance-bearing
`artifact_locations` and independent `artifact_stages` to the existing timeline
SQLite database. `print_sessions` remains the observed execution ledger; artifacts
are associated through `gcode_files.sha256`, never guessed from matching filenames.
The existing manufacturing project/revision/worker system remains unchanged.

```mermaid
erDiagram
    ManufacturingProject ||--o{ ModelRevision : versions
    ModelRevision ||--o{ FabricationArtifact : slices
    FabricationArtifact ||--o{ ArtifactLocation : discovered_at
    FabricationArtifact ||--o{ PrintExecution : manufactured_as
    PrintExecution ||--o{ ExecutionEvent : observes
    PrintExecution ||--o{ QualityAssessment : assessed_by_operator
```

This diagram describes the intended complete domain. The current migration covers
artifacts, locations and staging alongside existing sessions, telemetry and events;
model-revision/quality-assessment consolidation is still pending. A staging receipt
is not an execution and must not be represented as a completed print.

## Storage contract

Original bounded G-code is streamed to a temporary file, SHA-256 addressed, fsynced
and atomically published beside the existing persistent timeline database under
`fabrication-archive/`. Original bytes are never preview-cache eviction candidates.
Reconciliation is idempotent. Identical names with different bytes remain separate.
Deleting an upload does not remove its archived bytes. The API verifies integrity
before derivative generation and repeat staging. Derivatives reuse the existing
versioned filament approximation pipeline; they are not physical measurements.
No fixture records are automatically created. Archives retain the production
volume's existing credentials and deployment architecture.

Authenticated endpoints: `GET /api/fabrication/history|unprinted` use bounded,
stable `(timestamp,identity)` keyset pages; `POST /api/fabrication/reconcile`
archives current local indexed bytes. Artifact geometry/thumbnail endpoints reuse
one interactive HMI renderer. `POST /api/fabrication/executions/{id}/stage`
creates a fresh staging identity for verified bytes without issuing printer commands.
The UI exposes these collections in the existing horizontal gallery and retains
explicit unavailable indicators for metadata-only history.

## Remaining integration gates — not yet production-complete

- Manufacturing source-model ingestion; local files now ingest automatically in bounded
  four-file passes, and verified native-SD transfers archive before completion.
- Durable execution-to-artifact links independent of timeline cache eviction, removal
  of legacy session retention, OctoPrint event reconciliation and explicit events.
- Independently scrollable simultaneous chronological lanes, pagination controls,
  persistent selection, Print Bay compatibility validation and staging receipt delivery.
- Source-model revisions, operator assessments, media attachments and complete replay.
- Shared geometry presentation modes and asynchronous derivative work queue.
- Transaction-consistent backup/restore tooling and regression validation.
- Full browser/ARM64/OctoPrint CI and target-resolution rendered archive screenshots.

Do not merge this checkpoint as the complete lifecycle feature. Existing legacy
session retention has now been removed. Execution-to-SHA links survive geometry
cache eviction; previously deleted history cannot be reconstructed without external records.

For an offline backup, stop the application normally, copy the existing timeline
SQLite database and its adjacent fabrication-archive directory together, then
restart. Restore both into the same persistent data volume with original ownership.
Do not copy only SQLite while omitting original artifact bytes. Online backup and
restore verification remain an explicit delivery gate.

No physical print, heating, motion, upload or firmware operation is part of these
software tests. Physical SD retrieval and Pi frame-rate validation remain separately
commissioned capabilities.

## Motherboard SD backup policy

The default durable location is `/data/fabrication-archive`, beside the persisted
timeline database, not on the removable card or in the evictable native-SD cache.
A verified native transfer copies into this archive before reporting completion.
`NEPTUNE_NATIVE_SD_AUTO_ARCHIVE=true` enables a bounded background pass (one new
transfer per minute, exclusive existing USB owner); it also requires separately
enabled, commissioned RSD/1 capability. Failed/cancelled transfers require an
operator retry. Factory firmware metadata-only entries are never marked backed up.
No automatic hardware operations were run to validate this experimental opt-in.
Original bytes and execution records survive card reformatting only after bytes
have actually been acquired. No software can reconstruct missing original bytes
from M20 filenames or M23 sizes.

## Unified ingestion contract (schema 1)

Many source adapters → `FabricationIngestion.submit(ArtifactSubmission, stream)` →
one canonical archive → gallery, history, derivatives and staging consumers.

`ArtifactSubmission` carries original filename, adapter provider, source URI,
artifact kind and schema version. Canonical identity is always SHA-256 of the
original bytes, independent of filename, adapter and transport. Schema migration 2
adds idempotent provenance records (including original names, first/last arrival)
and typed relationship storage: sliced_from, derived_from, observes, configured_by.
Different sources submitting identical bytes create one canonical artifact and
multiple provenance records. Ingestion never stages, selects or starts a print.

Implemented adapters: configured local-file scans, verified native-SD transfers,
existing browser/HMI G-code upload, and authenticated raw HTTP G-code submission
at `POST /api/fabrication/ingest?filename=part.gcode` (application/octet-stream).
The HTTP endpoint requires existing write authentication and origin protections;
it works without OctoPrint and performs no delivery or execution. HMI legacy
uploads archive through the same service before their existing local delivery.

Removable-media bulk hydration still needs adapter/UI integration with the existing
SD importer. OrcaSlicer direct submission, network storage protocols and non-G-code formats
remain future adapters. LilBitDrop discovery infrastructure is installed and operator-verified on the Pi;
the receiving-side completed-file transfer lifecycle remains unverified. Reuse that established service rather than rebuilding its transport.
The receiving directory/completion contract must be identified before wiring the
adapter, so partially received files are never treated as complete artifacts.
Native Apple AirDrop compatibility is not inferred from this handoff. The contract currently
accepts execution_gcode only; unsupported artifact kinds fail explicitly. CAD,
slicer projects, photos and derivatives must gain typed validation before being
accepted, rather than masquerading as G-code. Source URIs are provenance labels,
not URLs fetched by the ingestion service.

## LilBitDrop completed-directory adapter

`CompletedDirectoryAdapter` uses watchfiles filesystem notifications plus startup
and periodic reconciliation. The bounded adapter reads at most four new files per
scan, ignores hidden/system/partial/symlink paths and non-G-code suffixes, waits
for stable file metadata, and uses the unified ingestion contract with provider
`lilbitdrop`. Completion receipts persist across restarts, while originals remain
independent of the incoming folder. It never stages, uploads to OctoPrint or prints.
Status, pending counts, current file and bounded errors are exposed through
`GET /api/fabrication/ingestion/status` and HMI Settings.

Receiver contract: receive into `/srv/neptune/incoming/partial/`, close and fsync the
completed file, then atomically rename into `/srv/neptune/incoming/completed/` on
the same filesystem. Never write into an already published completed file.
Settling and mutation checks are defense in depth, not proof of completion for a
receiver that violates this contract. Observatory never deletes incoming files.
The adapter ignores temporary suffixes such as `.part` and `.partial`.

The optional `compose.lilbitdrop.yaml` adds only a read-only bind of completed/; it
requires that the host directory already exists and never grants privileged mode.
Set `COMPOSE_FILE=compose.yaml:compose.lilbitdrop.yaml` in the existing private .env
(or append this overlay after an existing USB overlay). Keep the current deployment
checkout, existing `/srv/neptune-observatory/` configuration and existing persistent
data volume; there is no new canonical storage tree. Override
`NEPTUNE_LILBITDROP_HOST_COMPLETED_DIR` if the actual receiver uses another path.
The internal read-only mount is `/mnt/lilbitdrop/completed`. Direct non-Compose
configuration uses `NEPTUNE_LILBITDROP_INGEST_ENABLED=true` and
`NEPTUNE_LILBITDROP_COMPLETED_DIR` (default `/srv/neptune/incoming/completed`).

The BLE prototype `~/lilbitdrop-lab/peripheral.py`, advertised name
`CDA Neptune PrintBay`, and wlan0 are operator-reported context. No Bluetooth, WiFi
interface, SSH trust or receiver changes are made by this adapter. GitHub searches
and direct repository lookup did not reveal an accessible LilBitDrop repository;
its precise URL/access and receiving implementation still need reconciliation.
No complete wireless-transfer or Pi deployment validation is claimed.
