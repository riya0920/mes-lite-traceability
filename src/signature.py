"""Electronic signatures and a hash-linked audit chain.

WHAT THE README SAID, and it was accurate: "Audit posture is AS9100-aware, not
compliant. Every transaction carries operator, timestamp and workstation, and the
log is append-only BY CONVENTION -- but there is no electronic signature, no
record retention policy, no controlled document linkage, and no validation
package."

"Append-only by convention" is the load-bearing phrase. A table anyone can UPDATE
is not an audit log; it is a table that currently happens to contain the truth.

TWO MECHANISMS, and they defend against different attackers.

1. ELECTRONIC SIGNATURE. 21 CFR Part 11 requires a signature to carry the
   signer's identity, the timestamp, and **the MEANING of the signature** --
   approved, reviewed, authored. That third component is the one that gets left
   out and it is the one that matters in a deposition: "OP-03 signed this" is
   worthless without knowing whether OP-03 was approving the work or merely
   recording that they had read it.

   The signature is over a CANONICAL serialisation of the record, so it commits
   to the content. Re-serialising with keys in a different order would produce a
   different hash and break every signature, which is why the ordering is fixed
   rather than left to dict iteration.

2. HASH-LINKED CHAIN. Each audit row carries the hash of its predecessor. Editing
   row 40 changes its hash, which breaks row 41's link, and so on to the end.
   This does not PREVENT tampering -- nothing in a database can, against someone
   with write access -- it makes tampering DETECTABLE, and detectable is what an
   auditor is actually asking for.

   The distinction is worth being precise about because it is often overstated:
   an attacker who can rewrite the whole table can also recompute the whole
   chain. What the chain defeats is the realistic case -- a targeted edit to one
   inconvenient row -- and it forces the harder case to rewrite everything, which
   leaves its own traces in backups and replicas.

WHAT IS STILL NOT COMPLIANT, stated so this is not mistaken for a validated
system: no identity provider, no password policy or account lockout, no periodic
access review, no controlled-document linkage, no IQ/OQ/PQ validation package,
and the signing key lives in the process rather than in an HSM.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS esign (
    sig_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    signer     TEXT NOT NULL,
    meaning    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    signed_ts  TEXT NOT NULL,
    signature  TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    row_hash   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retention_policy (
    record_type TEXT PRIMARY KEY,
    years       INTEGER NOT NULL,
    basis       TEXT NOT NULL
);
"""

# Meanings are an enumeration, not free text. Free text produces "ok", "done"
# and "signed", none of which answers what the signature asserts.
MEANINGS = {
    "AUTHORED": "I performed this work",
    "REVIEWED": "I have examined this record",
    "APPROVED": "I authorise this",
    "REJECTED": "I refuse this",
    "WITNESSED": "I observed this being performed",
}

# Retention is driven by the regulation that applies to the product, not by
# storage cost. Aerospace is the long one because an airframe outlives the
# company that built the part.
RETENTION = [
    ("op_record", 10, "AS9100 / customer contract: life of the part plus 7 years"),
    ("consumption", 10, "AS9100 traceability: must outlive any recall window"),
    ("ncr", 10, "AS9100 nonconformance records"),
    ("esign", 10, "21 CFR 11 s11.10(c): signatures live as long as the record"),
    ("audit", 10, "audit trail retained at least as long as what it audits"),
]


