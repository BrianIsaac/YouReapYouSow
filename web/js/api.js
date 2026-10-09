// Thin client for the room's HTTP API. Every refusal becomes an ApiError carrying the
// server's one-line message, which the screens show as is.

const BASE = "/api";

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function request(method, path, body) {
  const init = { method, headers: { Accept: "application/json" }, cache: "no-store" };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(BASE + path, init);
  } catch {
    throw new ApiError("OFFLINE", "The room's server cannot be reached. Retrying.", 0);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = data && data.error;
    if (err && err.message) throw new ApiError(err.code || "ERROR", err.message, res.status);
    let detail = `The server answered ${res.status}.`;
    if (data && typeof data.detail === "string") detail = data.detail;
    else if (data && Array.isArray(data.detail) && data.detail[0] && data.detail[0].msg) detail = `Check the form: ${data.detail[0].msg}.`;
    throw new ApiError("HTTP_" + res.status, detail, res.status);
  }
  return data;
}

// Which drop the room is looking at. Null means the older single-drop API at /api/state.
let dropId = null;

export function useDrop(id) {
  dropId = id;
}

// Every drop-scoped path is built here, so the scheme changes in one place.
function scoped(path) {
  return dropId ? `/drops/${encodeURIComponent(dropId)}${path}` : path;
}

export const api = {
  drops: () => request("GET", "/drops"),
  state: () => request("GET", dropId ? scoped("") : "/state"),
  join: (name) => request("POST", scoped("/join"), { name }),
  intake: (playerId, message) => request("POST", scoped("/intake"), { player_id: playerId, message }),
  editContract: (playerId, fields) => request("PUT", scoped("/contract"), { player_id: playerId, ...fields }),
  lockContract: (playerId) => request("POST", scoped("/contract/lock"), { player_id: playerId }),
  accept: (playerId) => request("POST", scoped("/accept"), { player_id: playerId }),
  decline: (playerId) => request("POST", scoped("/decline"), { player_id: playerId }),
  checkin: (form) => request("POST", scoped("/checkin"), form),
  dispute: (playerId, eventId, reason) =>
    request("POST", scoped("/dispute"), { player_id: playerId, event_id: eventId, reason }),
  reviewDispute: (eventId, reinstate) => request("POST", scoped("/dispute/review"), { event_id: eventId, reinstate }),
  finalize: () => request("POST", scoped("/finalize"), {}),
  retryPurchase: () => request("POST", scoped("/purchase/retry"), {}),
  reset: () => request("POST", scoped("/reset"), {}),
  ledger: (afterSeq = 0) => request("GET", scoped(`/ledger?after_seq=${afterSeq}`)),
  events: (playerId) =>
    request("GET", scoped(playerId ? `/events?player_id=${encodeURIComponent(playerId)}` : "/events")),
  evidenceUrl: (evidenceId) => `${BASE}${scoped(`/evidence/${encodeURIComponent(evidenceId)}`)}`,
};
