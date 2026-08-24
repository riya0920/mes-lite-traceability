"""A terminal that writes.

The README said the terminal was read-only because wiring it to `execution.py`
"needs a server and a session model, and the buttons are disabled rather than
faked". Both halves of that are now built, and the session model is the half that
mattered: every rule in `execution.py` that protects anything is a rule about
*who* — is this operator certified, who approved the deviation, whose name goes
on the audit row. A terminal with no identity can only write as "somebody", and a
Part-11-shaped signature over an anonymous action is worse than no signature,
because it looks like accountability.

Three decisions worth stating:

  THE SERVER ENFORCES NOTHING. Every write goes through `execution.py`, and the
  handler's only job is to turn an `ExecutionError` into a 409 with the reason
  text intact. A UI that re-implements a check is a second place for the rule to
  live, and the two drift -- the copy in the UI is the one that gets relaxed,
  because that is where the pressure is.

  A DOUBLE-CLICK IS NOT A SECOND COMPLETION. `ux_one_complete_per_pass` already
  guaranteed that at the storage layer; what was missing was a handler that
  reports the resulting IntegrityError as "already completed" rather than a 500.
  The guarantee was there and the user experience of hitting it was a stack trace.

  BLOCKED BUTTONS CARRY THEIR REASON. The rendered terminal already argued that
  a greyed-out button with no explanation gets worked around within a shift. The
  writing version keeps that promise: a refusal returns the message
  `execution.py` raised, which names the certification and says how to override
  it with an authorised deviation.

Standard library only -- `http.server` and `sqlite3`. This is a demonstration of
the write path, not a deployment: see the limits at the bottom of the module.
"""
from __future__ import annotations

import http.server
import json
import secrets
import socket
import sqlite3
import threading
import urllib.parse

import execution as ex
import model


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

