import { timingSafeEqual } from "node:crypto";

export function configuredToken() {
  const token = process.env.SPARKDASH_TOKEN || process.env.DASHBOARD_TOKEN || "";
  return token.trim();
}

export function isLoopbackBind(host) {
  return host === "localhost" || host === "::1" || /^127\./.test(host);
}

export function requireRemoteAuth(bindHost) {
  return !isLoopbackBind(bindHost);
}

/** Unset/empty/"1" allow a tokenless remote bind. Set "0" to fail closed. */
export function allowOpenRemote() {
  const v = process.env.SPARKDASH_ALLOW_OPEN_REMOTE;
  if (v == null || v === "") return true;
  return v === "1";
}

/** GB10 overlay: SPARKDASH_READ_ONLY=1 rejects mutating HTTP except loopback LLM benches. */
export function readOnlyMode() {
  return process.env.SPARKDASH_READ_ONLY === "1";
}

const LOOPBACK_ORIGINS = new Set(["http://127.0.0.1:20080", "http://localhost:20080"]);

function requestPath(req) {
  return String(req.path || req.url || "").split("?")[0];
}

function loopbackOriginOk(req) {
  const origin = req.headers?.origin;
  if (origin == null || origin === "") return true;
  return LOOPBACK_ORIGINS.has(origin);
}

/** Decode/prefill start + cancel only. Shutdown/settings/showcase stay blocked. */
export function allowedReadOnlyMutation(req) {
  const method = (req.method || "GET").toUpperCase();
  const path = requestPath(req);
  if (method === "POST" && /^\/api\/sparks\/[^/]+\/llm\/bench$/.test(path)) return true;
  if (method === "DELETE" && /^\/api\/sparks\/[^/]+\/llm\/bench\/[^/]+$/.test(path)) return true;
  if (method === "POST" && /^\/api\/sparks\/[^/]+\/llm\/prefill-bench$/.test(path)) return true;
  if (method === "DELETE" && /^\/api\/sparks\/[^/]+\/llm\/prefill-bench\/[^/]+$/.test(path)) return true;
  return false;
}

function tokensEqual(left, right) {
  const a = Buffer.from(String(left));
  const b = Buffer.from(String(right));
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export function extractBearer(req) {
  const header = req.headers?.authorization || "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  if (match) return match[1].trim();
  const query = req.query?.token;
  return typeof query === "string" ? query.trim() : "";
}

export function authenticate(req) {
  const expected = configuredToken();
  if (!expected) return { ok: true, mode: "open-loopback" };
  const provided = extractBearer(req);
  if (!provided || !tokensEqual(provided, expected)) {
    return { ok: false, status: 401, error: "Authentication required" };
  }
  return { ok: true, mode: "bearer" };
}

export function createAuthMiddleware() {
  return function authMiddleware(req, res, next) {
    const method = (req.method || "GET").toUpperCase();
    const mutating = method !== "GET" && method !== "HEAD" && method !== "OPTIONS";
    if (mutating && readOnlyMode()) {
      if (allowedReadOnlyMutation(req) && loopbackOriginOk(req)) return next();
      return res.status(403).json({ error: "sparkDash is read-only; mutating routes are disabled" });
    }
    const remote = requireRemoteAuth(process.env.BIND_HOST || "127.0.0.1");
    if (!mutating && !remote && !configuredToken()) return next();
    if (!mutating && !remote) return next();
    if (!mutating && remote && !configuredToken()) {
      if (allowOpenRemote()) return next();
      return res.status(403).json({ error: "Remote access requires SPARKDASH_TOKEN" });
    }
    const result = authenticate(req);
    if (!result.ok) return res.status(result.status).json({ error: result.error });
    next();
  };
}

export function authorizeUpgrade(req) {
  if (readOnlyMode()) return true;
  const remote = requireRemoteAuth(process.env.BIND_HOST || "127.0.0.1");
  if (!remote && !configuredToken()) return true;
  return authenticate(req).ok;
}
