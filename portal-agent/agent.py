#!/usr/bin/env python3
"""Media butler agent — tool-calling backend for the portal chatbot.

Holds the LLM-gateway key and the Jellyseerr key SERVER-side and exposes a
tiny HTTP API that the portal page calls same-origin at /agent/ (see
portal-nginx.conf). The browser never sees a credential; tailnet membership
is the access boundary.

  GET  /health                 -> {"ok": true}
  GET  /models                 -> proxies <LLM>/v1/models
  POST /chat {model,messages}  -> tool-calling loop over Jellyseerr,
                                  streamed back as OpenAI-style SSE

Works with any OpenAI-compatible endpoint (OpenAI, LiteLLM, one-api,
cli-proxy-api, ...). Stdlib only — no dependencies.
"""
import os, json, re, urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LLM_BASE = os.environ.get("LLM_BASE_URL", "").rstrip("/")
if LLM_BASE.endswith("/v1"):
    LLM_BASE = LLM_BASE[:-3].rstrip("/")   # tolerate the OpenAI-SDK "…/v1" convention
LLM_KEY = os.environ.get("LLM_API_KEY", "")
JS_BASE = os.environ.get("JELLYSEERR_BASE", "http://jellyseerr-ts:5055").rstrip("/")
JS_KEY = os.environ.get("JELLYSEERR_KEY", "")
# Empty = no CORS headers at all (same-origin only — the portal reaches us via
# the nginx /agent/ proxy, so no cross-origin access is ever needed). Set only
# if a genuinely different origin must call the agent; "*" is never emitted.
ORIGIN = os.environ.get("ALLOW_ORIGIN", "")
# Loopback-only: nginx in the shared portal-ts netns proxies /agent/ here.
# Set HOST=0.0.0.0 only for debugging.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
MAX_STEPS = 5

if not LLM_BASE or not LLM_KEY:
    raise SystemExit("LLM_BASE_URL and LLM_API_KEY are required (any OpenAI-compatible endpoint)")
if not JS_KEY:
    raise SystemExit("JELLYSEERR_KEY is required (Jellyseerr -> Settings -> General -> API Key)")

SYSTEM = """You are the "media butler", an assistant embedded in a private, \
tailnet-only media-center portal. Reply in the user's language, concise and \
conversational; keep technical terms in English; use lists when they help; \
refer to services by name (Jellyseerr, Sonarr, Radarr, Jellyfin, ...) — the \
portal already links them.

You have tools that REALLY execute (not just instructions):
- search_media: search movies/TV in Jellyseerr (title/year/type/tmdbId/library status).
- request_media: file a request for a tmdbId — this triggers the whole
  pipeline: Sonarr/Radarr -> Prowlarr indexers -> debrid download -> library.
- list_requests: recent requests and their status.

Rules:
- To request something: search first. If several plausible matches (year or
  type ambiguous), list candidates and ask; if unambiguous, request directly
  and report the outcome.
- If it is already available in the library, say so instead of re-requesting.
- Be concrete: on success say the title/year, that the pipeline will fetch it
  automatically, and to check the player in a few minutes. On failure, say why.
- Everything beyond these three tools: give steps and service names only.
- When unsure, ask — never guess a tmdbId.

Pipeline topology (background): Jellyseerr request -> Sonarr (TV) / Radarr
(movies) -> Prowlarr indexers -> debrid bridge -> zurg -> rclone FUSE
/mnt/zurg -> symlink library -> Jellyfin/Plex. Subtitles via Bazarr."""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_media",
        "description": "Search movies/TV in Jellyseerr. Returns candidates with tmdbId, type, year, library status. Always search before requesting.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Title only, any language. Do NOT append a year or season — use the year to pick from results."}
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "request_media",
        "description": "File a Jellyseerr request for a tmdbId, triggering the download pipeline. Only after the exact item is confirmed.",
        "parameters": {"type": "object", "properties": {
            "tmdb_id": {"type": "integer", "description": "tmdbId from search_media"},
            "media_type": {"type": "string", "enum": ["movie", "tv"]},
            "title": {"type": "string", "description": "Title for the report"}
        }, "required": ["tmdb_id", "media_type"]}}},
    {"type": "function", "function": {
        "name": "list_requests",
        "description": "List recent requests and their status (pending/processing/available). title may be null if lookup failed — then present the tmdb_id honestly instead of guessing.",
        "parameters": {"type": "object", "properties": {}}}},
]