def canonical(payload: dict) -> str:
    """Deterministic serialisation. Sorted keys, no whitespace drift.

    A signature over a non-canonical form verifies today and fails the first
    time anything changes dict ordering -- which in practice is the first time
    somebody adds a field.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)


def sign(payload: dict, signer: str, meaning: str, key: bytes) -> dict:
    if meaning not in MEANINGS:
        raise ValueError(
            f"{meaning!r} is not a signature meaning. A signature without a "
            f"stated meaning does not say what it asserts; use one of "
            f"{sorted(MEANINGS)}")
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = canonical({"payload": payload, "signer": signer,
                      "meaning": meaning, "ts": ts})
    return {"signer": signer, "meaning": meaning, "payload": canonical(payload),
            "signed_ts": ts,
            "signature": hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}


def verify(row: dict, key: bytes) -> bool:
    body = canonical({"payload": json.loads(row["payload"]),
                      "signer": row["signer"], "meaning": row["meaning"],
                      "ts": row["signed_ts"]})
    expect = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, row["signature"])


def _row_hash(prev: str, sig: str, record_id: str, ts: str) -> str:
    return hashlib.sha256(f"{prev}|{sig}|{record_id}|{ts}".encode()).hexdigest()


class SignatureLog:
    def __init__(self, conn: sqlite3.Connection, key: bytes) -> None:
        self.conn = conn
        self.key = key
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR REPLACE INTO retention_policy VALUES (?,?,?)", RETENTION)
        conn.commit()

    def _last_hash(self) -> str:
        r = self.conn.execute(
            "SELECT row_hash FROM esign ORDER BY sig_id DESC LIMIT 1").fetchone()
        return r[0] if r else "GENESIS"

    def add(self, record_type: str, record_id: str, payload: dict,
            signer: str, meaning: str) -> dict:
        s = sign(payload, signer, meaning, self.key)
        prev = self._last_hash()
        rh = _row_hash(prev, s["signature"], record_id, s["signed_ts"])
        self.conn.execute(
            "INSERT INTO esign (record_type, record_id, signer, meaning, "
            "payload, signed_ts, signature, prev_hash, row_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (record_type, record_id, signer, meaning, s["payload"],
             s["signed_ts"], s["signature"], prev, rh))
        self.conn.commit()
        return {**s, "prev_hash": prev, "row_hash": rh}

    def verify_chain(self) -> dict:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM esign ORDER BY sig_id")]
        prev = "GENESIS"
        for r in rows:
            if r["prev_hash"] != prev:
                return {"intact": False, "broken_at": r["sig_id"],
                        "why": "prev_hash does not match the preceding row"}
            if not verify(r, self.key):
                return {"intact": False, "broken_at": r["sig_id"],
                        "why": "signature does not verify against the payload"}
            expect = _row_hash(r["prev_hash"], r["signature"], r["record_id"],
                               r["signed_ts"])
            if expect != r["row_hash"]:
                return {"intact": False, "broken_at": r["sig_id"],
                        "why": "row_hash does not match its own contents"}
            prev = r["row_hash"]
        return {"intact": True, "n_rows": len(rows)}

    def retention_due(self, now_year: int) -> list[dict]:
        return [{"record_type": t, "years": y, "basis": b,
                 "purge_before_year": now_year - y}
                for t, y, b in self.conn.execute(
                    "SELECT record_type, years, basis FROM retention_policy")]


def demo(db_path) -> dict:
    """Sign some records, then try to tamper with them."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    key = b"a-key-that-would-live-in-an-HSM"
    log = SignatureLog(conn, key)

    for i in range(6):
        log.add("ncr", f"NCR-{i:03d}",
                {"unit": f"U{i:03d}", "defect": "porosity", "qty": 1},
                signer=f"QE-{i % 2 + 1}", meaning="APPROVED")

    checks = []
    # (a) a signature with no stated meaning
    try:
        sign({"x": 1}, "QE-1", "looks fine", key)
        checks.append({"attempt": "sign with a free-text meaning", "refused": False})
    except ValueError:
        checks.append({"attempt": "sign with a free-text meaning", "refused": True})

    # (b) a forged signature
    forged = dict(conn.execute("SELECT * FROM esign WHERE sig_id=1").fetchone())
    forged["payload"] = json.dumps({"unit": "U999", "defect": "none", "qty": 1})
    checks.append({"attempt": "alter the payload under a valid signature",
                   "refused": not verify(forged, key)})

    # (c) a wrong key
    checks.append({"attempt": "verify with the wrong key",
                   "refused": not verify(
                       dict(conn.execute("SELECT * FROM esign WHERE sig_id=1")
                            .fetchone()), b"wrong-key")})

    before = log.verify_chain()
    # (d) the realistic attack: edit ONE inconvenient row in place.
    conn.execute("UPDATE esign SET payload=? WHERE sig_id=3",
                 (json.dumps({"unit": "U002", "defect": "none", "qty": 0}),))
    conn.commit()
    after = log.verify_chain()
    checks.append({"attempt": "edit one row in place after the fact",
                   "refused": not after["intact"]})

    retention = log.retention_due(2026)
    conn.close()
    return {
        "signed": 6, "chain_before_tamper": before, "chain_after_tamper": after,
        "tamper_detected": not after["intact"],
        "tamper_row": after.get("broken_at"),
        "checks": checks, "retention": retention,
        "meanings": MEANINGS,
        "summary": (f"{6} records signed with a stated meaning, chain intact "
                    f"before tampering ({before['intact']}), broken and detected "
                    f"after ({not after['intact']})"),
    }
