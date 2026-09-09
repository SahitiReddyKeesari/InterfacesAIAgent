"""Seed data for the mock back-office apps.

Deliberately includes fields that must never reach an artifact or a log (SSN, full
DOB) so redaction has something real to prove itself against.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Account:
    number: str
    kind: str          # "Savings" | "Checking" | "Certificate"
    balance: float
    status: str = "Open"


@dataclass
class Member:
    member_id: str
    first_name: str
    last_name: str
    ssn: str           # sensitive - must never be persisted downstream
    dob: str           # sensitive
    status: str        # "Active" | "Dormant" | "Restricted"
    branch: str
    accounts: list[Account] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def masked_ssn(self) -> str:
        return f"XXX-XX-{self.ssn[-4:]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SEED: list[Member] = [
    Member("12345", "Dolores", "Abernathy", "412-88-9031", "1971-03-14", "Active", "Sweetwater",
           [Account("0001234501", "Savings", 8421.55),
            Account("0001234502", "Checking", 1290.03)]),
    Member("12346", "Bernard", "Lowe", "501-22-7744", "1969-11-02", "Active", "Escalante",
           [Account("0001234601", "Savings", 152.10)]),
    Member("12347", "Maeve", "Millay", "377-45-1188", "1980-06-23", "Restricted", "Sweetwater",
           [Account("0001234701", "Checking", 44210.87),
            Account("0001234702", "Certificate", 25000.00, "Matured")]),
    Member("12348", "Teddy", "Flood", "298-13-5560", "1985-01-30", "Dormant", "Las Mudas",
           [Account("0001234801", "Savings", 0.00, "Closed")]),
]

_STATE: dict[str, Member] = {}


def reset() -> None:
    """Restore seed state. Called at app start and by the test-control endpoint."""
    global _STATE
    _STATE = {m.member_id: copy.deepcopy(m) for m in _SEED}


def get(member_id: str) -> Member | None:
    return _STATE.get((member_id or "").strip())


def search(term: str) -> list[Member]:
    """Match on member id or a case-insensitive substring of the name."""
    t = (term or "").strip().lower()
    if not t:
        return []
    return [
        m for m in _STATE.values()
        if m.member_id == t or t in m.full_name.lower() or t in m.last_name.lower()
    ]


def next_account_number(m: Member) -> str:
    return f"{int(m.accounts[-1].number) + 1:010d}" if m.accounts else f"{int(m.member_id)}01"


def open_subaccount(member_id: str, kind: str, initial: float) -> Account | None:
    m = get(member_id)
    if m is None:
        return None
    acct = Account(next_account_number(m), kind, initial)
    m.accounts.append(acct)
    return acct


reset()
