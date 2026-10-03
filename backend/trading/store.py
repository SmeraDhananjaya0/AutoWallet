"""Storage for trade proposals, positions and trading rules.

MemoryTradingStore: single process (local dev, tests).
PgTradingStore: shared Postgres (hosted); schema in migrations/002_trading.sql.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

RULES_KEY = "trading_rules"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


class MemoryTradingStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proposals: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, dict[str, int]] = {}
        self._rules: dict[str, Any] | None = None

    # proposals
    def insert_proposal(self, p: dict[str, Any]) -> None:
        with self._lock:
            self._proposals[p["id"]] = {**copy.deepcopy(p), "_created": _now()}

    def get_proposal(self, pid: str) -> dict[str, Any] | None:
        with self._lock:
            p = self._proposals.get(pid)
            return self._public(p) if p else None

    def transition(self, pid: str, from_status: str, to_status: str) -> bool:
        """Atomically move a proposal between states (prevents double execution)."""
        with self._lock:
            p = self._proposals.get(pid)
            if not p or p["status"] != from_status:
                return False
            p["status"] = to_status
            return True

    def update_proposal(self, pid: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            p = self._proposals[pid]
            p.update(copy.deepcopy(changes))
            if changes.get("status") == "executed":
                p["_executed"] = _now()
            return self._public(p)

    def list_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._proposals.values(), key=lambda p: p["_created"], reverse=True)[:limit]
            return [self._public(p) for p in items]

    def executed_notional_since(self, hours: float) -> int:
        cutoff = _now() - timedelta(hours=hours)
        with self._lock:
            return sum(
                (p.get("fill") or {}).get("usd_micros", 0)
                for p in self._proposals.values()
                if p["status"] == "executed" and p.get("_executed", cutoff) >= cutoff
            )

    # positions
    def positions(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {s: dict(v) for s, v in self._positions.items() if v["shares_units"] > 0}

    def apply_fill(self, symbol: str, shares_delta: int, cost_delta: int) -> None:
        with self._lock:
            pos = self._positions.setdefault(symbol, {"shares_units": 0, "cost_micros": 0})
            pos["shares_units"] = max(0, pos["shares_units"] + shares_delta)
            pos["cost_micros"] = max(0, pos["cost_micros"] + cost_delta)

    # rules
    def get_rules(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._rules)

    def set_rules(self, rules: dict[str, Any]) -> None:
        with self._lock:
            self._rules = copy.deepcopy(rules)

    @staticmethod
    def _public(p: dict[str, Any]) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in p.items() if not k.startswith("_")}


class PgTradingStore:
    def __init__(self, db: Any):
        self._db = db

    def insert_proposal(self, p: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._db.tx() as c:
            c.execute(
                "insert into autowallet.trade_proposals (id, data, status) values (%s, %s, %s)",
                (p["id"], Jsonb(p), p["status"]),
            )

    def get_proposal(self, pid: str) -> dict[str, Any] | None:
        with self._db.tx() as c:
            row = c.execute("select data, status from autowallet.trade_proposals where id = %s", (pid,)).fetchone()
        return {**row["data"], "status": row["status"]} if row else None

    def transition(self, pid: str, from_status: str, to_status: str) -> bool:
        with self._db.tx() as c:
            row = c.execute(
                "update autowallet.trade_proposals set status = %s where id = %s and status = %s returning id",
                (to_status, pid, from_status),
            ).fetchone()
        return row is not None

    def update_proposal(self, pid: str, **changes: Any) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        with self._db.tx() as c:
            row = c.execute(
                "update autowallet.trade_proposals set data = data || %s, "
                "status = coalesce(%s, status), "
                "executed_at = case when %s = 'executed' then now() else executed_at end, "
                "notional_micros = coalesce(%s, notional_micros) "
                "where id = %s returning data, status",
                (
                    Jsonb(changes),
                    changes.get("status"),
                    changes.get("status"),
                    (changes.get("fill") or {}).get("usd_micros"),
                    pid,
                ),
            ).fetchone()
        return {**row["data"], "status": row["status"]}

    def list_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._db.tx() as c:
            rows = c.execute(
                "select data, status from autowallet.trade_proposals order by created_at desc limit %s", (limit,)
            ).fetchall()
        return [{**r["data"], "status": r["status"]} for r in rows]

    def executed_notional_since(self, hours: float) -> int:
        with self._db.tx() as c:
            row = c.execute(
                "select coalesce(sum(notional_micros), 0) as total from autowallet.trade_proposals "
                "where status = 'executed' and executed_at >= now() - make_interval(secs => %s)",
                (hours * 3600,),
            ).fetchone()
        return int(row["total"])

    def positions(self) -> dict[str, dict[str, int]]:
        with self._db.tx() as c:
            rows = c.execute(
                "select symbol, shares_units, cost_micros from autowallet.positions where shares_units > 0"
            ).fetchall()
        return {r["symbol"]: {"shares_units": int(r["shares_units"]), "cost_micros": int(r["cost_micros"])} for r in rows}

    def apply_fill(self, symbol: str, shares_delta: int, cost_delta: int) -> None:
        with self._db.tx() as c:
            c.execute(
                "insert into autowallet.positions (symbol, shares_units, cost_micros) "
                "values (%s, greatest(0, %s::numeric), greatest(0, %s::bigint)) "
                "on conflict (symbol) do update set "
                "shares_units = greatest(0, autowallet.positions.shares_units + %s::numeric), "
                "cost_micros = greatest(0, autowallet.positions.cost_micros + %s::bigint)",
                (symbol, shares_delta, cost_delta, shares_delta, cost_delta),
            )

    def get_rules(self) -> dict[str, Any] | None:
        import json

        value = self._db.get_meta(RULES_KEY)
        return json.loads(value) if value else None

    def set_rules(self, rules: dict[str, Any]) -> None:
        import json

        self._db.set_meta(RULES_KEY, json.dumps(rules))
