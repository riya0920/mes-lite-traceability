"""Authentication and TLS for the terminal, and the Part-11 gap that remains.

The README listed three things about the server: *no authentication, no TLS, no
CSRF protection*. The first two are here. The third is not, and why is at the
bottom.

WHY THIS IS NOT AN IMPORT FROM SE-1. SE-1 has a working mutual-TLS module and it
would take one line to import it. This project's own README argues against
exactly that: *a cross-project import is the thing that makes two systems
impossible to deploy separately*. The pattern transfers; the code does not. What
is shared is the standard library.

WHAT A SHOP-FLOOR CREDENTIAL IS. Not a password typed at a terminal. Operators
work in gloves, the terminal is on the line, and anything requiring a keyboard
gets written on a label and stuck to the monitor. The realistic factors are a
badge (something you have) and a PIN (something you know), and the badge alone is
what most plants actually run. So:

  * a badge id is an identifier, not a secret, and is treated as one
  * a PIN is a secret, stored only as a salted hash, and is what turns the badge
    scan into an authentication rather than a claim
  * lockout after repeated failures, because a four-digit PIN with unlimited
    attempts is a four-digit PIN that anybody can have in ten minutes
  * sessions expire, because a terminal on a line is never logged out by the
    person who logged in

PBKDF2-HMAC-SHA256 rather than a bare hash: a four-digit PIN has ten thousand
possibilities, and the only thing standing between a leaked hash table and every
operator's PIN is how long each guess takes. 200k iterations is roughly 100 ms
here, which is invisible at a badge scan and expensive at ten thousand guesses
per operator.

WHAT THIS STILL IS NOT, and it is the honest heart of it: **none of this makes
the project 21 CFR Part 11 compliant.** Part 11 wants an identity lifecycle -- an
authority who issues and revokes credentials, periodic access review, a password
policy, and a validation package for the software that enforces them. This is the
mechanism a compliance programme would sit on top of, and a mechanism without a
programme is not compliance. That distinction is the whole reason the README says
"Part-11 shaped".
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import pathlib
import secrets
import socket
import ssl
import sqlite3
import threading
import time

ITERATIONS = 200_000
SALT_BYTES = 16
PIN_MIN_LEN = 4

# A badge scan should not take a noticeable moment, and a guessing attack should.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300.0
SESSION_SECONDS = 900.0        # a terminal on a line is never logged out


SCHEMA = """
CREATE TABLE IF NOT EXISTS credential (
    op_id       TEXT PRIMARY KEY REFERENCES operator(op_id),
    salt        BLOB NOT NULL,
    hash        BLOB NOT NULL,
    iterations  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    must_change INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    op_id      TEXT,
    event      TEXT NOT NULL,
    detail     TEXT
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def derive(pin: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


class Credentials:
    """PIN storage and verification. No PIN is ever written anywhere."""

    def __init__(self, conn_factory):
        """`conn_factory` is a CALLABLE, not a connection.

        SQLite connections are bound to the thread that opened them and the
        server hands every request to a new one, so capturing a connection here
        works on the main thread and fails on every request -- which is exactly
        the bug the rest of this server already fixed with a thread-local, and
        exactly the bug this class reintroduced by taking the connection instead
        of the way to get one.
        """
        self._conn_factory = (conn_factory if callable(conn_factory)
                              else (lambda c=conn_factory: c))
        c = self.conn
        c.executescript(SCHEMA)
        c.commit()
        self._failures: dict = {}
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn_factory()

    def log(self, op_id, event: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO auth_event (ts, op_id, event, detail) VALUES (?,?,?,?)",
            (_now(), op_id, event, detail))
        self.conn.commit()

    def enrol(self, op_id: str, pin: str, must_change: bool = False) -> dict:
        if len(pin) < PIN_MIN_LEN:
            raise ValueError(f"PIN must be at least {PIN_MIN_LEN} characters")
        row = self.conn.execute("SELECT 1 FROM operator WHERE op_id=?",
                                (op_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown operator {op_id!r}")
        salt = secrets.token_bytes(SALT_BYTES)
        self.conn.execute(
            "INSERT OR REPLACE INTO credential "
            "(op_id, salt, hash, iterations, created_at, must_change) "
            "VALUES (?,?,?,?,?,?)",
            (op_id, salt, derive(pin, salt), ITERATIONS, _now(),
             int(must_change)))
        self.conn.commit()
        self.log(op_id, "ENROL")
        return {"op_id": op_id, "iterations": ITERATIONS}

    def locked_for(self, op_id: str) -> float:
        """Seconds remaining on a lockout, 0 if not locked."""
        with self._lock:
            rec = self._failures.get(op_id)
        if not rec or rec["count"] < MAX_FAILURES:
            return 0.0
        left = LOCKOUT_SECONDS - (time.monotonic() - rec["at"])
        return max(0.0, left)

    def verify(self, op_id: str, pin: str) -> dict:
        """Constant-time comparison, lockout, and an audit row either way.

        `compare_digest` rather than `==`: comparing a derived key with `==`
        returns as soon as a byte differs, and the time it takes is a
        measurement of how many leading bytes were right. That is a real attack
        on a local network and it costs one function call to remove.
        """
        left = self.locked_for(op_id)
        if left > 0:
            self.log(op_id, "LOCKED_OUT", f"{left:.0f}s remaining")
            return {"ok": False, "reason": "locked_out",
                    "retry_after_s": left}

        row = self.conn.execute(
            "SELECT salt, hash, iterations, must_change FROM credential "
            "WHERE op_id=?", (op_id,)).fetchone()
        if row is None:
            # An unenrolled operator still costs a derivation. Returning
            # immediately would make "no such credential" measurably faster than
            # "wrong PIN", which tells an attacker which badge ids are real.
            derive(pin, b"\x00" * SALT_BYTES)
            self.log(op_id, "NO_CREDENTIAL")
            return {"ok": False, "reason": "no_credential"}

        want = row["hash"] if not isinstance(row, tuple) else row[1]
        salt = row["salt"] if not isinstance(row, tuple) else row[0]
        iters = row["iterations"] if not isinstance(row, tuple) else row[2]
        got = derive(pin, bytes(salt), int(iters))
        if not hmac.compare_digest(bytes(want), got):
            with self._lock:
                rec = self._failures.setdefault(op_id, {"count": 0, "at": 0.0})
                rec["count"] += 1
                rec["at"] = time.monotonic()
                n = rec["count"]
            self.log(op_id, "BAD_PIN", f"attempt {n}")
            return {"ok": False, "reason": "bad_pin", "failures": n,
                    "locked": n >= MAX_FAILURES}

        with self._lock:
            self._failures.pop(op_id, None)
        self.log(op_id, "AUTH_OK")
        return {"ok": True, "op_id": op_id,
                "must_change": bool(row["must_change"]
                                    if not isinstance(row, tuple) else row[3])}

    def events(self, limit: int = 100) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM auth_event ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def self_signed(directory, host: str = "localhost", days: int = 2) -> dict:
    """A server certificate, generated per run.

    Server-only, not mutual. SE-1's collector authenticates GATEWAYS, which are
    machines the plant owns and can issue certificates to. A terminal
    authenticates PEOPLE, and issuing a client certificate per operator is an
    identity lifecycle -- the thing this module explicitly does not provide. So
    the transport proves the server and the PIN proves the person, and pretending
    otherwise by adding client certificates nobody manages would be worse than
    not having them.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = pathlib.Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MES-lite terminal"),
        x509.NameAttribute(NameOID.COMMON_NAME, host)])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName(host), x509.DNSName("localhost"),
                 x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]),
                critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .sign(key, hashes.SHA256()))
    cp, kp = d / "terminal.crt", d / "terminal.key"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return {"cert": str(cp), "key": str(kp), "host": host}


def server_context(pki: dict) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # A floor, not a preference. A minimum version is a setting somebody can
    # lower under pressure; refusing to build the context without it is not.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(pki["cert"], pki["key"])
    return ctx


def client_context(pki: dict, verify: bool = True) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if verify:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(pki["cert"])
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


LIMITS = [
    "This is not Part 11 compliance. Part 11 wants an identity lifecycle -- an "
    "authority issuing and revoking credentials, periodic access review, a "
    "password policy, and a validation package for the software enforcing them. "
    "This is the mechanism such a programme would sit on; a mechanism without a "
    "programme is not compliance.",
    "The certificate is self-signed and generated per run. There is no CA, no "
    "revocation, and a client that trusts it has to be handed the certificate "
    "out of band.",
    "Server-authenticated TLS only. Client certificates would authenticate "
    "MACHINES; a terminal needs to authenticate PEOPLE, and issuing a "
    "certificate per operator is the identity lifecycle this does not provide.",
    "Lockout state is in process memory, so it is lost on restart -- which is a "
    "way to clear a lockout by bouncing the service.",
    "No CSRF protection and no origin checking. It matters the moment this is "
    "served anywhere a browser can reach it from another page, and the fix is a "
    "same-site cookie plus an origin check rather than the bearer token used "
    "here.",
    "A four-digit PIN is four digits. PBKDF2 makes a leaked hash expensive to "
    "attack; it does not make the PIN strong, and the real defence is the "
    "lockout.",
]
