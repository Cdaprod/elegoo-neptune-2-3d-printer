"""Filesystem handoff tests; no BLE, network receiver or printer is simulated as verified."""

import asyncio
from types import SimpleNamespace

from mks_wifi_bridge.completed_ingestion import CompletedDirectoryAdapter
from mks_wifi_bridge.fabrication_archive import FabricationArchive


def test_completed_handoff_is_idempotent_and_ignores_partial_files(tmp_path):
    archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "ledger.sqlite")))
    completed = tmp_path / "completed"
    completed.mkdir()
    (completed / "part.gcode.part").write_text("partial")
    (completed / ".hidden.gcode").write_text("hidden")
    (completed / "partial").mkdir()
    (completed / "partial/other.gcode").write_text("partial")
    outside = tmp_path / "outside.gcode"
    outside.write_text("outside")
    (completed / "link.gcode").symlink_to(outside)
    source = completed / "part.gcode"
    source.write_text("G90\nG1 X10 E1\n")
    adapter = CompletedDirectoryAdapter(archive, completed, settle_seconds=0)
    adapter.scan()
    source.write_text("G90\nG1 X20 E2\n")
    adapter.scan()
    assert adapter.status["ingested"] == 0, "Changing files must restart settling"
    adapter.scan()
    assert adapter.status["ingested"] == 1
    restarted = CompletedDirectoryAdapter(archive, completed, settle_seconds=0)
    restarted.scan()
    assert restarted.status["ingested"] == 0
    source.unlink()
    with archive.db() as db:
        rows = db.execute("SELECT * FROM artifact_provenance").fetchall()
        assert len(rows) == 1 and rows[0]["provider"] == "lilbitdrop"
        assert archive.verified(rows[0]["sha256"]).read_text() == "G90\nG1 X20 E2\n"
        assert db.execute("SELECT count(*) FROM artifact_stages").fetchone()[0] == 0
    missing = CompletedDirectoryAdapter(archive, tmp_path / "absent")
    missing.scan()
    assert missing.status["state"] == "unavailable"


def test_filesystem_notification_and_startup_reconciliation(tmp_path):
    async def run():
        archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "ledger.sqlite")))
        completed = tmp_path / "completed"
        completed.mkdir()
        adapter = CompletedDirectoryAdapter(archive, completed, settle_seconds=0)
        task = asyncio.create_task(adapter.run())
        try:
            await asyncio.sleep(0.1)
            temporary = tmp_path / "receiving.part"
            temporary.write_text("G1 X10 E1\n")
            temporary.rename(completed / "received.gcode")
            async with asyncio.timeout(8):
                while adapter.status["ingested"] != 1:
                    await asyncio.sleep(0.05)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
