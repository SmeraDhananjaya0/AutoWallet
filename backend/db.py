"""Postgres-backed state for multi-instance hosting (e.g. Vercel).

When DATABASE_URL is set, every piece of mutable state lives here instead of
in process memory: the ledger, scoped payment tokens, used payment proofs, chat
sessions and small settings. The database is authoritative, so any number of
server instances stay consistent. Schema: ``migrations/001_autowallet.sql``.

Money operations are single SQL statements or single transactions, so the
balance can't go negative or double-spend under concurrency.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.ledger import InsufficientFunds, Transaction

TRANSACTION_LIMIT = 200


class Database:
    def __init__(self, url: str):
        self._url = url

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        # One short connection per operation: suits serverless, and Supabase's
        # transaction pooler multiplexes them. prepare_threshold=None is required
        # behind a transaction-mode pooler.
        with psycopg.connect(self._url, prepare_threshold=None, row_factory=dict_row, connect_timeout=10) as conn:
            with conn.transaction():
                yield conn

    def get_meta(self, key: str) -> str | None:
        with self.tx() as c:
            row = c.execute("select value from autowallet.meta where key = %s", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "insert into autowallet.meta (key, value) values (%s, %s) "
                "on conflict (key) do update set value = excluded.value",
                (key, value),
            )


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _txn_from_row(row: dict[str, Any]) -> Transaction:
    return Transaction(
        kind=row["kind"],
        reason=row["reason"],
        amount_micros=row["amount_micros"],
        rail=row["rail"],
        reference=row["reference"],
        status=row["status"],
        simulated=row["simulated"],
        explorer_url=row["explorer_url"],
        metadata=row["metadata"] or {},
        id=row["id"],
        timestamp=_iso(row["created_at"]),
    )


_INSERT_TXN = """
    insert into autowallet.transactions
        (id, kind, reason, amount_micros, rail, reference, status, simulated, explorer_url, metadata)
    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    returning *
