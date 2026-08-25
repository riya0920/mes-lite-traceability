"""Pass 5: authentication and TLS on the terminal.

The tests that matter are the refusals and the things that must NOT differ:
a wrong PIN and an unenrolled badge have to be indistinguishable, and the
comparison has to be constant-time. A test suite that only checks the happy path
would pass against a server that accepts anything.
"""
from __future__ import annotations

import json
import pathlib
import ssl
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import auth as AU              # noqa: E402
import generate                # noqa: E402
import model                   # noqa: E402
import server as SV            # noqa: E402

PIN = "8417"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    db = tmp_path_factory.mktemp("auth") / "a.db"
    conn = model.create(db)
    generate.seed_definitions(conn)
    generate.seed_lots(conn)
    generate.run_week(conn, np.random.default_rng(0))
    conn.close()
    return db


@pytest.fixture
def creds(seeded, tmp_path):
    import shutil
    db = tmp_path / "c.db"
    shutil.copy(seeded, db)
    # The factory must return the SAME connection each call, the way the
    # server's thread-local property does. A factory that opens a fresh
    # connection every time deadlocks against its own uncommitted writes --
    # which is what the first version of this fixture did.
    held = {}

    def factory():
        if "c" not in held:
            held["c"] = model.connect(db)
        return held["c"]

    return AU.Credentials(factory)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

def test_a_pin_is_never_stored(creds):
    creds.enrol("OP-01", PIN)
    row = creds.conn.execute(
        "SELECT salt, hash FROM credential WHERE op_id='OP-01'").fetchone()
    blob = bytes(row["salt"]) + bytes(row["hash"])
    assert PIN.encode() not in blob
    assert creds.verify("OP-01", PIN)["ok"] is True


def test_two_operators_with_the_same_pin_get_different_hashes(creds):
    """Which is what the salt is for. Without it, one rainbow table does the
    whole plant, and equal hashes tell you two people share a PIN."""
    creds.enrol("OP-01", PIN)
    creds.enrol("OP-02", PIN)
    rows = {r["op_id"]: bytes(r["hash"]) for r in creds.conn.execute(
        "SELECT op_id, hash FROM credential")}
    assert rows["OP-01"] != rows["OP-02"]


def test_a_wrong_pin_is_refused(creds):
    creds.enrol("OP-01", PIN)
    r = creds.verify("OP-01", "0000")
    assert r["ok"] is False and r["reason"] == "bad_pin"


def test_an_unenrolled_badge_still_costs_a_derivation(creds):
    """Otherwise 'no such credential' returns measurably faster than 'wrong
    PIN', and the timing tells an attacker which badge ids are real."""
    creds.enrol("OP-01", PIN)
    t0 = time.perf_counter()
    creds.verify("OP-01", "0000")
    wrong_pin = time.perf_counter() - t0
    t0 = time.perf_counter()
    creds.verify("OP-99", "0000")
    no_cred = time.perf_counter() - t0
    # within a factor of three either way -- generous, because CI timing is
    # noisy, and still enough to catch an early return
    assert 0.33 < (no_cred / max(wrong_pin, 1e-9)) < 3.0, (no_cred, wrong_pin)


def test_the_comparison_is_constant_time(creds):
    src = (ROOT / "src" / "auth.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src
    assert "if want == got" not in src


def test_a_short_pin_is_refused_at_enrolment(creds):
    with pytest.raises(ValueError, match="at least"):
        creds.enrol("OP-01", "12")


def test_an_unknown_operator_cannot_be_enrolled(creds):
    with pytest.raises(KeyError):
        creds.enrol("OP-99", PIN)


def test_lockout_after_repeated_failures(creds):
    creds.enrol("OP-01", PIN)
    for _ in range(AU.MAX_FAILURES):
        creds.verify("OP-01", "0000")
    assert creds.locked_for("OP-01") > 0
    # and the RIGHT pin is refused too, which is the point of a lockout
    r = creds.verify("OP-01", PIN)
    assert r["ok"] is False and r["reason"] == "locked_out"


def test_a_success_clears_the_failure_count(creds):
    creds.enrol("OP-01", PIN)
    for _ in range(AU.MAX_FAILURES - 1):
        creds.verify("OP-01", "0000")
    assert creds.verify("OP-01", PIN)["ok"] is True
    assert creds.locked_for("OP-01") == 0.0


def test_every_attempt_leaves_an_audit_row(creds):
    creds.enrol("OP-01", PIN)
    creds.verify("OP-01", PIN)
    creds.verify("OP-01", "0000")
    creds.verify("OP-99", "0000")
    events = [e["event"] for e in creds.events()]
    for want in ("ENROL", "AUTH_OK", "BAD_PIN", "NO_CREDENTIAL"):
        assert want in events, (want, events)


# ---------------------------------------------------------------------------
# over HTTP
# ---------------------------------------------------------------------------

def _call(url, path, body=None, tok=None, ctx=None):
    hdr = {"Content-Type": "application/json"}
    if tok:
        hdr["X-Session"] = tok
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, headers=hdr)
    try:
        with urllib.request.urlopen(req, context=ctx) as f:
            return f.status, json.load(f)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.fixture
