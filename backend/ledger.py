"""The agent wallet's single source of truth.

Balances are integer micro-dollars (1 USD = 1_000_000 micros). That matches the
6-decimal precision of USDG/USDC and makes sub-cent prices exact.

Spending is two-phase so money never "moves" in the ledger unless the rail
actually settled: ``reserve`` holds the funds, then ``commit`` records the
payment or ``release`` returns the funds.

With a ``db_path`` the balance and transactions persist to SQLite. Holds are
never persisted: after a crash, an in-flight reservation is simply not spent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

MICROS_PER_USD = 1_000_000


def usd_to_micros(usd: float | str | Decimal) -> int:
    return int((Decimal(str(usd)) * MICROS_PER_USD).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> float:
    return micros / MICROS_PER_USD


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class InsufficientFunds(Exception):
    def __init__(self, balance_micros: int, needed_micros: int):
        self.balance_micros = balance_micros
        self.needed_micros = needed_micros
        super().__init__(
            f"Insufficient balance: have ${micros_to_usd(balance_micros):.6f}, "
            f"need ${micros_to_usd(needed_micros):.6f}"
        )


@dataclass
class Transaction:
    kind: str  # "charge" | "topup"
    reason: str
    amount_micros: int  # always positive; kind decides the direction
    rail: str
    reference: str = ""
    status: str = "settled"
    simulated: bool = False
    explorer_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"txn_{uuid.uuid4().hex[:12]}")
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # UI convention kept from v1: positive = money out, negative = money in.
        signed = self.amount_micros if self.kind == "charge" else -self.amount_micros
        data["amount_usd"] = micros_to_usd(signed)
        return data


@dataclass
class _Hold:
    amount_micros: int
    reason: str


class _Store:
    """Tiny SQLite persistence for the ledger."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS transactions (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, data TEXT NOT NULL)"
        )

    def load(self) -> tuple[int | None, list[Transaction]]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'balance_micros'").fetchone()
        txns = [
            Transaction(**{k: v for k, v in json.loads(data).items() if k != "amount_usd"})
            for (data,) in self._conn.execute("SELECT data FROM transactions ORDER BY seq DESC")
        ]
        return (int(row[0]) if row else None), txns

    def save(self, balance_micros: int, txn: Transaction | None = None) -> None:
        with self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('balance_micros', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(balance_micros),),
            )
            if txn is not None:
                self._conn.execute(
                    "INSERT INTO transactions (id, data) VALUES (?, ?)", (txn.id, json.dumps(txn.to_dict()))
                )


class Ledger:
    def __init__(self, initial_balance_micros: int = 0, db_path: str | Path | None = None):
        self._lock = threading.RLock()
        self._holds: dict[str, _Hold] = {}
        self._store = _Store(Path(db_path)) if db_path else None
        self._balance = initial_balance_micros
        self._transactions: list[Transaction] = []
        if self._store is not None:
            saved_balance, self._transactions = self._store.load()
            if saved_balance is None:
                self._store.save(initial_balance_micros)
            else:
                self._balance = saved_balance

    def _persist(self, txn: Transaction | None = None) -> None:
        if self._store is not None:
            # Durable balance excludes holds: an unfinished reservation isn't spent.
            self._store.save(self._balance + sum(h.amount_micros for h in self._holds.values()), txn)

    @property
    def balance_micros(self) -> int:
        with self._lock:
            return self._balance

    def transactions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self._transactions]

    def has_reference(self, reference: str) -> bool:
        ref = reference.lower()
        with self._lock:
            return any(t.reference.lower() == ref for t in self._transactions)

    def credited_since(self, rail: str, hours: float) -> int:
        """Total top-ups credited via ``rail`` in the last ``hours`` (survives restarts)."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self._lock:
            return sum(
                t.amount_micros
                for t in self._transactions
                if t.kind == "topup" and t.rail == rail and _parse_iso(t.timestamp) >= cutoff
            )

    def reserve(self, amount_micros: int, reason: str) -> str:
        if amount_micros <= 0:
            raise ValueError("amount must be positive")
        with self._lock:
            if self._balance < amount_micros:
                raise InsufficientFunds(self._balance, amount_micros)
            self._balance -= amount_micros
            hold_id = f"hold_{uuid.uuid4().hex[:12]}"
            self._holds[hold_id] = _Hold(amount_micros, reason)
            return hold_id

    def release(self, hold_id: str) -> None:
        with self._lock:
            hold = self._holds.pop(hold_id, None)
            if hold is not None:
                self._balance += hold.amount_micros

    def commit(self, hold_id: str, **txn_fields: Any) -> Transaction:
        with self._lock:
            hold = self._holds.pop(hold_id)
            txn = Transaction(
                kind="charge",
                reason=hold.reason,
                amount_micros=hold.amount_micros,
                **txn_fields,
            )
            self._transactions.insert(0, txn)
            self._persist(txn)
            return txn

    def credit(self, amount_micros: int, reason: str, **txn_fields: Any) -> Transaction:
        if amount_micros <= 0:
            raise ValueError("amount must be positive")
        with self._lock:
            self._balance += amount_micros
            txn = Transaction(kind="topup", reason=reason, amount_micros=amount_micros, **txn_fields)
            self._transactions.insert(0, txn)
            self._persist(txn)
            return txn

    def set_balance(self, amount_micros: int) -> None:
        """Demo helper: force the balance (e.g. to stage an auto top-up)."""
        with self._lock:
            self._balance = max(0, amount_micros)
            self._persist()
