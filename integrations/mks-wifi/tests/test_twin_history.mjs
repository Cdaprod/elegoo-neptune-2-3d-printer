import test from "node:test";
import assert from "node:assert/strict";
import { progressAtOffset, snapshotToHistorySample } from "../src/mks_wifi_bridge/static/twin_history.js";

function telemetrySnapshot(overrides = {}) {
  return {
    observed_at: "2026-11-01T12:00:00.000Z",
    stale: false,
    connection: { connected: true },
    telemetry: {
      printer_state: { value: "printing" },
      print_active: { value: true },
      progress_percent: { status: "available", value: 37, source_command: "M27" },
      current_file: { status: "available", value: "sample.gcode", source_command: "M994" },
      elapsed_seconds: { status: "available", value: 120 },
      nozzle_temperature: { status: "available", value: 205 },
      bed_temperature: { status: "available", value: 60 },
    },
    ...overrides,
  };
}

test("history sample stores only current MKS-reported print telemetry", () => {
  const sample = snapshotToHistorySample(telemetrySnapshot());
  assert.deepEqual(sample, {
    observed_at: "2026-11-01T12:00:00.000Z",
    file_name: "sample.gcode",
    state: "printing",
    progress_percent: 37,
    elapsed_seconds: 120,
    nozzle_c: 205,
    bed_c: 60,
  });
});

test("history capture rejects idle, stale, disconnected, missing-progress, and invalid samples", () => {
  assert.equal(snapshotToHistorySample(telemetrySnapshot({ stale: true })), null);
  assert.equal(snapshotToHistorySample(telemetrySnapshot({ connection: { connected: false } })), null);
  assert.equal(snapshotToHistorySample(telemetrySnapshot({ telemetry: { print_active: { value: false } } })), null);
  assert.equal(snapshotToHistorySample(telemetrySnapshot({
    telemetry: { ...telemetrySnapshot().telemetry, progress_percent: { status: "unsupported_or_not_reported", value: null } },
  })), null);
  assert.equal(snapshotToHistorySample(telemetrySnapshot({
    telemetry: { ...telemetrySnapshot().telemetry, progress_percent: { status: "available", value: 120 } },
  })), null);
});

test("historical progress interpolates by recorded time and clamps to the stored samples", () => {
  const session = {
    started_at: "2026-11-01T12:00:00.000Z",
    samples: [
      { observed_at: "2026-11-01T12:00:00.000Z", progress_percent: 10 },
      { observed_at: "2026-11-01T12:00:10.000Z", progress_percent: 30 },
      { observed_at: "2026-11-01T12:00:20.000Z", progress_percent: 50 },
    ],
  };
  assert.equal(progressAtOffset(session, 0), 10);
  assert.equal(progressAtOffset(session, 5000), 20);
  assert.equal(progressAtOffset(session, 15000), 40);
  assert.equal(progressAtOffset(session, 25000), 50);
});

test("historical progress starts at the oldest retained sample after bounded-history rollover", () => {
  const session = {
    started_at: "2026-11-01T08:00:00.000Z",
    retained_from_at: "2026-11-01T10:00:00.000Z",
    samples: [
      { observed_at: "2026-11-01T10:00:00.000Z", progress_percent: 40 },
      { observed_at: "2026-11-01T10:00:10.000Z", progress_percent: 42 },
    ],
  };
  assert.equal(progressAtOffset(session, 0), 40);
  assert.equal(progressAtOffset(session, 5000), 41);
});
