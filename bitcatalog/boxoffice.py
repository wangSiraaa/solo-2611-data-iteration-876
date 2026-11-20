"""Ticket-office batch bagging: simultaneous change-bag orders over one stock.

Every bag order claims an exact total face value, a maximum token count and a
set of acceptable face values.  The crucial constraint is *batch-wide*: each
bag may be fillable on its own while the whole batch is not, because the bags
draw from one physical inventory.  :func:`plan_batch` therefore solves all
orders together against the stock counts and returns either one concrete
allocation (specific token serials per bag) or a structured blocker.

Confirmed plans are stored by :class:`BoxOfficeLedger` (SQLite, append-only):
confirmation deducts physical token counts in a single transaction and every
bag line keeps the token serial together with its gauging evidence (product
hash, width, width-table version, source frame ids).  A self-contained JSON
manifest is exported alongside, so an offline computer with only the standard
library can re-check the bag totals and the width basis.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .catalog import fraction_text

MANIFEST_FORMAT = "bitcatalog-bag-manifest/1"
BLOCKER_STOCK = "insufficient_stock"
BLOCKER_DENOMINATION = "denomination_unreachable"
BLOCKER_BATCH = "batch_contention"

_STATUS_AVAILABLE = "available"
_STATUS_BAGGED = "bagged"


class BoxOfficeError(ValueError):
    """An order, plan or ledger operation is invalid."""

    def __init__(self, message: str, *, code: str = "boxoffice_error", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# input model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventoryToken:
    """One physical token and the gauging basis it was admitted with."""

    serial: str
    face_value: int
    width: Fraction
    width_table_version: str
    product_sha256: str
    frame_event_ids: tuple[str, ...]

    @classmethod
    def from_admission(cls, admission: Mapping[str, Any]) -> "InventoryToken":
        if admission.get("status") != "admitted":
            raise BoxOfficeError(
                f"only admitted tokens enter inventory: {admission.get('token_id')}",
                code="not_admitted",
            )
        width = Fraction(str(admission["width"]))
        return cls(
            serial=str(admission["token_id"]),
            face_value=int(admission["face_value"]),
            width=width,
            width_table_version=str(admission["width_table_version"]),
            product_sha256=str(admission["product_sha256"]),
            frame_event_ids=tuple(admission.get("frame_event_ids", ())),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "face_value": self.face_value,
            "width": fraction_text(self.width),
            "width_table_version": self.width_table_version,
            "product_sha256": self.product_sha256,
            "frame_event_ids": list(self.frame_event_ids),
        }


@dataclass(frozen=True)
class BagOrder:
    order_id: str
    amount: int
    max_tokens: int
    accepted_values: frozenset[int]

    @classmethod
    def coerce(cls, raw: Mapping[str, Any]) -> "BagOrder":
        order_id = raw.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise BoxOfficeError("order needs order_id", code="bad_order")
        amount = raw.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise BoxOfficeError(f"{order_id}: amount must be a positive integer", code="bad_order")
        max_tokens = raw.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise BoxOfficeError(f"{order_id}: max_tokens must be a positive integer", code="bad_order")
        values = raw.get("accepted_values")
        if not isinstance(values, (list, tuple)) or not values:
            raise BoxOfficeError(f"{order_id}: accepted_values must be a non-empty list", code="bad_order")
        accepted: set[int] = set()
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise BoxOfficeError(f"{order_id}: face values must be positive integers", code="bad_order")
            accepted.add(value)
        return cls(order_id=order_id, amount=amount, max_tokens=max_tokens, accepted_values=frozenset(accepted))


@dataclass(frozen=True)
class BagLine:
    serial: str
    face_value: int
    width: Fraction
    width_table_version: str
    product_sha256: str
    frame_event_ids: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        out = {
            "serial": self.serial,
            "face_value": self.face_value,
            "width": fraction_text(self.width),
            "width_table_version": self.width_table_version,
            "product_sha256": self.product_sha256,
        }
        if self.frame_event_ids:
            out["frame_event_ids"] = list(self.frame_event_ids)
        return out


@dataclass(frozen=True)
class FilledBag:
    order_id: str
    amount: int
    max_tokens: int
    lines: tuple[BagLine, ...]

    @property
    def total(self) -> int:
        return sum(line.face_value for line in self.lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "amount": self.amount,
            "max_tokens": self.max_tokens,
            "token_count": len(self.lines),
            "total": self.total,
            "lines": [line.to_json() for line in self.lines],
        }


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    order_id: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "order_id": self.order_id,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BatchPlan:
    feasible: bool
    bags: tuple[FilledBag, ...]
    blocker: Blocker | None
    stock_before: Mapping[int, int]
    stock_after: Mapping[int, int]

    def to_json(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "bags": [bag.to_json() for bag in self.bags],
            "blocker": None if self.blocker is None else self.blocker.to_json(),
            "stock_before": {str(k): v for k, v in sorted(self.stock_before.items())},
            "stock_after": {str(k): v for k, v in sorted(self.stock_after.items())},
        }


# ---------------------------------------------------------------------------
# batch solver
# ---------------------------------------------------------------------------


def plan_batch(
    tokens: Iterable[InventoryToken | Mapping[str, Any]],
    orders: Iterable[BagOrder | Mapping[str, Any]],
) -> BatchPlan:
    """Find one allocation that fills every order simultaneously.

    Tokens are fungible within a face value; the solver enumerates denomination
    counts per bag (bounded by ``max_tokens`` and the batch-wide stock) with
    memoised depth-first search, then binds concrete serials deterministically.
    """

    stock_tokens = _coerce_tokens(tokens)
    bag_orders = [order if isinstance(order, BagOrder) else BagOrder.coerce(order) for order in orders]
    if len({order.order_id for order in bag_orders}) != len(bag_orders):
        raise BoxOfficeError("order_id values must be unique", code="bad_order")

    by_value: dict[int, list[InventoryToken]] = {}
    for token in stock_tokens:
        by_value.setdefault(token.face_value, []).append(token)
    for serials in by_value.values():
        serials.sort(key=lambda t: t.serial)
    stock_counts = {value: len(serials) for value, serials in by_value.items()}

    # Per-order impossibility first: the error message must say which bag and
    # whether the denomination itself can never reach the amount.
    for order in bag_orders:
        verdict = _single_order_verdict(order, stock_counts)
        if verdict is not None:
            return BatchPlan(False, (), verdict, dict(stock_counts), dict(stock_counts))

    allocation = _solve(bag_orders, stock_counts)
    if allocation is None:
        # Every bag is individually reachable but the joint claim is not.
        contention = _contention_detail(bag_orders, stock_counts)
        blocker = Blocker(
            code=BLOCKER_BATCH,
            message="each bag is fillable alone but the orders cannot be filled together from one stock",
            order_id=None,
            detail=contention,
        )
        return BatchPlan(False, (), blocker, dict(stock_counts), dict(stock_counts))

    remaining = {value: list(serials) for value, serials in by_value.items()}
    bags: list[FilledBag] = []
    for order, counts in zip(bag_orders, allocation):
        lines: list[BagLine] = []
        for value in sorted(counts):
            for _ in range(counts[value]):
                token = remaining[value].pop(0)
                lines.append(
                    BagLine(
                        serial=token.serial,
                        face_value=token.face_value,
                        width=token.width,
                        width_table_version=token.width_table_version,
                        product_sha256=token.product_sha256,
                        frame_event_ids=token.frame_event_ids,
                    )
                )
        bags.append(
            FilledBag(
                order_id=order.order_id,
                amount=order.amount,
                max_tokens=order.max_tokens,
                lines=tuple(lines),
            )
        )
    stock_after = {value: len(serials) for value, serials in remaining.items()}
    for value, count in stock_counts.items():
        stock_after.setdefault(value, count)
    return BatchPlan(True, tuple(bags), None, dict(stock_counts), stock_after)


def _coerce_tokens(
    tokens: Iterable[InventoryToken | Mapping[str, Any]],
) -> list[InventoryToken]:
    out: list[InventoryToken] = []
    seen: set[str] = set()
    for raw in tokens:
        token = raw if isinstance(raw, InventoryToken) else InventoryToken.from_admission(raw)
        if token.serial in seen:
            raise BoxOfficeError(f"duplicate token serial {token.serial!r}", code="duplicate_token")
        seen.add(token.serial)
        out.append(token)
    return out


def _single_order_verdict(
    order: BagOrder, stock_counts: Mapping[int, int]
) -> Blocker | None:
    usable = sorted(value for value in order.accepted_values if stock_counts.get(value, 0) > 0)
    if not usable:
        return Blocker(
            code=BLOCKER_DENOMINATION,
            message="no accepted face value is in stock",
            order_id=order.order_id,
            detail={"accepted_values": sorted(order.accepted_values)},
        )
    if not _reachable(order.amount, usable, order.max_tokens):
        return Blocker(
            code=BLOCKER_DENOMINATION,
            message=(
                f"amount {order.amount} cannot be made from {sorted(order.accepted_values)} "
                f"within {order.max_tokens} token(s)"
            ),
            order_id=order.order_id,
            detail={
                "accepted_values": sorted(order.accepted_values),
                "in_stock_values": usable,
                "max_tokens": order.max_tokens,
            },
        )
    needed_min = _minimum_tokens(order.amount, usable)
    available = sum(stock_counts.get(value, 0) for value in usable)
    if available < needed_min or not _fillable_with_stock(order.amount, usable, order.max_tokens, stock_counts):
        return Blocker(
            code=BLOCKER_STOCK,
            message="stock of the accepted face values is insufficient for this bag",
            order_id=order.order_id,
            detail={
                "needed_at_least_tokens": needed_min,
                "available_tokens": available,
                "stock": {str(v): stock_counts.get(v, 0) for v in usable},
            },
        )
    return None


def _reachable(amount: int, values: Sequence[int], max_tokens: int) -> bool:
    """Can ``amount`` be made from ``values`` with at most ``max_tokens`` coins?"""

    reachable = {0}
    for _ in range(max_tokens):
        nxt = set(reachable)
        for total in reachable:
            for value in values:
                if total + value <= amount:
                    nxt.add(total + value)
        reachable = nxt
        if amount in reachable:
            return True
    return amount in reachable


def _minimum_tokens(amount: int, values: Sequence[int]) -> int:
    inf = amount + 1
    best = [inf] * (amount + 1)
    best[0] = 0
    for total in range(1, amount + 1):
        for value in values:
            if value <= total and best[total - value] + 1 < best[total]:
                best[total] = best[total - value] + 1
    return best[amount]


def _fillable_with_stock(
    amount: int,
    values: Sequence[int],
    max_tokens: int,
    stock_counts: Mapping[int, int],
) -> bool:
    memo: dict[tuple[int, int, tuple[tuple[int, int], ...]], bool] = {}

    def search(remaining: int, slots: int, stock: tuple[tuple[int, int], ...]) -> bool:
        if remaining == 0:
            return True
        if slots == 0 or remaining < 0:
            return False
        key = (remaining, slots, stock)
        if key in memo:
            return memo[key]
        for i, (value, count) in enumerate(stock):
            if count == 0 or value > remaining:
                continue
            reduced = tuple(
                (v, c - 1 if j == i else c) for j, (v, c) in enumerate(stock)
            )
            if search(remaining - value, slots - 1, reduced):
                memo[key] = True
                return True
        memo[key] = False
        return False

    stock = tuple(sorted((value, stock_counts.get(value, 0)) for value in values))
    return search(amount, max_tokens, stock)


def _solve(
    orders: Sequence[BagOrder], stock_counts: Mapping[int, int]
) -> list[dict[int, int]] | None:
    memo: dict[tuple[int, tuple[tuple[int, int], ...]], bool] = {}

    def search(index: int, stock: dict[int, int]) -> list[dict[int, int]] | None:
        if index == len(orders):
            return []
        key = (index, tuple(sorted(stock.items())))
        if key in memo:
            return None  # known dead end
        order = orders[index]
        values = sorted(order.accepted_values)
        for combo in _count_combos(order.amount, values, order.max_tokens, stock):
            reduced = dict(stock)
            for value, count in combo.items():
                reduced[value] -= count
            rest = search(index + 1, reduced)
            if rest is not None:
                return [combo, *rest]
        memo[key] = False
        return None

    return search(0, dict(stock_counts))


def _count_combos(
    amount: int,
    values: Sequence[int],
    max_tokens: int,
    stock: Mapping[int, int],
) -> list[dict[int, int]]:
    """All denomination-count vectors summing to ``amount`` within the limits."""

    combos: list[dict[int, int]] = []

    def extend(prefix: list[int], remaining: int, slots: int, start: int) -> None:
        if remaining == 0:
            combo: dict[int, int] = {}
            for value, count in prefix:
                if count:
                    combo[value] = count
            combos.append(combo)
            return
        if slots == 0 or start == len(values):
            return
        value = values[start]
        limit = min(slots, remaining // value, stock.get(value, 0))
        for count in range(limit + 1):
            prefix.append((value, count))
            extend(prefix, remaining - count * value, slots - count, start + 1)
            prefix.pop()

    extend([], amount, max_tokens, 0)
    # Prefer fewer-token fills: they leave the most flexibility for later bags.
    combos.sort(key=lambda combo: (sum(combo.values()), tuple(sorted(combo.items()))))
    return combos


def _contention_detail(
    orders: Sequence[BagOrder], stock_counts: Mapping[int, int]
) -> dict[str, Any]:
    demand: dict[int, int] = {}
    for order in orders:
        for value in order.accepted_values:
            demand.setdefault(value, 0)
    over = {}
    for value, count in stock_counts.items():
        claimed = sum(
            min(order.max_tokens, (order.amount + value - 1) // value)
            for order in orders
            if value in order.accepted_values
        )
        if claimed > count:
            over[str(value)] = {"stock": count, "upper_bound_demand": claimed}
    return {
        "orders": [order.order_id for order in orders],
        "stock": {str(k): v for k, v in sorted(stock_counts.items())},
        "contended_values": over,
    }


# ---------------------------------------------------------------------------
# SQLite ledger
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gauging_products (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    width_table_version TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    serial TEXT PRIMARY KEY,
    face_value INTEGER NOT NULL,
    width_text TEXT NOT NULL,
    width_table_version TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES gauging_products(id),
    frame_event_ids_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('available','bagged')),
    bag_id TEXT
);

CREATE TABLE IF NOT EXISTS bag_manifests (
    bag_batch_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bag_lines (
    bag_batch_id TEXT NOT NULL REFERENCES bag_manifests(bag_batch_id),
    order_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    serial TEXT NOT NULL REFERENCES tokens(serial),
    face_value INTEGER NOT NULL,
    width_text TEXT NOT NULL,
    PRIMARY KEY (bag_batch_id, order_id, line_no)
);

CREATE INDEX IF NOT EXISTS ix_tokens_status ON tokens(status, face_value);
CREATE INDEX IF NOT EXISTS ix_bag_lines_serial ON bag_lines(serial);
"""