def live(seeded, tmp_path):
    import shutil
    db = tmp_path / "s.db"
    shutil.copy(seeded, db)
    h = SV.serve(db)
    h["app"].credentials.enrol("OP-01", PIN)
    yield h
    h["server"].shutdown()


def test_a_login_without_a_pin_is_refused(live):
    c, b = _call(live["url"], "/api/login", {"op_id": "OP-01"})
    assert c == 401 and "PIN" in b["error"]


def test_a_wrong_pin_and_an_unenrolled_badge_give_the_same_message(live):
    """Telling them apart tells an attacker which badge ids are enrolled."""
    _, wrong = _call(live["url"], "/api/login",
                     {"op_id": "OP-01", "pin": "0000"})
    _, none_ = _call(live["url"], "/api/login",
                     {"op_id": "OP-02", "pin": "0000"})
    assert wrong["error"] == none_["error"]


def test_a_correct_pin_returns_a_session(live):
    c, b = _call(live["url"], "/api/login", {"op_id": "OP-01", "pin": PIN})
    assert c == 200 and len(b["token"]) >= 32


def test_the_write_path_still_requires_the_session(live):
    c, _ = _call(live["url"], "/api/complete",
                 {"unit_id": "x", "seq": 10, "wc_id": "WC-CUT"})
    assert c == 401


def test_a_session_expires(seeded, tmp_path):
    import shutil
    db = tmp_path / "e.db"
    shutil.copy(seeded, db)
    h = SV.serve(db, session_ttl_s=0.4)
    try:
        h["app"].credentials.enrol("OP-01", PIN)
        tok = _call(h["url"], "/api/login",
                    {"op_id": "OP-01", "pin": PIN})[1]["token"]
        assert h["app"].sessions.operator(tok) == "OP-01"
        time.sleep(0.6)
        with pytest.raises(PermissionError):
            h["app"].sessions.operator(tok)
    finally:
        h["server"].shutdown()


def test_require_pin_is_a_property_of_the_app_not_of_the_call(seeded, tmp_path):
    """A server that accepts an unauthenticated login whenever a PIN happens to
    be omitted has no authentication at all."""
    import shutil
    db = tmp_path / "n.db"
    shutil.copy(seeded, db)
    h = SV.serve(db, require_pin=False)
    try:
        c, b = _call(h["url"], "/api/login", {"op_id": "OP-01"})
        assert c == 200 and "token" in b
    finally:
        h["server"].shutdown()
    assert SV.TerminalApp(db).require_pin is True, "the default must be secure"


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

@pytest.fixture
def tls(seeded, tmp_path):
    import shutil
    db = tmp_path / "t.db"
    shutil.copy(seeded, db)
    h = SV.serve(db, tls_dir=tmp_path / "pki")
    h["app"].credentials.enrol("OP-01", PIN)
    yield h
    h["server"].shutdown()


def test_the_terminal_serves_over_tls(tls):
    assert tls["scheme"] == "https"
    ctx = AU.client_context(tls["pki"])
    url = f"https://localhost:{tls['port']}"
    c, b = _call(url, "/api/login", {"op_id": "OP-01", "pin": PIN}, ctx=ctx)
    assert c == 200 and "token" in b


def test_plain_http_against_the_tls_port_fails(tls):
    with pytest.raises(Exception):
        _call(f"http://127.0.0.1:{tls['port']}", "/api/dispatch")


def test_a_client_that_does_not_trust_the_certificate_is_refused(tls):
    """The self-signed certificate has to be handed over out of band. A client
    that skips that must fail, or the trust decision is not being made."""
    ctx = ssl.create_default_context()      # system trust store only
    with pytest.raises(Exception):
        _call(f"https://localhost:{tls['port']}", "/api/dispatch", ctx=ctx)


def test_the_contexts_set_a_version_floor():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        pki = AU.self_signed(d)
        s = AU.server_context(pki)
        c = AU.client_context(pki)
    assert s.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert c.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert c.check_hostname is True and c.verify_mode == ssl.CERT_REQUIRED


def test_no_key_material_is_checked_in():
    tracked = [p for p in ROOT.rglob("*")
               if p.suffix in (".key", ".pem", ".pfx", ".p12")
               and ".git" not in p.parts]
    assert tracked == [], tracked


def test_this_does_not_import_se1():
    """SE-2's own README argues that a cross-project import is what makes two
    systems impossible to deploy separately. The pattern transfers; the code
    does not."""
    src = (ROOT / "src" / "auth.py").read_text(encoding="utf-8")
    assert "se1" not in src.lower().replace("se1's", "")
    assert "import mtls" not in src


def test_the_limits_still_say_it_is_not_part_11():
    joined = " ".join(AU.LIMITS) + " " + " ".join(SV.LIMITS)
    assert "Part 11" in joined
    assert "not compliance" in joined
