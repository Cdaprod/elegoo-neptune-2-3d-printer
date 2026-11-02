# Observatory storage and recovery

Neptune Observatory stores its SQLite history and command/capability records in
the Compose named volume `mks-wifi_commissioning-data`, mounted at `/data`.
The database is `/data/neptune-observatory.db`; it is not in the disposable
container writable layer. SQLite journal files, if present, are created beside
the database in the same mounted volume.

The `/api/diagnostics` response includes `history_storage` with the database
size, available bytes, whether the path is persistent, and a storage status.
`/health` reports `degraded` if the shared poller is down or history storage is
unavailable, low on free space, or not persistent. This is an on-demand check;
it starts no background storage job. If a SQLite write fails because the disk
is full, the API returns an actionable insufficient-storage response and live
telemetry remains independent of history persistence.

The Compose service rotates JSON logs at 10 MB and keeps at most three files.
This bounds the Observatory container's own log footprint.

For recovery, inspect Docker Desktop capacity and `docker system df -v` first.
The supported safe cleanup is reclaimable BuildKit build cache only. Do not
prune volumes, images, or containers to recover Observatory history. After
recovery, check `PRAGMA quick_check` and verify `/health` and
`/api/diagnostics` before considering deployment restored.

Do not publish local Docker Desktop capacity, filesystem paths, or database
sizes as project defaults. Those values vary by host; use the diagnostics
endpoint and Docker Desktop storage panel on the machine running the service.
