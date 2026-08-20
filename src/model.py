"""MES-lite domain model. SQLite schema + the invariants that make it manufacturing.

ISA-95 naming where it applies, and the level 3/4 boundary stated plainly: this is
LEVEL 3 (manufacturing operations management -- execution). Orders come DOWN from
ERP (level 4) with a quantity and a due date; completions, consumption, and scrap
go UP. Planning, costing, purchasing and the general ledger are level 4 and are
deliberately absent. A system that plans its own orders is not an MES.

The three modelling decisions that make this manufacturing execution rather than a
task tracker:

1. ROUTINGS. A product is not a thing you build, it is an ordered sequence of
   operations at work centres, each with standard times, required materials, and
   required operator certifications.

2. CONSUMPTION AT OPERATION, not at order. The genealogy edge is created when lot
   L is issued to work order W *at operation N*. This is the single most important
   modelling choice in the file and §"Why consumption-at-operation" below explains
   what it buys during a recall.

3. QUANTITY CONSERVATION as an enforced invariant, not a report. At every
   operation, started == completed + scrapped + in_process, always. This is
   manufacturing's double-entry bookkeeping: it is the property that makes the
   numbers auditable, and like double-entry it only works if the system refuses to
   record a transaction that breaks it.

WHY CONSUMPTION-AT-OPERATION
---------------------------
Suppose work order WO-1001 builds 300 units and consumes bar stock across
operation 20. It draws from lot L-4471 for the first 180 units and lot L-4998 for
the rest. Supplier recalls L-4471.

  order-level consumption: the order used L-4471, so ALL 300 units are suspect.
  operation-level:         the 180 units that consumed from L-4471 are suspect.

The exposure differs by 40%, and in a recall that difference is the number of
customers who get a letter. Order-level consumption cannot answer the question
because it never recorded which units were built while which lot was mounted.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- definitions
CREATE TABLE IF NOT EXISTS product (
    sku          TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    tracking     TEXT NOT NULL CHECK (tracking IN ('serial','lot'))
);

CREATE TABLE IF NOT EXISTS work_center (
    wc_id        TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    capacity     INTEGER NOT NULL DEFAULT 1
);

-- An operation belongs to a product's routing. `seq` is the classic 10/20/30
-- numbering, spaced so an operation can be inserted later without renumbering.
CREATE TABLE IF NOT EXISTS operation (
    sku           TEXT NOT NULL REFERENCES product(sku),
    seq           INTEGER NOT NULL,
    name          TEXT NOT NULL,
    wc_id         TEXT NOT NULL REFERENCES work_center(wc_id),
    std_setup_s   REAL NOT NULL DEFAULT 0,
    std_run_s     REAL NOT NULL,
    cert_required TEXT,
    PRIMARY KEY (sku, seq)
);

-- BOM lines are attached to the operation that consumes them, not to the product.
CREATE TABLE IF NOT EXISTS bom_line (
    sku          TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    component    TEXT NOT NULL,
    qty_per      REAL NOT NULL,
    FOREIGN KEY (sku, seq) REFERENCES operation(sku, seq),
    PRIMARY KEY (sku, seq, component)
);

CREATE TABLE IF NOT EXISTS operator (
    op_id  TEXT PRIMARY KEY,
    name   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certification (
    op_id  TEXT NOT NULL REFERENCES operator(op_id),
    cert   TEXT NOT NULL,
    PRIMARY KEY (op_id, cert)
);

-- ---------------------------------------------------------------- inventory
CREATE TABLE IF NOT EXISTS lot (
    lot_id       TEXT PRIMARY KEY,
    component    TEXT NOT NULL,
    supplier     TEXT NOT NULL,
    qty_received REAL NOT NULL,
    qty_on_hand  REAL NOT NULL,
    parent_lot   TEXT REFERENCES lot(lot_id),   -- set when this lot is a split
    received_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- execution
CREATE TABLE IF NOT EXISTS work_order (
    wo_id      TEXT PRIMARY KEY,
    sku        TEXT NOT NULL REFERENCES product(sku),
    qty        INTEGER NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('RELEASED','IN_PROCESS','CLOSED')),
    released_at TEXT NOT NULL
);

-- One row per serialised unit. Lot-tracked products get a single pseudo-unit
-- carrying the batch quantity; the dual model is explained in README.
CREATE TABLE IF NOT EXISTS unit (
    unit_id    TEXT PRIMARY KEY,
    wo_id      TEXT NOT NULL REFERENCES work_order(wo_id),
    serial     TEXT,
    lot_qty    REAL,
    status     TEXT NOT NULL CHECK (status IN ('IN_PROCESS','COMPLETE','SCRAPPED','QUARANTINED')),
    current_seq INTEGER
);

-- The execution ledger. Append-only: a correction is another row, never an UPDATE.
CREATE TABLE IF NOT EXISTS op_record (
    rec_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id       TEXT NOT NULL REFERENCES work_order(wo_id),
    unit_id     TEXT NOT NULL REFERENCES unit(unit_id),
    seq         INTEGER NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('START','COMPLETE','SCRAP','REWORK_ENTRY')),
    qty         REAL NOT NULL DEFAULT 1,
    op_id       TEXT REFERENCES operator(op_id),
    wc_id       TEXT REFERENCES work_center(wc_id),
    reason      TEXT,
    deviation_ref TEXT,
    ts          TEXT NOT NULL
);

-- THE genealogy table. One row = "this unit consumed this quantity of this lot at
-- this operation". Everything traceability does is a query over this.
CREATE TABLE IF NOT EXISTS consumption (
    cons_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id   TEXT NOT NULL REFERENCES unit(unit_id),
    wo_id     TEXT NOT NULL REFERENCES work_order(wo_id),
    seq       INTEGER NOT NULL,
    lot_id    TEXT NOT NULL REFERENCES lot(lot_id),
    component TEXT NOT NULL,
    qty       REAL NOT NULL,
    op_id     TEXT REFERENCES operator(op_id),
    ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ncr (
    ncr_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id      TEXT NOT NULL REFERENCES unit(unit_id),
    seq          INTEGER NOT NULL,
    defect       TEXT NOT NULL,
    disposition  TEXT CHECK (disposition IN ('REWORK','USE_AS_IS','SCRAP')),
    rework_to_seq INTEGER,
    approved_by  TEXT REFERENCES operator(op_id),
    raised_at    TEXT NOT NULL,
    closed_at    TEXT
);

CREATE TABLE IF NOT EXISTS shipment (
    ship_id   TEXT PRIMARY KEY,
    customer  TEXT NOT NULL,
    shipped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_line (
    ship_id  TEXT NOT NULL REFERENCES shipment(ship_id),
    unit_id  TEXT NOT NULL REFERENCES unit(unit_id),
    PRIMARY KEY (ship_id, unit_id)
);

-- Every transaction carries who/when/where. AS9100 and ISO 9001 both require
-- traceable records of who performed and who verified; this is the minimum shape
-- of that, and calling it "AS9100-aware" rather than "AS9100-compliant" is the
-- honest scope statement.
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    op_id     TEXT,
    station   TEXT,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS ix_cons_lot  ON consumption(lot_id);
CREATE INDEX IF NOT EXISTS ix_cons_unit ON consumption(unit_id);
CREATE INDEX IF NOT EXISTS ix_op_unit   ON op_record(unit_id, seq);
CREATE INDEX IF NOT EXISTS ix_unit_wo   ON unit(wo_id);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def create(path) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