class BoxOfficeLedger:
    """Append-only stock and bag ledger; confirmations are atomic batches."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BoxOfficeLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def reopen(self) -> "BoxOfficeLedger":
        self.close()
        return BoxOfficeLedger(self.path)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # -- products and stock ---------------------------------------------

    def register_gauging_product(self, product: Any) -> None:
        manifest = product.manifest()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO gauging_products(sha256, width_table_version, manifest_json)"
                " VALUES (?, ?, ?)",
                (
                    product.fingerprint,
                    product.width_table.version,
                    json.dumps(manifest, sort_keys=True, ensure_ascii=False),
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def admit_tokens(self, tokens: Iterable[InventoryToken | Mapping[str, Any]]) -> int:
        added = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for raw in tokens:
                token = raw if isinstance(raw, InventoryToken) else InventoryToken.from_admission(raw)
                product_id = self._product_id(token.product_sha256)
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO tokens(serial, face_value, width_text, width_table_version,"
                    " product_id, frame_event_ids_json, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        token.serial,
                        token.face_value,
                        fraction_text(token.width),
                        token.width_table_version,
                        product_id,
                        json.dumps(list(token.frame_event_ids)),
                        _STATUS_AVAILABLE,
                    ),
                )
                added += cursor.rowcount
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return added

    def _product_id(self, sha256: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM gauging_products WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if row is None:
            raise BoxOfficeError(f"unknown gauging product {sha256}", code="unknown_product")
        return row["id"]

    def available_tokens(self) -> list[InventoryToken]:
        rows = self._conn.execute(
            "SELECT serial, face_value, width_text, t.width_table_version AS width_table_version,"
            " p.sha256 AS sha256,"
            " frame_event_ids_json FROM tokens t JOIN gauging_products p ON p.id = t.product_id"
            " WHERE t.status = ? ORDER BY t.serial",
            (_STATUS_AVAILABLE,),
        ).fetchall()
        return [
            InventoryToken(
                serial=r["serial"],
                face_value=r["face_value"],
                width=Fraction(r["width_text"]),
                width_table_version=r["width_table_version"],
                product_sha256=r["sha256"],
                frame_event_ids=tuple(json.loads(r["frame_event_ids_json"])),
            )
            for r in rows
        ]

    def stock_counts(self) -> dict[int, int]:
        rows = self._conn.execute(
            "SELECT face_value, COUNT(*) AS n FROM tokens WHERE status = ? GROUP BY face_value",
            (_STATUS_AVAILABLE,),
        ).fetchall()
        return {r["face_value"]: r["n"] for r in rows}

    # -- planning against live stock ------------------------------------

    def plan(self, orders: Iterable[BagOrder | Mapping[str, Any]]) -> BatchPlan:
        return plan_batch(self.available_tokens(), orders)

    def confirm(
        self,
        plan: BatchPlan,
        *,
        bag_batch_id: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Deduct every bag in one transaction and persist the manifest."""

        if not plan.feasible:
            raise BoxOfficeError("cannot confirm an infeasible plan", code="infeasible_plan")
        if self._conn.execute(
            "SELECT 1 FROM bag_manifests WHERE bag_batch_id = ?", (bag_batch_id,)
        ).fetchone():
            raise BoxOfficeError(f"bag batch {bag_batch_id!r} already exists", code="duplicate_batch")
        manifest = self._manifest_document(plan, bag_batch_id, created_at)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for bag in plan.bags:
                for line in bag.lines:
                    row = self._conn.execute(
                        "SELECT status FROM tokens WHERE serial = ?", (line.serial,)
                    ).fetchone()
                    if row is None:
                        raise BoxOfficeError(
                            f"token {line.serial!r} is not in the ledger", code="unknown_token"
                        )
                    if row["status"] != _STATUS_AVAILABLE:
                        raise BoxOfficeError(
                            f"token {line.serial!r} is no longer available", code="token_taken"
                        )
            # The manifest row must exist before bag_lines reference it.
            self._conn.execute(
                "INSERT INTO bag_manifests(bag_batch_id, created_at, manifest_json, content_sha256)"
                " VALUES (?, ?, ?, ?)",
                (
                    bag_batch_id,
                    created_at,
                    json.dumps(manifest, sort_keys=True, ensure_ascii=False),
                    manifest["content_sha256"],
                ),
            )
            for bag in plan.bags:
                for line_no, line in enumerate(bag.lines):
                    self._conn.execute(
                        "UPDATE tokens SET status = ?, bag_id = ? WHERE serial = ?",
                        (_STATUS_BAGGED, bag_batch_id, line.serial),
                    )
                    self._conn.execute(
                        "INSERT INTO bag_lines(bag_batch_id, order_id, line_no, serial,"
                        " face_value, width_text) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            bag_batch_id,
                            bag.order_id,
                            line_no,
                            line.serial,
                            line.face_value,
                            fraction_text(line.width),
                        ),
                    )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return manifest

    def _manifest_document(
        self, plan: BatchPlan, bag_batch_id: str, created_at: str
    ) -> dict[str, Any]:
        body = {
            "format": MANIFEST_FORMAT,
            "bag_batch_id": bag_batch_id,
            "created_at": created_at,
            "plan": plan.to_json(),
        }
        body["content_sha256"] = _manifest_sha(body)
        return body

    def manifest(self, bag_batch_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT manifest_json FROM bag_manifests WHERE bag_batch_id = ?",
            (bag_batch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no bag batch {bag_batch_id!r}")
        manifest = json.loads(row["manifest_json"])
        if manifest.get("content_sha256") != _manifest_sha(
            {k: v for k, v in manifest.items() if k != "content_sha256"}
        ):
            raise BoxOfficeError("stored manifest fails its content hash", code="bad_manifest_hash")
        return manifest

    def manifests(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT bag_batch_id FROM bag_manifests ORDER BY created_at, bag_batch_id"
        ).fetchall()
        return [r["bag_batch_id"] for r in rows]

    def export_manifest(self, bag_batch_id: str, path: str | os.PathLike[str]) -> str:
        """Write the self-contained JSON manifest for an offline computer."""

        manifest = self.manifest(bag_batch_id)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return str(target)


def _manifest_sha(body: Mapping[str, Any]) -> str:
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# offline re-check (standard library only, no database)
# ---------------------------------------------------------------------------


def recheck_manifest(
    manifest: Mapping[str, Any],
    *,
    inventory: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify an exported bag manifest without opening the ledger database.

    Checks, per bag: exact amount, max-token limit, unique serials, and (when
    the current inventory/admission records are supplied) that every line still
    matches the physical token's face value and gauging basis.
    """

    if manifest.get("format") != MANIFEST_FORMAT:
        return {"ok": False, "problems": [f"unsupported format {manifest.get('format')!r}"]}
    body = {k: v for k, v in manifest.items() if k != "content_sha256"}
    if _manifest_sha(body) != manifest.get("content_sha256"):
        return {"ok": False, "problems": ["manifest content hash mismatch"]}

    known: dict[str, Mapping[str, Any]] = {}
    if inventory is not None:
        for token in inventory:
            known[token["serial"]] = token

    problems: list[str] = []
    seen_serials: set[str] = set()
    bags = manifest.get("plan", {}).get("bags", [])
    for bag in bags:
        order_id = bag.get("order_id")
        lines = bag.get("lines", [])
        total = sum(line["face_value"] for line in lines)
        if total != bag.get("amount"):
            problems.append(f"{order_id}: total {total} != amount {bag.get('amount')}")
        if len(lines) > bag.get("max_tokens"):
            problems.append(f"{order_id}: {len(lines)} tokens exceed max {bag.get('max_tokens')}")
        for line in lines:
            serial = line["serial"]
            if serial in seen_serials:
                problems.append(f"{order_id}: serial {serial!r} used more than once in the batch")
            seen_serials.add(serial)
            record = known.get(serial)
            if record is not None:
                if record.get("face_value") != line["face_value"]:
                    problems.append(f"{order_id}/{serial}: face value differs from inventory")
                if str(record.get("width")) != str(line.get("width")):
                    problems.append(f"{order_id}/{serial}: width basis differs from inventory")
                if record.get("product_sha256") != line.get("product_sha256"):
                    problems.append(f"{order_id}/{serial}: gauging product differs from inventory")
    return {
        "ok": not problems,
        "bag_batch_id": manifest.get("bag_batch_id"),
        "bags_checked": len(bags),
        "serials_checked": len(seen_serials),
        "problems": problems,
    }