STATUS = {1: "unknown", 2: "pending", 3: "processing", 4: "partially available", 5: "available"}


def _req(method, url, headers=None, data=None, timeout=40):
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except (OSError, ValueError) as e:
        # connection refused/DNS/timeout, or a 200 with a non-JSON body
        # (e.g. an HTML page from a misconfigured gateway)
        return 502, {"error": "upstream request failed: %s" % e}


# ---------------- Jellyseerr tools ----------------
def js_search(query):
    def _run(q):
        u = JS_BASE + "/api/v1/search?query=" + urllib.parse.quote(q) + "&page=1"
        return _req("GET", u, {"X-Api-Key": JS_KEY})

    st, d = _run(query)
    if st == 200 and d and not (d.get("results") or []):
        # Jellyseerr doesn't parse an appended year ("Dune 2021" -> 0 hits);
        # retry once with a trailing standalone year stripped. Never strip
        # eagerly — years can be part of the title ("Blade Runner 2049").
        q2 = re.sub(r"\s*\(?\b(19|20)\d{2}\b\)?\s*$", "", query).strip()
        if q2 and q2 != query:
            st, d = _run(q2)
    if st != 200 or not d:
        return {"error": "search failed (%s)" % st}
    out = []
    for r in (d.get("results") or []):
        if r.get("mediaType") not in ("movie", "tv"):
            continue
        dt = r.get("releaseDate") or r.get("firstAirDate") or ""
        out.append({
            "tmdb_id": r.get("id"),
            "media_type": r.get("mediaType"),
            "title": r.get("title") or r.get("name"),
            "year": dt[:4],
            "overview": (r.get("overview") or "")[:120],
            "library_status": STATUS.get((r.get("mediaInfo") or {}).get("status"), "not in library"),
        })
    return {"results": out[:8]}


def js_request(tmdb_id, media_type, title=None):
    payload = {"mediaType": media_type, "mediaId": int(tmdb_id)}
    if media_type == "tv":
        payload["seasons"] = "all"
    st, d = _req("POST", JS_BASE + "/api/v1/request", {"X-Api-Key": JS_KEY}, payload)
    if st in (200, 201):
        return {"ok": True, "title": title, "request_id": (d or {}).get("id"),
                "message": "request filed; the pipeline is fetching it"}
    msg = (d or {}).get("message") if isinstance(d, dict) else None
    if st == 409 or (msg and "exist" in str(msg).lower()):
        return {"ok": True, "already": True, "message": "already requested or already in the library"}
    return {"ok": False, "status": st, "message": msg or "request failed"}


def js_list():
    u = JS_BASE + "/api/v1/request?take=15&skip=0&filter=all&sort=added"
    st, d = _req("GET", u, {"X-Api-Key": JS_KEY})
    if st != 200 or not d:
        return {"error": "list failed (%s)" % st}
    out = []
    for r in (d.get("results") or []):
        media = r.get("media") or {}
        mtype = r.get("type")
        tmdb = media.get("tmdbId")
        # The request.media entity carries no title — resolve it with one
        # details call per row, tolerating failures (title stays null).
        title = None
        if tmdb and mtype in ("movie", "tv"):
            ep = "/api/v1/movie/%d" % tmdb if mtype == "movie" else "/api/v1/tv/%d" % tmdb
            dst, dd = _req("GET", JS_BASE + ep, {"X-Api-Key": JS_KEY}, timeout=10)
            if dst == 200 and isinstance(dd, dict):
                title = dd.get("title") or dd.get("name")
        out.append({
            "type": mtype,
            "tmdb_id": tmdb,
            "title": title,
            "library_status": STATUS.get(media.get("status"), "unknown"),
            "requested_at": (r.get("createdAt") or "")[:10],
        })
    return {"requests": out}


def run_tool(name, args):
    try:
        if name == "search_media":
            return js_search(args.get("query", ""))
        if name == "request_media":
            return js_request(args.get("tmdb_id"), args.get("media_type"), args.get("title"))
        if name == "list_requests":
            return js_list()
    except Exception as e:
        return {"error": str(e)}
    return {"error": "unknown tool " + name}


