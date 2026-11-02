const DATABASE = "neptune-observatory-twin-history";
const VERSION = 2;
const SESSIONS = "sessions";
const SAMPLES = "samples";
const LEGACY_SESSIONS = "print-sessions";
const OPT_IN_KEY = "neptune-observatory-twin-history-enabled";
const MAX_SESSIONS = 16;
const MAX_SAMPLES_PER_SESSION = 3600;
let databasePromise;
let activeSessionExists = null;

function validNumber(value) {
  return Number.isFinite(value) ? value : null;
}

export function snapshotToHistorySample(snapshot) {
  if (!snapshot || snapshot.stale || snapshot.connection?.connected !== true) return null;
  const telemetry = snapshot.telemetry || {};
  if (telemetry.print_active?.value !== true) return null;
  if (!["printing", "paused"].includes(telemetry.printer_state?.value)) return null;
  if (telemetry.progress_percent?.status !== "available") return null;
  const progress = Number(telemetry.progress_percent.value);
  const fileName = String(telemetry.current_file?.value || "").trim();
  const observedAt = snapshot.observed_at;
  if (!fileName || !Number.isFinite(progress) || progress < 0 || progress > 100 || !Number.isFinite(Date.parse(observedAt))) return null;
  return {
    observed_at: observedAt,
    file_name: fileName,
    state: telemetry.printer_state.value,
    progress_percent: progress,
    elapsed_seconds: validNumber(telemetry.elapsed_seconds?.value),
    nozzle_c: validNumber(telemetry.nozzle_temperature?.value),
    bed_c: validNumber(telemetry.bed_temperature?.value),
  };
}

function openDatabase() {
  if (!globalThis.indexedDB) return Promise.reject(new Error("IndexedDB is not available."));
  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE, VERSION);
      request.onupgradeneeded = (event) => {
        const database = request.result;
        if (!database.objectStoreNames.contains(SESSIONS)) database.createObjectStore(SESSIONS, { keyPath: "id" });
        const sessionStore = request.transaction.objectStore(SESSIONS);
        if (!database.objectStoreNames.contains(SAMPLES)) {
          const samples = database.createObjectStore(SAMPLES, { keyPath: "id" });
          samples.createIndex("session_id", "session_id", { unique: false });
        }
        if (event.oldVersion < 2 && database.objectStoreNames.contains(LEGACY_SESSIONS)) {
          const sampleStore = request.transaction.objectStore(SAMPLES);
          const legacyRequest = request.transaction.objectStore(LEGACY_SESSIONS).getAll();
          legacyRequest.onsuccess = () => {
            for (const legacy of legacyRequest.result || []) {
              const allSamples = Array.isArray(legacy.samples) ? legacy.samples : [];
              const retained = allSamples.slice(-MAX_SAMPLES_PER_SESSION);
              if (!legacy.id || !legacy.file_name || retained.length === 0) continue;
              const id = String(legacy.id);
              const firstSequence = allSamples.length - retained.length;
              const samples = retained.map((sample, index) => ({
                id: `${id}|${firstSequence + index}`,
                session_id: id,
                sequence: firstSequence + index,
                ...sample,
              }));
              const firstRetainedAt = retained[0].observed_at || legacy.started_at;
              sessionStore.put({
                id,
                file_name: legacy.file_name,
                started_at: legacy.started_at || firstRetainedAt,
                retained_from_at: firstRetainedAt,
                last_seen_at: legacy.last_seen_at || retained.at(-1).observed_at || firstRetainedAt,
                ended_at: legacy.ended_at || legacy.last_seen_at || retained.at(-1).observed_at || firstRetainedAt,
                active: false,
                sample_count: retained.length,
                sample_total: allSamples.length,
              });
              for (const sample of samples) sampleStore.put(sample);
            }
          };
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("Could not open local history storage."));
    });
  }
  return databasePromise;
}

function requestResult(request, fallbackMessage) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error(fallbackMessage));
  });
}

function transactionPromise(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("Local history write failed."));
    transaction.onabort = () => reject(transaction.error || new Error("Local history write was aborted."));
  });
}

export function recordingEnabled() {
  try { return globalThis.localStorage?.getItem(OPT_IN_KEY) === "true"; }
  catch { return false; }
}

export async function setRecordingEnabled(enabled) {
  try { globalThis.localStorage?.setItem(OPT_IN_KEY, enabled ? "true" : "false"); }
  catch { throw new Error("This browser does not allow local storage preferences."); }
  if (!enabled) await closeActiveSessions();
  return Boolean(enabled);
}

async function closeActiveSessions() {
  const database = await openDatabase();
  const transaction = database.transaction(SESSIONS, "readwrite");
  const store = transaction.objectStore(SESSIONS);
  const done = transactionPromise(transaction);
  store.getAll().onsuccess = (event) => {
    const sessions = event.target.result || [];
    const now = new Date().toISOString();
    for (const session of sessions) {
      if (session.active) store.put({ ...session, active: false, ended_at: session.ended_at || now, last_seen_at: now });
    }
  };
  await done;
  activeSessionExists = false;
}

