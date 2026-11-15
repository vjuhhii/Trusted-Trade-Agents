"""SQLite persistence: purchase requests, orders and a hash-chained audit log.

One file is shared by the REST server and the MCP server, so an approval given in the browser
is visible to the agent's next call.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aimo_agent.models import AuditEvent, Order, PurchaseRequest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, buyer_id TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, request_id TEXT NOT NULL,
    buyer_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, request_id TEXT, order_id TEXT,
    actor TEXT NOT NULL, type TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""

# Stock held by these orders is not offered again.
_HOLDING_STOCK = ("confirmed", "pending_approval")
_GENESIS = "0" * 64


def _digest(previous: str, at: str, fields: dict[str, Any]) -> str:
    payload = json.dumps({"previous": previous, "at": at, **fields}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class Store:
    def __init__(self, path: Path | str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # IMMEDIATE takes the write lock up front, so two processes cannot interleave.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def clear(self) -> None:
        """Delete every request, order and audit event. For rehearsing a demo from scratch."""
        with self._transaction() as db:
            for table in ("requests", "orders", "events", "counters"):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM sqlite_sequence WHERE name = 'events'")

    def next_id(self, prefix: str) -> str:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO counters (name, value) VALUES (?, 1) "
                "ON CONFLICT (name) DO UPDATE SET value = value + 1",
                (prefix,),
            )
            (value,) = db.execute("SELECT value FROM counters WHERE name = ?", (prefix,)).fetchone()
        return f"{prefix}-{value:04d}"

    # Requests

    def save_request(self, request: PurchaseRequest) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO requests (id, created_at, buyer_id, body) VALUES (?, ?, ?, ?)",
            (
                request.id,
                request.created_at.isoformat(),
                request.buyer_id,
                request.model_dump_json(),
            ),
        )

    def get_request(self, request_id: str) -> PurchaseRequest | None:
        row = self._db.execute("SELECT body FROM requests WHERE id = ?", (request_id,)).fetchone()
        return PurchaseRequest.model_validate_json(row[0]) if row else None

    def list_requests(self, limit: int = 30) -> list[PurchaseRequest]:
        """The latest requests, newest first."""
        rows = self._db.execute(
            "SELECT body FROM requests ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [PurchaseRequest.model_validate_json(row[0]) for row in rows]

    def requests_of(self, buyer_id: str, after_seq: int = 0) -> list[PurchaseRequest]:
        """A buyer's requests, oldest first: those received after audit event `after_seq`.

        The audit log's sequence orders events strictly; timestamps may tie.
        """
        rows = self._db.execute(
            "SELECT body FROM requests WHERE buyer_id = ? AND id IN "
            "(SELECT request_id FROM events WHERE type = 'request.received' AND seq > ?) "
            "ORDER BY id",
            (buyer_id, after_seq),
        ).fetchall()
        return [PurchaseRequest.model_validate_json(row[0]) for row in rows]

    def last_seq(self, order_ids: list[str]) -> int:
        """The latest audit event of these orders, 0 when there is none."""
        if not order_ids:
            return 0
        marks = ", ".join("?" * len(order_ids))
        (seq,) = self._db.execute(
            f"SELECT COALESCE(MAX(seq), 0) FROM events WHERE order_id IN ({marks})", order_ids
        ).fetchone()
        return seq

    # Orders

    def save_order(self, order: Order) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO orders (id, created_at, request_id, buyer_id, status, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                order.id,
                order.created_at.isoformat(),
                order.request_id,
                order.buyer_id,
                order.status,
                order.model_dump_json(),
            ),
        )

    def get_order(self, order_id: str) -> Order | None:
        row = self._db.execute("SELECT body FROM orders WHERE id = ?", (order_id,)).fetchone()
        return Order.model_validate_json(row[0]) if row else None

    def list_orders(
        self,
        *,
        status: str | None = None,
        buyer_id: str | None = None,
        request_id: str | None = None,
    ) -> list[Order]:
        """Orders matching every given filter, newest first."""
        clauses, values = [], []
        for column, value in (
            ("status", status),
            ("buyer_id", buyer_id),
            ("request_id", request_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.execute(
            f"SELECT body FROM orders {where} ORDER BY created_at DESC, id DESC", values
        ).fetchall()
        return [Order.model_validate_json(row[0]) for row in rows]

    def reserved_units(self) -> dict[str, float]:
        """Sales units per product held by confirmed and pending orders."""
        reserved: dict[str, float] = {}
        for status in _HOLDING_STOCK:
            for order in self.list_orders(status=status):
                for line in order.option.lines:
                    reserved[line.product_id] = reserved.get(line.product_id, 0.0) + line.units
        return reserved

    # Audit log

    def add_event(
        self,
        actor: str,
        type: str,
        summary: str,
        *,
        request_id: str | None = None,
        order_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        at = datetime.now(UTC).isoformat()
        fields = {
            "request_id": request_id,
            "order_id": order_id,
            "actor": actor,
            "type": type,
            "summary": summary,
            "detail": json.dumps(detail or {}, sort_keys=True, default=str),
        }
        with self._transaction() as db:
            last = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            digest = _digest(last[0] if last else _GENESIS, at, fields)
            cursor = db.execute(
                "INSERT INTO events (at, request_id, order_id, actor, type, summary, detail, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (at, request_id, order_id, actor, type, summary, fields["detail"], digest),
            )
            seq = cursor.lastrowid
        return self._event((seq, at, *fields.values(), digest))

    @staticmethod
    def _event(row: tuple[Any, ...]) -> AuditEvent:
        seq, at, request_id, order_id, actor, type_, summary, detail, digest = row
        return AuditEvent(
            seq=seq,
            at=datetime.fromisoformat(at),
            request_id=request_id,
            order_id=order_id,
            actor=actor,
            type=type_,
            summary=summary,
            detail=json.loads(detail),
            hash=digest,
        )

    def events(
        self, *, request_id: str | None = None, order_id: str | None = None, limit: int = 500
    ) -> list[AuditEvent]:
        """Events in order. An order's trail includes the events of its request."""
        clauses, values = [], []
        if order_id is not None:
            order = self.get_order(order_id)
            clauses.append("(order_id = ? OR (order_id IS NULL AND request_id = ?))")
            values += [order_id, order.request_id if order else ""]
        elif request_id is not None:
            clauses.append("request_id = ?")
            values.append(request_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.execute(
            "SELECT seq, at, request_id, order_id, actor, type, summary, detail, hash "
            f"FROM events {where} ORDER BY seq DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def verify_chain(self) -> tuple[bool, int, int | None]:
        """Recompute every hash. Returns (intact, events checked, seq of the first bad event)."""
        previous = _GENESIS
        checked = 0
        rows = self._db.execute(
            "SELECT seq, at, request_id, order_id, actor, type, summary, detail, hash "
            "FROM events ORDER BY seq"
        )
        for seq, at, request_id, order_id, actor, type_, summary, detail, digest in rows:
            fields = {
                "request_id": request_id,
                "order_id": order_id,
                "actor": actor,
                "type": type_,
                "summary": summary,
                "detail": detail,
            }
            if _digest(previous, at, fields) != digest:
                return False, checked, seq
            previous = digest
            checked += 1
        return True, checked, None