# ---------------- LLM (OpenAI-compatible) ----------------
def llm_chat(model, messages, use_tools=True):
    body = {"model": model, "messages": messages, "stream": False}
    if use_tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"
    st, d = _req("POST", LLM_BASE + "/v1/chat/completions",
                 {"Authorization": "Bearer " + LLM_KEY}, body, timeout=120)
    return st, d


def agent_turn(model, client_messages):
    """Run the tool loop; yield (kind, text) progress + final."""
    messages = [{"role": "system", "content": SYSTEM}] + client_messages
    use_tools = True
    for _ in range(MAX_STEPS):
        st, d = llm_chat(model, messages, use_tools)
        if st != 200 or not d:
            # Fall back to no-tools ONLY for a tools-param rejection on the
            # first call (before any tool results are in the history) — a
            # transient 429/5xx must not silently strip the tools, or the
            # model may claim a request was filed without calling Jellyseerr.
            tool_results_present = any(m.get("role") == "tool" for m in messages)
            if use_tools and st in (400, 404, 422) and not tool_results_present:
                use_tools = False
                continue
            yield ("final", "⚠️ LLM call failed (%s). Try another model or retry later." % st)
            return
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            # Echo back only the OpenAI-spec fields — provider extras like
            # reasoning_content make strict gateways reject the next call.
            clean = {"role": "assistant", "content": msg.get("content"),
                     "tool_calls": [{"id": tc.get("id"), "type": "function",
                                     "function": {"name": (tc.get("function") or {}).get("name"),
                                                  "arguments": (tc.get("function") or {}).get("arguments") or "{}"}}
                                    for tc in tool_calls]}
            messages.append(clean)
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if name == "search_media":
                    yield ("status", "🔎 searching \"%s\"…" % args.get("query", ""))
                elif name == "request_media":
                    yield ("status", "🎬 requesting %s…" % (args.get("title") or args.get("tmdb_id")))
                elif name == "list_requests":
                    yield ("status", "📋 checking requests…")
                result = run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "name": name, "content": json.dumps(result, ensure_ascii=False)})
            continue
        yield ("final", msg.get("content") or "")
        return
    yield ("final", "(tool-step limit reached — stopping here.)")


# ---------------- HTTP server ----------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        # No CORS headers unless an operator explicitly opted in to a specific
        # origin ("*" is never honored): /agent/ is same-origin behind nginx,
        # and emitting a permissive ACAO would let foreign pages read replies.
        if ORIGIN and ORIGIN != "*":
            self.send_header("Access-Control-Allow-Origin", ORIGIN)
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def log_message(self, *a):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(200, {"ok": True})
        elif self.path.startswith("/models"):
            st, d = _req("GET", LLM_BASE + "/v1/models",
                         {"Authorization": "Bearer " + LLM_KEY})
            self._json(st, d if d is not None else {"data": []})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/chat"):
            self._json(404, {"error": "not found"})
            return
        # Require JSON: rejects cross-origin "simple" (text/plain) requests, so
        # any cross-origin call needs a CORS preflight — which _cors() denies
        # by default. Tailnet membership alone is not CSRF protection.
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct != "application/json":
            self._json(415, {"error": "content-type must be application/json"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 256 * 1024:
                self._json(413, {"error": "body too large"})
                return
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        model = payload.get("model")
        messages = payload.get("messages") or []
        if not model or not messages:
            self._json(400, {"error": "model and messages required"})
            return
        # SSE stream, OpenAI-compatible delta chunks
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def sse(text):
            chunk = {"choices": [{"delta": {"content": text}}]}
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            saw_status = False
            for kind, text in agent_turn(model, messages):
                if kind == "status":
                    saw_status = True
                    sse(text + "\n")
                else:
                    if saw_status and text:
                        sse("\n")
                    for piece in re.findall(r".{1,48}", text, re.S) or [text]:
                        sse(piece)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    print("agent on %s:%d  llm=%s  jellyseerr=%s" % (HOST, PORT, LLM_BASE, JS_BASE), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
