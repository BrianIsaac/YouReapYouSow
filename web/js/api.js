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

export const api = {
  state: () => request("GET", "/state"),
  join: (name) => request("POST", "/join", { name }),
  intake: (playerId, message) => request("POST", "/intake", { player_id: playerId, message }),
  editContract: (playerId, fields) => request("PUT", "/contract", { player_id: playerId, ...fields }),
  lockContract: (playerId) => request("POST", "/contract/lock", { player_id: playerId }),
  accept: (playerId) => request("POST", "/accept", { player_id: playerId }),
  decline: (playerId) => request("POST", "/decline", { player_id: playerId }),
  checkin: (form) => request("POST", "/checkin", form),
  dispute: (playerId, eventId, reason) =>
    request("POST", "/dispute", { player_id: playerId, event_id: eventId, reason }),
  reviewDispute: (eventId, reinstate) => request("POST", "/dispute/review", { event_id: eventId, reinstate }),
  finalize: () => request("POST", "/finalize", {}),
  reset: () => request("POST", "/reset", {}),
  ledger: (afterSeq = 0) => request("GET", `/ledger?after_seq=${afterSeq}`),
  events: (playerId) => request("GET", playerId ? `/events?player_id=${encodeURIComponent(playerId)}` : "/events"),
  evidenceUrl: (evidenceId) => `${BASE}/evidence/${encodeURIComponent(evidenceId)}`,
};
