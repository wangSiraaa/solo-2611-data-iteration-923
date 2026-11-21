"""SQLite plumbing shared by the quote service and the after-sales ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_loads(value: str) -> Any:
    return json.loads(value)


def deep_copy(value: Any) -> Any:
    return json_loads(json.dumps(value, ensure_ascii=False))


def ensure_parent(path: str) -> None:
    if path and path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)


class Database:
    """A tiny per-operation connection factory.

    Every call opens its own connection so two instances (or two threads) can
    exercise real SQLite locking.  ``BEGIN IMMEDIATE`` serialises writers; the
    busy timeout makes a competing writer wait instead of failing spuriously.
    """

    def __init__(self, path: str, busy_timeout_ms: int = 30000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._memory_lock = threading.RLock()
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._memory_ready = False
        ensure_parent(path)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema(conn)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.path == ":memory:":
            with self._memory_lock:
                if self._memory_conn is None:
                    self._memory_conn = self._new_connection()
                    if not self._memory_ready:
                        self._init_schema(self._memory_conn)
                        self._memory_ready = True
                yield self._memory_conn
            return
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL UNIQUE,
                content_json TEXT NOT NULL,
                coupons_json TEXT NOT NULL,
                products_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quotes (
                request_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                basket_sig TEXT NOT NULL,
                basket_json TEXT NOT NULL,
                quote_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sales (
                sale_id TEXT PRIMARY KEY,
                quote_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                quote_sig TEXT NOT NULL,
                quote_json TEXT NOT NULL,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sale_units (
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                price INTEGER NOT NULL,
                coupon_id TEXT,
                discount INTEGER NOT NULL,
                paid INTEGER NOT NULL,
                returned INTEGER NOT NULL DEFAULT 0,
                return_request_id TEXT,
                grade TEXT,
                PRIMARY KEY (sale_id, unit_id),
                FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
            );

            CREATE TABLE IF NOT EXISTS return_requests (
                request_id TEXT PRIMARY KEY,
                sale_id TEXT NOT NULL,
                canonical_request TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
            );

            CREATE TABLE IF NOT EXISTS return_items (
                request_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                grade TEXT NOT NULL,
                original_paid INTEGER NOT NULL,
                refund INTEGER NOT NULL,
                PRIMARY KEY (request_id, unit_id),
                FOREIGN KEY (request_id) REFERENCES return_requests(request_id)
            );

            CREATE TABLE IF NOT EXISTS adjustments (
                event_id TEXT PRIMARY KEY,
                content_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK (bucket IN ('RESTOCK', 'SCRAP')),
                delta INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_totals (
                sku TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK (bucket IN ('RESTOCK', 'SCRAP')),
                qty INTEGER NOT NULL,
                PRIMARY KEY (sku, bucket)
            );
            """
        )