class SessionStore:
    """Token -> operator. Deliberately thin, and deliberately not a login.

    There is no password here and no identity provider, which is stated rather
    than implied: this maps a badge scan to an operator id the way a shop-floor
    terminal does, and it is the part that would be replaced first by anything
    real. What it does provide is the property the write path actually needs --
    every mutating request carries an operator id that the database recognises,
    and requests without one are refused rather than defaulted.
    """

    def __init__(self, app: "TerminalApp"):
        self._app = app
        self._by_token: dict[str, str] = {}
        self._lock = threading.Lock()

    def login(self, op_id: str) -> str:
        row = self._app.conn.execute(
            "SELECT op_id FROM operator WHERE op_id=?", (op_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown operator {op_id!r}")
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._by_token[token] = op_id
        return token

    def operator(self, token: str | None) -> str:
        with self._lock:
            op = self._by_token.get(token or "")
        if op is None:
            raise PermissionError("no session; scan a badge first")
        return op

    def logout(self, token: str) -> None:
        with self._lock:
            self._by_token.pop(token, None)


# ---------------------------------------------------------------------------
# the write path
# ---------------------------------------------------------------------------

class TerminalApp:
    """Routes requests to `execution.py`. Holds no business rules of its own."""

    def __init__(self, db_path, key: bytes = b"demo-key"):
        self.db_path = str(db_path)
        self.key = key
        # SQLite connections are bound to the thread that opened them, and the
        # server hands every request to a new one. A thread-local connection is
        # the fix rather than check_same_thread=False, which would silently
        # share a connection across threads and leave the transaction boundary
        # -- the thing this project spent a whole section getting right -- to
        # whichever request happened to call commit() last.
        self._tl = threading.local()
        self.sessions = SessionStore(self)
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._tl, "conn", None)
        if c is None:
            c = self._tl.conn = model.connect(self.db_path)
        return c

    # -- reads ------------------------------------------------------------
    def dispatch_list(self, wc_id: str | None = None) -> list:
        # The SKU lives on the work order, not the unit -- a unit knows which
        # order it belongs to and the order knows what is being built. Joining
        # through it is the difference between a dispatch list and a guess.
        q = ("SELECT u.unit_id, w.sku, u.wo_id, o.seq, o.name, o.wc_id, "
             "       o.cert_required, o.std_run_s "
             "FROM unit u "
             "JOIN work_order w ON w.wo_id = u.wo_id "
             "JOIN operation o ON o.sku = w.sku "
             "WHERE u.status = 'IN_PROCESS' "
             "  AND NOT EXISTS (SELECT 1 FROM op_record r WHERE r.unit_id=u.unit_id "
             "                  AND r.seq=o.seq AND r.action='COMPLETE') "
             "ORDER BY u.unit_id, o.seq LIMIT 500")
        rows = [dict(r) for r in self.conn.execute(q)]
        if wc_id:
            rows = [r for r in rows if r["wc_id"] == wc_id]
        return rows

    def unit_status(self, unit_id: str) -> dict:
        u = self.conn.execute(
            "SELECT u.unit_id, u.wo_id, u.status, w.sku FROM unit u "
            "JOIN work_order w ON w.wo_id = u.wo_id WHERE u.unit_id=?",
            (unit_id,)).fetchone()
        if u is None:
            raise KeyError(f"unknown unit {unit_id!r}")
        ops = [dict(r) for r in self.conn.execute(
            "SELECT seq, name, wc_id, cert_required FROM operation "
            "WHERE sku=? ORDER BY seq", (u["sku"],))]
        done = {r["seq"] for r in self.conn.execute(
            "SELECT seq FROM op_record WHERE unit_id=? AND action='COMPLETE'",
            (unit_id,))}
        for o in ops:
            o["complete"] = o["seq"] in done
        nxt = next((o for o in ops if not o["complete"]), None)
        return {"unit_id": unit_id, "sku": u["sku"], "wo_id": u["wo_id"],
                "status": u["status"], "operations": ops,
                "next_seq": nxt["seq"] if nxt else None}

    def can_start(self, token, unit_id: str, seq: int) -> dict:
        """A dry run of the same checks the write path uses.

        Same functions, not a copy of them -- which is the only way the greyed-out
        button and the refusal can be guaranteed to agree.
        """
        op_id = self.sessions.operator(token)
        sku = ex._sku_of(self.conn, unit_id)
        try:
            ex.check_precedence(self.conn, unit_id, seq)
            ex.check_certification(self.conn, op_id, sku, seq)
        except ex.ExecutionError as e:
            return {"allowed": False, "reason": str(e),
                    "kind": type(e).__name__}
        return {"allowed": True, "reason": None, "kind": None}

    # -- writes -----------------------------------------------------------
    def _write(self, fn, *a, **kw):
        with self._lock:
            conn = self.conn
            try:
                out = fn(conn, *a, **kw)
                conn.commit()
                return {"ok": True, "result": out}
            except ex.ExecutionError as e:
                conn.rollback()
                return {"ok": False, "error": str(e), "kind": type(e).__name__}
            except sqlite3.IntegrityError as e:
                conn.rollback()
                # The UNIQUE index firing is the double-click case, and it is a
                # normal outcome of a normal user action -- not a server fault.
                msg = str(e)
                if "ux_one_complete_per_pass" in msg or "UNIQUE" in msg.upper():
                    return {"ok": False, "kind": "AlreadyRecorded",
                            "error": "this operation is already complete for "
                                     "this pass; a second completion would break "
                                     "the one-record-per-pass guarantee"}
                return {"ok": False, "kind": "IntegrityError", "error": msg}

    def start(self, token, unit_id, seq, wc_id, deviation_ref=None):
        op_id = self.sessions.operator(token)
        return self._write(ex.start_operation, unit_id, int(seq), op_id, wc_id,
                           deviation_ref=deviation_ref)

    def issue(self, token, unit_id, seq, lot_id, qty):
        op_id = self.sessions.operator(token)
        return self._write(ex.issue_material, unit_id, int(seq), lot_id,
                           float(qty), op_id)

    def complete(self, token, unit_id, seq, wc_id):
        op_id = self.sessions.operator(token)
        return self._write(ex.complete_operation, unit_id, int(seq), op_id, wc_id)

    def scrap(self, token, unit_id, seq, wc_id, reason):
        op_id = self.sessions.operator(token)
        return self._write(ex.scrap, unit_id, int(seq), op_id, wc_id, reason)

    def ncr(self, token, unit_id, seq, defect):
        self.sessions.operator(token)          # identity required to raise one
        return self._write(ex.raise_ncr, unit_id, int(seq), defect)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

ROUTES = {
    "/api/start": ("start", ("unit_id", "seq", "wc_id")),
    "/api/issue": ("issue", ("unit_id", "seq", "lot_id", "qty")),
    "/api/complete": ("complete", ("unit_id", "seq", "wc_id")),
    "/api/scrap": ("scrap", ("unit_id", "seq", "wc_id", "reason")),
    "/api/ncr": ("ncr", ("unit_id", "seq", "defect")),
}


def _handler(app: TerminalApp, page_html: str):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):                      # quiet under pytest
            pass

        def _send(self, code: int, payload, ctype="application/json"):
            body = (json.dumps(payload).encode() if ctype == "application/json"
                    else payload.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # One request per connection. Keep-alive buys nothing for a terminal
            # that posts a handful of times a shift, and it makes a half-read
            # response the client's problem on the NEXT request rather than this
            # one -- which is a confusing failure to debug for no benefit.
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _token(self):
            return self.headers.get("X-Session")

        def _guard(self, fn):
            """Any unhandled exception becomes a 500 WITH A BODY.

            The default is to let it escape, which closes the socket and reaches
            the client as "remote end closed connection without response" -- so
            every bug in a handler presents as a network fault, which is the
            wrong place to look. It cost two rounds of debugging on this module
            before it was added.
            """
            try:
                return fn()
            except PermissionError as e:
                return self._send(401, {"ok": False, "error": str(e)})
            except KeyError as e:
                return self._send(404, {"ok": False, "error": str(e.args[0])})
            except (ValueError, TypeError) as e:
                return self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:                       # noqa: BLE001
                return self._send(500, {"ok": False, "kind": type(e).__name__,
                                        "error": str(e)})

        def do_GET(self):
            return self._guard(self._get)

        def do_POST(self):
            return self._guard(self._post)

        def _get(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._send(200, page_html, "text/html; charset=utf-8")
            if u.path == "/api/dispatch":
                return self._send(200, app.dispatch_list(
                    q.get("wc_id", [None])[0]))
            if u.path == "/api/unit":
                return self._send(200, app.unit_status(q["unit_id"][0]))
            if u.path == "/api/can_start":
                return self._send(200, app.can_start(
                    self._token(), q["unit_id"][0], int(q["seq"][0])))
            return self._send(404, {"ok": False, "error": "no such route"})

        def _post(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "error": "bad JSON"})
            path = urllib.parse.urlparse(self.path).path

            if path == "/api/login":
                return self._send(200, {"ok": True, "token": app.sessions.login(
                    body.get("op_id", ""))})

            route = ROUTES.get(path)
            if route is None:
                return self._send(404, {"ok": False, "error": "no such route"})
            name, fields = route
            missing = [f for f in fields if f not in body]
            if missing:
                return self._send(400, {"ok": False,
                                        "error": f"missing {', '.join(missing)}"})
            args = [body[f] for f in fields]
            if name == "start" and "deviation_ref" in body:
                args.append(body["deviation_ref"])
            out = getattr(app, name)(self._token(), *args)
            # A refused write is a 409, not a 500. It is the system working.
            return self._send(200 if out.get("ok") else 409, out)

    return H


def serve(db_path, page_html: str = "<h1>MES terminal</h1>", port: int = 0):
    """Start the terminal. `port=0` takes a free one, which the tests rely on."""
    app = TerminalApp(db_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                          _handler(app, page_html))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return {"app": app, "server": srv, "thread": t,
            "port": srv.server_address[1],
            "url": f"http://127.0.0.1:{srv.server_address[1]}"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# WHAT THIS IS NOT
# ---------------------------------------------------------------------------
LIMITS = [
    "No authentication. `login` takes an operator id and trusts it; there is no "
    "password, no identity provider, no lockout and no session expiry. It is a "
    "badge scan without the badge.",
    "No transport security. Plain HTTP on the loopback interface.",
    "One process, one SQLite connection, one lock. It serialises every write, "
    "which is correct and does not scale; the concurrency work in "
    "`scheduling.race_two_operators` is about the database boundary, and this "
    "server sits above it rather than exercising it.",
    "No CSRF protection and no origin checking, which matters the moment this "
    "is served anywhere a browser can reach it from another page.",
    "The page it serves is the rendered terminal with live buttons; it is not a "
    "front end anybody designed, and it re-fetches rather than updating in place.",
]