function deleteSession(database, sessionId, sampleStore) {
  // The caller supplies a store from the same read/write transaction.
  const keys = sampleStore.index("session_id").getAllKeys(sessionId);
  keys.onsuccess = () => { for (const key of keys.result || []) sampleStore.delete(key); };
}

export async function recordProgressSnapshot(snapshot) {
  const sample = snapshotToHistorySample(snapshot);
  const enabled = recordingEnabled();
  if ((!enabled || !sample) && activeSessionExists === false) return;
  const database = await openDatabase();
  const transaction = database.transaction([SESSIONS, SAMPLES], "readwrite");
  const sessionsStore = transaction.objectStore(SESSIONS);
  const samplesStore = transaction.objectStore(SAMPLES);
  const done = transactionPromise(transaction);
  sessionsStore.getAll().onsuccess = (event) => {
    const all = event.target.result || [];
    const open = all.filter((session) => session.active).sort((a, b) => b.started_at.localeCompare(a.started_at))[0];
    const now = snapshot?.observed_at || new Date().toISOString();
    if (!enabled || !sample) {
      if (open) sessionsStore.put({ ...open, active: false, ended_at: open.ended_at || now, last_seen_at: now });
      activeSessionExists = false;
    } else if (!open || open.file_name.toLowerCase() !== sample.file_name.toLowerCase()) {
      if (open) sessionsStore.put({ ...open, active: false, ended_at: open.ended_at || now, last_seen_at: now });
      const id = `${Date.parse(sample.observed_at)}-${globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`;
      const session = { id, file_name: sample.file_name, started_at: sample.observed_at, retained_from_at: sample.observed_at, last_seen_at: sample.observed_at, ended_at: null, active: true, sample_count: 1, sample_total: 1 };
      sessionsStore.put(session);
      samplesStore.put({ id: `${id}|0`, session_id: id, sequence: 0, ...sample });
      activeSessionExists = true;
    } else {
      const sequence = Number(open.sample_total || 0);
      const slot = sequence % MAX_SAMPLES_PER_SESSION;
      const session = { ...open, last_seen_at: sample.observed_at, sample_count: Math.min(MAX_SAMPLES_PER_SESSION, sequence + 1), sample_total: sequence + 1 };
      if (sequence >= MAX_SAMPLES_PER_SESSION) {
        const firstSequence = sequence - MAX_SAMPLES_PER_SESSION + 1;
        const firstSlot = firstSequence % MAX_SAMPLES_PER_SESSION;
        samplesStore.get(`${open.id}|${firstSlot}`).onsuccess = (sampleEvent) => {
          const firstRetained = sampleEvent.target.result;
          sessionsStore.put({ ...session, retained_from_at: firstRetained?.observed_at || open.retained_from_at || open.started_at });
        };
      } else {
        sessionsStore.put(session);
      }
      samplesStore.put({ id: `${open.id}|${slot}`, session_id: open.id, sequence, ...sample });
      activeSessionExists = true;
    }

    const retained = [...all].sort((a, b) => b.started_at.localeCompare(a.started_at));
    const keepIds = new Set(retained.slice(0, MAX_SESSIONS).map((session) => session.id));
    for (const stale of retained.slice(MAX_SESSIONS)) {
      sessionsStore.delete(stale.id);
      deleteSession(database, stale.id, samplesStore);
    }
  };
  await done;
}

export async function listPrintSessions() {
  const database = await openDatabase();
  const sessions = await requestResult(
    database.transaction(SESSIONS, "readonly").objectStore(SESSIONS).getAll(),
    "Could not read local print history.",
  );
  return (sessions || [])
    .sort((a, b) => b.started_at.localeCompare(a.started_at))
    .map(({ id, file_name, started_at, last_seen_at, ended_at, active, sample_count }) => ({
      id, file_name, started_at, last_seen_at, ended_at, active, sample_count,
    }));
}

export async function readPrintSession(id) {
  const database = await openDatabase();
  const transaction = database.transaction([SESSIONS, SAMPLES], "readonly");
  const done = transactionPromise(transaction);
  let result = null;
  transaction.objectStore(SESSIONS).get(id).onsuccess = (event) => {
    const session = event.target.result;
    if (!session) return;
    const request = transaction.objectStore(SAMPLES).index("session_id").getAll(id);
    request.onsuccess = () => {
      result = { ...session, samples: (request.result || []).sort((a, b) => a.sequence - b.sequence) };
    };
  };
  await done;
  return result;
}

export function progressAtOffset(session, offsetMs) {
  const samples = session?.samples || [];
  if (!samples.length) return null;
  const firstTime = Date.parse(session.retained_from_at || samples[0].observed_at);
  const targetTime = firstTime + Math.max(0, Number(offsetMs) || 0);
  let previous = samples[0];
  for (let index = 1; index < samples.length; index += 1) {
    const next = samples[index];
    const start = Date.parse(previous.observed_at), end = Date.parse(next.observed_at);
    if (targetTime <= end) {
      const ratio = end <= start ? 0 : Math.min(1, Math.max(0, (targetTime - start) / (end - start)));
      return previous.progress_percent + (next.progress_percent - previous.progress_percent) * ratio;
    }
    previous = next;
  }
  return samples.at(-1).progress_percent;
}
