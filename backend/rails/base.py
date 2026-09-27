"""Shared types for payment rails."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

SETTLED = "settled"  # final on the rail
SUBMITTED = "submitted"  # accepted by the rail, confirmation pending (e.g. Circle)
FAILED = "failed"


@dataclass
class PaymentResult:
    rail: str
    amount_micros: int
    status: str
    reference: str = ""
    simulated: bool = False
    explorer_url: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (SETTLED, SUBMITTED)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Rail(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """Return (usable, human-readable reason)."""

    def pay(self, amount_micros: int, memo: str) -> PaymentResult:
        """Move ``amount_micros`` to the merchant. Must not raise for payment failures."""

    def status(self) -> dict[str, Any]:
        """Non-secret status for the UI / health endpoint."""