"""


class PgLedger:
    """Same interface as ``backend.ledger.Ledger``; the database is the source of truth."""

    def __init__(self, db: Database, initial_balance_micros: int):
        self._db = db
        with db.tx() as c:
            c.execute(
                "insert into autowallet.wallet (id, balance_micros) values (1, %s) on conflict (id) do nothing",
                (initial_balance_micros,),
            )

    @property
    def balance_micros(self) -> int:
        with self._db.tx() as c:
            return c.execute("select balance_micros from autowallet.wallet where id = 1").fetchone()["balance_micros"]

    def transactions(self) -> list[dict[str, Any]]:
        with self._db.tx() as c:
            rows = c.execute(
                "select * from autowallet.transactions order by seq desc limit %s", (TRANSACTION_LIMIT,)
            ).fetchall()
        return [_txn_from_row(r).to_dict() for r in rows]

    def has_reference(self, reference: str) -> bool:
        with self._db.tx() as c:
            row = c.execute(
                "select exists(select 1 from autowallet.transactions where lower(reference) = lower(%s)) as found",
                (reference,),
            ).fetchone()
        return row["found"]

    def credited_since(self, rail: str, hours: float) -> int:
        with self._db.tx() as c:
            row = c.execute(
                "select coalesce(sum(amount_micros), 0) as total from autowallet.transactions "
                "where kind = 'topup' and rail = %s and created_at >= now() - make_interval(secs => %s)",
                (rail, hours * 3600),
            ).fetchone()
        return int(row["total"])

    def reserve(self, amount_micros: int, reason: str) -> str:
        if amount_micros <= 0:
            raise ValueError("amount must be positive")
        hold_id = f"hold_{uuid.uuid4().hex[:12]}"
        with self._db.tx() as c:
            row = c.execute(
                "update autowallet.wallet set balance_micros = balance_micros - %s "
                "where id = 1 and balance_micros >= %s returning balance_micros",
                (amount_micros, amount_micros),
            ).fetchone()
            if row is None:
                balance = c.execute("select balance_micros from autowallet.wallet where id = 1").fetchone()
                raise InsufficientFunds(balance["balance_micros"], amount_micros)
            c.execute(
                "insert into autowallet.holds (id, amount_micros, reason) values (%s, %s, %s)",
                (hold_id, amount_micros, reason),
            )
        return hold_id

    def release(self, hold_id: str) -> None:
        with self._db.tx() as c:
            hold = c.execute("delete from autowallet.holds where id = %s returning amount_micros", (hold_id,)).fetchone()
            if hold:
                c.execute(
                    "update autowallet.wallet set balance_micros = balance_micros + %s where id = 1",
                    (hold["amount_micros"],),
                )

    def commit(self, hold_id: str, **txn_fields: Any) -> Transaction:
        with self._db.tx() as c:
            hold = c.execute(
                "delete from autowallet.holds where id = %s returning amount_micros, reason", (hold_id,)
            ).fetchone()
            if hold is None:
                raise KeyError(hold_id)
            txn = Transaction(kind="charge", reason=hold["reason"], amount_micros=hold["amount_micros"], **txn_fields)
            row = c.execute(_INSERT_TXN, self._txn_params(txn)).fetchone()
        return _txn_from_row(row)

    def credit(self, amount_micros: int, reason: str, **txn_fields: Any) -> Transaction:
        if amount_micros <= 0:
            raise ValueError("amount must be positive")
        txn = Transaction(kind="topup", reason=reason, amount_micros=amount_micros, **txn_fields)
        with self._db.tx() as c:
            c.execute("update autowallet.wallet set balance_micros = balance_micros + %s where id = 1", (amount_micros,))
            row = c.execute(_INSERT_TXN, self._txn_params(txn)).fetchone()
        return _txn_from_row(row)

    def set_balance(self, amount_micros: int) -> None:
        with self._db.tx() as c:
            c.execute("update autowallet.wallet set balance_micros = %s where id = 1", (max(0, amount_micros),))

    @staticmethod
    def _txn_params(t: Transaction) -> tuple[Any, ...]:
        return (
            t.id, t.kind, t.reason, t.amount_micros, t.rail, t.reference,
            t.status, t.simulated, t.explorer_url, Jsonb(t.metadata),
        )


class PgGrantStore:
    """Scoped payment token grants (see ``backend.spt``)."""

    def __init__(self, db: Database):
        self._db = db

    def insert(self, grant: Any) -> None:
        with self._db.tx() as c:
            c.execute(
                "insert into autowallet.spt_grants (jti, agent_id, scope, max_micros, spent_micros, expires_at) "
                "values (%s, %s, %s, %s, %s, to_timestamp(%s))",
                (grant.jti, grant.agent_id, grant.scope, grant.max_micros, grant.spent_micros, grant.expires_at),
            )

    def get(self, jti: str) -> dict[str, Any] | None:
        with self._db.tx() as c:
            return c.execute(
                "select jti, agent_id, scope, max_micros, spent_micros, extract(epoch from expires_at)::bigint as expires_at "
                "from autowallet.spt_grants where jti = %s",
                (jti,),
            ).fetchone()

    def try_spend(self, jti: str, amount_micros: int) -> dict[str, Any] | None:
        with self._db.tx() as c:
            return c.execute(
                "update autowallet.spt_grants set spent_micros = spent_micros + %s "
                "where jti = %s and spent_micros + %s <= max_micros "
                "returning jti, agent_id, scope, max_micros, spent_micros, extract(epoch from expires_at)::bigint as expires_at",
                (amount_micros, jti, amount_micros),
            ).fetchone()

    def refund(self, jti: str, amount_micros: int) -> None:
        with self._db.tx() as c:
            c.execute(
                "update autowallet.spt_grants set spent_micros = greatest(0, spent_micros - %s) where jti = %s",
                (amount_micros, jti),
            )


class PgReplayGuard:
    def __init__(self, db: Database):
        self._db = db

    def claim(self, key: str) -> bool:
        with self._db.tx() as c:
            row = c.execute(
                "insert into autowallet.used_payment_proofs (tx_hash) values (%s) on conflict do nothing returning tx_hash",
                (key,),
            ).fetchone()
        return row is not None


class PgSessionStore:
    def __init__(self, db: Database):
        self._db = db

    def get(self, session_id: str) -> list[dict[str, Any]]:
        with self._db.tx() as c:
            row = c.execute("select messages from autowallet.chat_sessions where id = %s", (session_id,)).fetchone()
        return list(row["messages"]) if row else []

    def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._db.tx() as c:
            c.execute(
                "insert into autowallet.chat_sessions (id, messages, updated_at) values (%s, %s, now()) "
                "on conflict (id) do update set messages = excluded.messages, updated_at = now()",
                (session_id, Jsonb(json.loads(json.dumps(messages)))),
            )
