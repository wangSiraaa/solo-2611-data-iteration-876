"""Dual-gate optical gauging: calibration speed line, token widths, published products.

The offline sorting bench's light gates only emit edge timing; the occlusion
duration alone cannot size a token once the belt speed varies.  This module
publishes the *gauging convention* as a content-addressed product and turns the
raw gate frames into calibrated token widths.

Physical model
==============

Two gates (``front`` and ``rear``) are mounted a known distance apart on the
same belt.  Three frame branches exist on every gate:

* ``ruler``    -- a calibrated ruler mark passes the gate,
* ``token``    -- a souvenir token's leading/trailing edge passes the gate,
* ``selfcheck``-- an idle self-check frame that proves the gate is alive but
                  carries no mark and must not reset the speed line.

The product names one gate's edge as the *valid reading edge*.  Ruler marks are
observed in pairs (one frame per gate for the same mark id).  The calibrated
speed of the segment is the known gate spacing divided by that mark's transit
time between the two valid edges::

    speed = gate_spacing / (t_rear_valid - t_front_valid)

A calibration becomes active at the rear-gate event time and stays active until
the next calibration starts, or until its time-to-live (``calibration_ttl_ns``)
expires.  Self-check frames never extend, shorten or replace a calibration; a
calibration that simply has no successor remains valid past self-check frames.

A token's physical width is the integral of belt speed over the occlusion
interval ``[leading_ns, trailing_ns)``.  Time is split at every calibration
boundary inside that interval and each half-open piece contributes
``speed * piece_duration``; :class:`fractions.Fraction` keeps the result
exact.  Any piece without an in-force calibration isolates the token
(``missing_speed``).

Isolation (the token never enters the catalog) also happens on:

* ``mark_conflict``  -- a ruler mark is seen twice on a gate, arrives out of
                        order, lacks one gate, or yields a non-positive transit,
* ``edge_misaligned``-- a gate reports an edge of the wrong direction, a
                        duplicated edge, an inverted pair, or sees only one
                        edge of a token,
* ``late_ruler``     -- a ruler frame arrives after the station watermark and
                        would revise widths already admitted.

Widths are classified by a *versioned* table of mutually exclusive half-open
intervals ``[lo, hi)``; a width fitting no band is isolated
(``unclassified``).  The table is part of the published product, so every
stored width carries a permanently re-checkable gauging basis.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .catalog import as_fraction, fraction_text
from .warehouse import parse_occurred_ns

PROTOCOL = "bitcatalog-gauging/1"
PRODUCT_FORMAT = "bitcatalog-gauging-product/1"

BRANCH_RULER = "ruler"
BRANCH_TOKEN = "token"
BRANCH_SELFCHECK = "selfcheck"
BRANCHES = (BRANCH_RULER, BRANCH_TOKEN, BRANCH_SELFCHECK)

GATE_FRONT = "front"
GATE_REAR = "rear"
GATES = (GATE_FRONT, GATE_REAR)

EDGE_LEADING = "leading"
EDGE_TRAILING = "trailing"
EDGES = (EDGE_LEADING, EDGE_TRAILING)

STATUS_ADMITTED = "admitted"
STATUS_QUARANTINED = "quarantined"


class GaugingError(ValueError):
    """A gauging product or frame document is rejected."""

    def __init__(self, message: str, *, code: str = "gauging_error", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# published product
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateSpec:
    name: str
    role: str  # GATE_FRONT / GATE_REAR
    spacing_from_front: Fraction  # distance of this gate from the front gate

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "spacing_from_front": fraction_text(self.spacing_from_front),
        }


@dataclass(frozen=True)
class WidthBand:
    """One half-open classification interval ``[lo, hi)`` -> face value."""

    face_value: int
    lo: Fraction
    hi: Fraction

    def contains(self, width: Fraction) -> bool:
        return self.lo <= width < self.hi

    def to_json(self) -> dict[str, Any]:
        return {
            "face_value": self.face_value,
            "lo": fraction_text(self.lo),
            "hi": fraction_text(self.hi),
        }


@dataclass(frozen=True)
class WidthTable:
    """Versioned, mutually exclusive half-open width bands."""

    version: str
    bands: tuple[WidthBand, ...]

    def classify(self, width: Fraction) -> WidthBand | None:
        for band in self.bands:
            if band.contains(width):
                return band
        return None

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "bands": [b.to_json() for b in self.bands]}


@dataclass(frozen=True)
class GaugingProduct:
    """Immutable, content-addressed speed/width convention.

    ``selector_map`` gives the integer selector carried by frames of each
    branch when the same frames travel on the bit-field message bus; the
    product can also be materialised as a regular bitcatalog message via
    :meth:`bridge_message`.
    """

    fingerprint: str
    gate_spacing: Fraction
    valid_gate: str
    valid_edge: str
    calibration_ttl_ns: int | None
    width_table: WidthTable
    selector_map: Mapping[str, int]
    gates: tuple[GateSpec, ...]
    content: Mapping[str, Any] = field(default_factory=dict)

    # -- construction ----------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        gate_spacing: Any,
        valid_gate: str = GATE_REAR,
        valid_edge: str = EDGE_LEADING,
        calibration_ttl_ns: int | None = None,
        width_table: Mapping[str, Any] | WidthTable,
        gates: Sequence[Mapping[str, Any] | GateSpec] | None = None,
        selector_map: Mapping[str, int] | None = None,
    ) -> "GaugingProduct":
        spacing = as_fraction(gate_spacing, field_name="gate_spacing")
        if spacing <= 0:
            raise GaugingError("gate_spacing must be positive", code="bad_field")
        if valid_gate not in GATES:
            raise GaugingError(f"valid_gate must be one of {GATES}", code="bad_field")
        if valid_edge not in EDGES:
            raise GaugingError(f"valid_edge must be one of {EDGES}", code="bad_field")
        if calibration_ttl_ns is not None and calibration_ttl_ns <= 0:
            raise GaugingError("calibration_ttl_ns must be positive or null", code="bad_field")

        table = _parse_width_table(width_table)
        gate_specs = _parse_gates(gates, spacing)
        selectors = _parse_selector_map(selector_map)

        content = {
            "protocol": PROTOCOL,
            "gate_spacing": fraction_text(spacing),
            "valid_gate": valid_gate,
            "valid_edge": valid_edge,
            "calibration_ttl_ns": calibration_ttl_ns,
            "width_table": table.to_json(),
            "gates": [g.to_json() for g in gate_specs],
            "selector_map": dict(selectors),
        }
        fingerprint = _content_sha(content)
        return cls(
            fingerprint=fingerprint,
            gate_spacing=spacing,
            valid_gate=valid_gate,
            valid_edge=valid_edge,
            calibration_ttl_ns=calibration_ttl_ns,
            width_table=table,
            selector_map=selectors,
            gates=gate_specs,
            content=content,
        )

    # -- persistence -----------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        return {
            "format": PRODUCT_FORMAT,
            "protocol": PROTOCOL,
            "fingerprint": self.fingerprint,
            "content_sha256": self.fingerprint,
            "content": dict(self.content),
        }

    def save(self, path: str | os.PathLike[str]) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.manifest(), sort_keys=True, ensure_ascii=False, indent=1)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return str(target)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "GaugingProduct":
        text = Path(path).read_text(encoding="utf-8")
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GaugingError(f"product file is not valid JSON: {exc}", code="bad_product") from exc
        if manifest.get("format") != PRODUCT_FORMAT:
            raise GaugingError(
                f"unsupported product format {manifest.get('format')!r}", code="bad_product"
            )
        content = manifest.get("content")
        if not isinstance(content, dict):
            raise GaugingError("product has no embedded content", code="bad_product")
        fingerprint = _content_sha(content)
        declared = manifest.get("content_sha256") or manifest.get("fingerprint")
        if declared != fingerprint:
            raise GaugingError(
                f"product content hash mismatch: declared {declared}, computed {fingerprint}",
                code="bad_product_hash",
            )
        return cls.from_content(content, fingerprint)

    @classmethod
    def from_content(cls, content: Mapping[str, Any], fingerprint: str | None = None) -> "GaugingProduct":
        if content.get("protocol") != PROTOCOL:
            raise GaugingError(
                f"unsupported protocol {content.get('protocol')!r}", code="bad_protocol"
            )
        product = cls.build(
            gate_spacing=content["gate_spacing"],
            valid_gate=content["valid_gate"],
            valid_edge=content["valid_edge"],
            calibration_ttl_ns=content.get("calibration_ttl_ns"),
            width_table=content["width_table"],
            gates=content.get("gates"),
            selector_map=content.get("selector_map"),
        )
        if fingerprint is not None and product.fingerprint != fingerprint:
            raise GaugingError("rebuilt product fails its content hash", code="bad_product_hash")
        return product

    # -- bitcatalog bridge ----------------------------------------------

    def bridge_message(
        self,
        *,
        bus: str = "sorting-bench",
        frame_id: int = 0x6A01,
        payload_bytes: int = 8,
    ) -> dict[str, Any]:
        """Materialise the gauging convention as a bit-field catalog message.

        The selector byte carries the branch selector; ruler/token payload
        fields follow on disjoint branch sets, exactly like any other
        bitcatalog product.  This lets the same frames ride the existing bus
        and warehouse while the gauging engine owns the timing semantics.
        """

        if payload_bytes < 4:
            raise GaugingError("bridge message needs at least 4 payload bytes", code="bad_field")
        sel_width = max(2, max(self.selector_map.values()).bit_length())
        ruler_sel = self.selector_map[BRANCH_RULER]
        token_sel = self.selector_map[BRANCH_TOKEN]
        self_sel = self.selector_map[BRANCH_SELFCHECK]
        return {
            "bus": bus,
            "frame_id": frame_id,
            "payload_bytes": payload_bytes,
            "selector": {"name": "branch", "start": 0, "width": sel_width, "order": "little"},
            "signals": [
                {
                    "name": "ruler_mark",
                    "start": sel_width,
                    "width": min(30, payload_bytes * 8 - sel_width),
                    "order": "little",
                    "signed": False,
                    "factor": "1",
                    "offset": "0",
                    "branches": [ruler_sel],
                },
                {
                    "name": "token_ref",
                    "start": sel_width,
                    "width": min(30, payload_bytes * 8 - sel_width),
                    "order": "little",
                    "signed": False,
                    "factor": "1",
                    "offset": "0",
                    "branches": [token_sel],
                },
                {
                    "name": "selfcheck_seq",
                    "start": sel_width,
                    "width": min(30, payload_bytes * 8 - sel_width),
                    "order": "little",
                    "signed": False,
                    "factor": "1",
                    "offset": "0",
                    "branches": [self_sel],
                },
            ],
        }


def _parse_width_table(raw: Mapping[str, Any] | WidthTable) -> WidthTable:
    if isinstance(raw, WidthTable):
        return raw
    if not isinstance(raw, Mapping) or "version" not in raw:
        raise GaugingError("width_table needs a version and bands", code="bad_field")
    version = str(raw["version"])
    raw_bands = raw.get("bands")
    if not isinstance(raw_bands, list) or not raw_bands:
        raise GaugingError("width_table needs at least one band", code="bad_field")
    bands: list[WidthBand] = []
    for item in raw_bands:
        face = item.get("face_value")
        if isinstance(face, bool) or not isinstance(face, int) or face <= 0:
            raise GaugingError("band face_value must be a positive integer", code="bad_field")
        lo = as_fraction(item.get("lo"), field_name="band.lo")
        hi = as_fraction(item.get("hi"), field_name="band.hi")
        if not lo < hi:
            raise GaugingError(f"band {face}: need lo < hi", code="bad_field")
        bands.append(WidthBand(face_value=face, lo=lo, hi=hi))
    # Mutual exclusion proof over the half-open bands.
    ordered = sorted(bands, key=lambda b: b.lo)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.lo < prev.hi:
            raise GaugingError(
                f"width bands overlap: [{fraction_text(prev.lo)}, {fraction_text(prev.hi)}) "
                f"vs [{fraction_text(nxt.lo)}, {fraction_text(nxt.hi)})",
                code="band_overlap",
            )
    if len({b.face_value for b in bands}) != len(bands):
        raise GaugingError("each face value may define only one band", code="bad_field")
    return WidthTable(version=version, bands=tuple(ordered))


def _parse_gates(
    raw: Sequence[Mapping[str, Any] | GateSpec] | None, spacing: Fraction
) -> tuple[GateSpec, ...]:
    if raw is None:
        return (
            GateSpec(GATE_FRONT, GATE_FRONT, Fraction(0)),
            GateSpec(GATE_REAR, GATE_REAR, spacing),
        )
    gates: list[GateSpec] = []
    for item in raw:
        if isinstance(item, GateSpec):
            gates.append(item)
            continue
        name = item.get("name")
        role = item.get("role", name)
        if role not in GATES:
            raise GaugingError(f"gate role must be one of {GATES}", code="bad_field")
        distance = as_fraction(item.get("spacing_from_front", 0), field_name="gate.spacing_from_front")
        if distance < 0:
            raise GaugingError("gate spacing_from_front must be >= 0", code="bad_field")
        gates.append(GateSpec(str(name), role, distance))
    roles = {g.role for g in gates}
    if roles != set(GATES):
        raise GaugingError(f"product must declare both gates {GATES}", code="bad_field")
    front = next(g for g in gates if g.role == GATE_FRONT)
    rear = next(g for g in gates if g.role == GATE_REAR)
    if rear.spacing_from_front - front.spacing_from_front != spacing:
        raise GaugingError("rear/front gate distance must equal gate_spacing", code="bad_field")
    return tuple(sorted(gates, key=lambda g: g.spacing_from_front))


def _parse_selector_map(raw: Mapping[str, int] | None) -> dict[str, int]:
    selectors = dict(raw) if raw else {BRANCH_RULER: 0, BRANCH_TOKEN: 1, BRANCH_SELFCHECK: 2}
    missing = set(BRANCHES) - set(selectors)
    if missing:
        raise GaugingError(f"selector_map missing branches {sorted(missing)}", code="bad_field")
    values = list(selectors.values())
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        raise GaugingError("selector values must be non-negative integers", code="bad_field")
    if len(set(values)) != len(values):
        raise GaugingError("selector values must be distinct per branch", code="bad_field")
    return {branch: selectors[branch] for branch in BRANCHES}


def _content_sha(content: Mapping[str, Any]) -> str:
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GaugeFrame:
    frame_event_id: str
    occurred_at_ns: int
    gate: str
    branch: str
    edge: str | None
    mark_id: str | None
    token_id: str | None
    gate_seq: int

    @classmethod
    def coerce(cls, raw: Mapping[str, Any] | "GaugeFrame") -> "GaugeFrame":
        if isinstance(raw, GaugeFrame):
            return raw
        if not isinstance(raw, Mapping):
            raise GaugingError("frame must be a mapping", code="bad_frame")
        event_id = raw.get("frame_event_id")
        if not isinstance(event_id, str) or not event_id:
            raise GaugingError("frame needs frame_event_id", code="bad_frame")
        gate = raw.get("gate")
        if gate not in GATES:
            raise GaugingError(f"{event_id}: gate must be one of {GATES}", code="bad_frame")
        branch = raw.get("branch")
        if branch is None and "selector" in raw:
            branch = _branch_from_selector(raw["selector"])
        if branch not in BRANCHES:
            raise GaugingError(
                f"{event_id}: branch must be one of {BRANCHES}", code="bad_frame"
            )
        edge = raw.get("edge")
        if edge is not None and edge not in EDGES:
            raise GaugingError(f"{event_id}: edge must be one of {EDGES}", code="bad_frame")
        if branch == BRANCH_TOKEN and edge not in EDGES:
            raise GaugingError(f"{event_id}: token frame needs an edge", code="bad_frame")
        mark_id = raw.get("mark_id")
        if branch == BRANCH_RULER and (not isinstance(mark_id, str) or not mark_id):
            raise GaugingError(f"{event_id}: ruler frame needs a mark_id", code="bad_frame")
        token_id = raw.get("token_id")
        if branch == BRANCH_TOKEN and (not isinstance(token_id, str) or not token_id):
            raise GaugingError(f"{event_id}: token frame needs a token_id", code="bad_frame")
        try:
            ns = parse_occurred_ns(raw.get("occurred_at_ns", raw.get("occurred_at")))
        except ValueError as exc:
            raise GaugingError(f"{event_id}: {exc}", code="bad_time") from exc
        gate_seq = raw.get("gate_seq")
        if gate_seq is not None and (isinstance(gate_seq, bool) or not isinstance(gate_seq, int)):
            raise GaugingError(f"{event_id}: gate_seq must be an integer", code="bad_frame")
        return cls(
            frame_event_id=event_id,
            occurred_at_ns=ns,
            gate=gate,
            branch=branch,
            edge=edge,
            mark_id=mark_id if isinstance(mark_id, str) else None,
            token_id=token_id if isinstance(token_id, str) else None,
            gate_seq=gate_seq if isinstance(gate_seq, int) else 0,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "frame_event_id": self.frame_event_id,
            "occurred_at_ns": self.occurred_at_ns,
            "gate": self.gate,
            "branch": self.branch,
        }
        if self.edge is not None:
            out["edge"] = self.edge
        if self.mark_id is not None:
            out["mark_id"] = self.mark_id
        if self.token_id is not None:
            out["token_id"] = self.token_id
        if self.gate_seq:
            out["gate_seq"] = self.gate_seq
        return out


def _branch_from_selector(selector: Any) -> str | None:
    if not isinstance(selector, int) or isinstance(selector, bool):
        return None
    for branch, value in _DEFAULT_SELECTOR.items():
        if value == selector:
            return branch
    return None


_DEFAULT_SELECTOR = {BRANCH_RULER: 0, BRANCH_TOKEN: 1, BRANCH_SELFCHECK: 2}


def frames_hash(frames: Iterable[GaugeFrame | Mapping[str, Any]]) -> str:
    """Stable hash over the frame set in event-time order (audit identity)."""

    coerced = [f if isinstance(f, GaugeFrame) else GaugeFrame.coerce(f) for f in frames]
    ordered = sorted(coerced, key=lambda f: (f.occurred_at_ns, f.frame_event_id))
    blob = "\n".join(json.dumps(f.to_json(), sort_keys=True) for f in ordered).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# calibration line and width integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Calibration:
    mark_id: str
    start_ns: int  # active from the rear-gate valid-edge event time
    speed: Fraction
    ttl_end_ns: int | None
    front_event_id: str
    rear_event_id: str
    front_valid_ns: int
    rear_valid_ns: int

    def in_force(self, ns: int) -> bool:
        if ns < self.start_ns:
            return False
        return self.ttl_end_ns is None or ns < self.ttl_end_ns

    def to_json(self) -> dict[str, Any]:
        return {
            "mark_id": self.mark_id,
            "start_ns": self.start_ns,
            "speed": fraction_text(self.speed),
            "ttl_end_ns": self.ttl_end_ns,
            "front_event_id": self.front_event_id,
            "rear_event_id": self.rear_event_id,
            "transit_ns": self.rear_valid_ns - self.front_valid_ns,
        }


@dataclass(frozen=True)
class WidthSegment:
    start_ns: int
    end_ns: int
    speed: Fraction
    calibration_mark: str

    def to_json(self) -> dict[str, Any]:
        return {
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "duration_ns": self.end_ns - self.start_ns,
            "speed": fraction_text(self.speed),
            "calibration_mark": self.calibration_mark,
        }


@dataclass(frozen=True)
class TokenAdmission:
    token_id: str
    status: str
    width: Fraction | None
    face_value: int | None
    width_table_version: str | None
    gate: str
    leading_ns: int | None
    trailing_ns: int | None
    segments: tuple[WidthSegment, ...]
    reason: str | None
    frame_event_ids: tuple[str, ...]
    product_sha256: str

    def to_json(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "status": self.status,
            "width": None if self.width is None else fraction_text(self.width),
            "face_value": self.face_value,
            "width_table_version": self.width_table_version,
            "gate": self.gate,
            "leading_ns": self.leading_ns,
            "trailing_ns": self.trailing_ns,
            "segments": [s.to_json() for s in self.segments],
            "reason": self.reason,
            "frame_event_ids": list(self.frame_event_ids),
            "product_sha256": self.product_sha256,
        }


@dataclass(frozen=True)
class MarkConflict:
    mark_id: str
    reason: str
    frame_event_ids: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "mark_id": self.mark_id,
            "reason": self.reason,
            "frame_event_ids": list(self.frame_event_ids),
        }


@dataclass(frozen=True)
class GaugingReport:
    product_sha256: str
    frames_sha256: str
    valid_gate: str
    valid_edge: str
    calibrations: tuple[Calibration, ...]
    admissions: tuple[TokenAdmission, ...]
    mark_conflicts: tuple[MarkConflict, ...]
    rejected_frames: tuple[dict[str, Any], ...]
    late_frames: tuple[str, ...]

    def admitted(self) -> tuple[TokenAdmission, ...]:
        return tuple(a for a in self.admissions if a.status == STATUS_ADMITTED)

    def quarantined(self) -> tuple[TokenAdmission, ...]:
        return tuple(a for a in self.admissions if a.status == STATUS_QUARANTINED)

    def inventory(self) -> tuple[dict[str, Any], ...]:
        return [a.to_json() for a in self.admitted()]

    def to_json(self) -> dict[str, Any]:
        return {
            "product_sha256": self.product_sha256,
            "frames_sha256": self.frames_sha256,
            "valid_gate": self.valid_gate,
            "valid_edge": self.valid_edge,
            "calibrations": [c.to_json() for c in self.calibrations],
            "admissions": [a.to_json() for a in self.admissions],
            "mark_conflicts": [c.to_json() for c in self.mark_conflicts],
            "rejected_frames": list(self.rejected_frames),
            "late_frames": list(self.late_frames),
        }


# ---------------------------------------------------------------------------
# the station
# ---------------------------------------------------------------------------


class GaugingStation:
    """Processes gate frames against one immutable :class:`GaugingProduct`.

    A station keeps a watermark across ingest calls: frames older than the
    watermark are *late*.  Late ruler frames are quarantined rather than
    silently rewriting widths already handed to the catalog (``late_ruler``);
    late token/self-check frames are reported but never change admissions.
    """

    def __init__(self, product: GaugingProduct):
        self.product = product
        self._watermark_ns: int | None = None
        self._admitted: dict[str, TokenAdmission] = {}

    @property
    def watermark_ns(self) -> int | None:
        return self._watermark_ns

    def process(
        self, frames: Iterable[GaugeFrame | Mapping[str, Any]]
    ) -> GaugingReport:
        accepted: list[GaugeFrame] = []
        rejected: list[dict[str, Any]] = []
        for raw in frames:
            try:
                accepted.append(GaugeFrame.coerce(raw))
            except GaugingError as exc:
                rejected.append(
                    {
                        "frame_event_id": getattr(raw, "frame_event_id", None)
                        if isinstance(raw, Mapping)
                        else None,
                        "code": exc.code,
                        "detail": exc.detail,
                        "message": str(exc),
                    }
                )
        accepted.sort(key=lambda f: (f.occurred_at_ns, f.frame_event_id))
        late: list[str] = []
        if self._watermark_ns is not None:
            timely: list[GaugeFrame] = []
            for frame in accepted:
                if frame.occurred_at_ns < self._watermark_ns:
                    late.append(frame.frame_event_id)
                    if frame.branch != BRANCH_RULER:
                        continue
                    # A late ruler must never revise an existing admission.
                    continue
                timely.append(frame)
            accepted = timely
        if accepted:
            self._watermark_ns = max(
                accepted[-1].occurred_at_ns, self._watermark_ns or 0
            )

        conflicts, calibrations = self._build_calibrations(accepted)
        admissions = self._build_admissions(accepted, calibrations, conflicts)
        finalised: list[TokenAdmission] = []
        for admission in admissions:
            # The one-physical-token-one-admission rule takes precedence over
            # every other verdict: a serial already in the catalog can never
            # enter again, even if the new frames would otherwise be rejected.
            if admission.token_id in self._admitted:
                admission = TokenAdmission(
                    token_id=admission.token_id,
                    status=STATUS_QUARANTINED,
                    width=admission.width,
                    face_value=None,
                    width_table_version=None,
                    gate=admission.gate,
                    leading_ns=admission.leading_ns,
                    trailing_ns=admission.trailing_ns,
                    segments=admission.segments,
                    reason="duplicate_token",
                    frame_event_ids=admission.frame_event_ids,
                    product_sha256=self.product.fingerprint,
                )
            elif admission.status == STATUS_ADMITTED:
                self._admitted[admission.token_id] = admission
            finalised.append(admission)
        return GaugingReport(
            product_sha256=self.product.fingerprint,
            frames_sha256=frames_hash(accepted),
            valid_gate=self.product.valid_gate,
            valid_edge=self.product.valid_edge,
            calibrations=tuple(calibrations),
            admissions=tuple(finalised),
            mark_conflicts=tuple(conflicts.values()),
            rejected_frames=tuple(rejected),
            late_frames=tuple(late),
        )

    # -- calibrations ----------------------------------------------------

    def _build_calibrations(
        self, frames: list[GaugeFrame]
    ) -> tuple[dict[str, MarkConflict], list[Calibration]]:
        ruler: dict[str, dict[str, GaugeFrame]] = {}
        conflicts: dict[str, MarkConflict] = {}
        for frame in frames:
            if frame.branch != BRANCH_RULER:
                continue
            mark = frame.mark_id
            assert mark is not None
            gate_obs = ruler.setdefault(mark, {})
            if frame.gate in gate_obs:
                conflicts.setdefault(
                    mark,
                    MarkConflict(
                        mark_id=mark,
                        reason="duplicate_gate_observation",
                        frame_event_ids=(
                            gate_obs[frame.gate].frame_event_id,
                            frame.frame_event_id,
                        ),
                    ),
                )
                continue
            gate_obs[frame.gate] = frame

        calibrations: list[Calibration] = []
        for mark, gate_obs in ruler.items():
            if mark in conflicts:
                continue
            if GATE_FRONT not in gate_obs or GATE_REAR not in gate_obs:
                seen = tuple(sorted(f.frame_event_id for f in gate_obs.values()))
                conflicts[mark] = MarkConflict(
                    mark_id=mark, reason="single_gate_observation", frame_event_ids=seen
                )
                continue
            front = gate_obs[GATE_FRONT]
            rear = gate_obs[GATE_REAR]
            transit = rear.occurred_at_ns - front.occurred_at_ns
            if transit <= 0:
                conflicts[mark] = MarkConflict(
                    mark_id=mark,
                    reason="non_positive_transit",
                    frame_event_ids=(front.frame_event_id, rear.frame_event_id),
                )
                continue
            speed = self.product.gate_spacing / Fraction(transit)
            ttl_end = (
                rear.occurred_at_ns + self.product.calibration_ttl_ns
                if self.product.calibration_ttl_ns is not None
                else None
            )
            calibrations.append(
                Calibration(
                    mark_id=mark,
                    start_ns=rear.occurred_at_ns,
                    speed=speed,
                    ttl_end_ns=ttl_end,
                    front_event_id=front.frame_event_id,
                    rear_event_id=rear.frame_event_id,
                    front_valid_ns=front.occurred_at_ns,
                    rear_valid_ns=rear.occurred_at_ns,
                )
            )
        # Out-of-order ruler marks (arrival order disagrees with event time)
        # cannot define a monotone speed line.
        calibrations.sort(key=lambda c: c.start_ns)
        monotone: list[Calibration] = []
        for cal in calibrations:
            if monotone and cal.start_ns < monotone[-1].start_ns:
                conflicts[cal.mark_id] = MarkConflict(
                    mark_id=cal.mark_id,
                    reason="out_of_order_mark",
                    frame_event_ids=(cal.front_event_id, cal.rear_event_id),
                )
                continue
            monotone.append(cal)
        return conflicts, monotone

    # -- token widths ----------------------------------------------------

    def _build_admissions(
        self,
        frames: list[GaugeFrame],
        calibrations: list[Calibration],
        conflicts: Mapping[str, MarkConflict],
    ) -> list[TokenAdmission]:
        groups: dict[str, dict[str, list[GaugeFrame]]] = {}
        for frame in frames:
            if frame.branch == BRANCH_TOKEN:
                groups.setdefault(frame.token_id, {}).setdefault(frame.gate, []).append(frame)

        admissions: list[TokenAdmission] = []
        for token_id, per_gate in sorted(groups.items()):
            # One physical token => one admission; the valid reading gate is
            # authoritative and the other gate is cross-checked evidence.
            gate_results = {
                gate: self._admit_one_gate(token_id, gate, edges, calibrations)
                for gate, edges in per_gate.items()
            }
            primary_gate = self.product.valid_gate if self.product.valid_gate in gate_results else next(iter(gate_results))
            primary = gate_results[primary_gate]
            event_ids = tuple(
                sorted({fid for adm in gate_results.values() for fid in adm.frame_event_ids})
            )
            bad = next(
                (adm for adm in gate_results.values() if adm.status == STATUS_QUARANTINED),
                None,
            )
            if bad is not None:
                chosen = primary if primary.status == STATUS_QUARANTINED else bad
                admissions.append(
                    TokenAdmission(
                        token_id=token_id,
                        status=STATUS_QUARANTINED,
                        width=primary.width,
                        face_value=None,
                        width_table_version=None,
                        gate=primary_gate,
                        leading_ns=primary.leading_ns,
                        trailing_ns=primary.trailing_ns,
                        segments=primary.segments,
                        reason=chosen.reason,
                        frame_event_ids=event_ids,
                        product_sha256=self.product.fingerprint,
                    )
                )
                continue
            if any(adm.width != primary.width for adm in gate_results.values()):
                admissions.append(
                    TokenAdmission(
                        token_id=token_id,
                        status=STATUS_QUARANTINED,
                        width=primary.width,
                        face_value=None,
                        width_table_version=None,
                        gate=primary_gate,
                        leading_ns=primary.leading_ns,
                        trailing_ns=primary.trailing_ns,
                        segments=primary.segments,
                        reason="edge_misaligned",
                        frame_event_ids=event_ids,
                        product_sha256=self.product.fingerprint,
                    )
                )
                continue
            admissions.append(
                TokenAdmission(
                    token_id=token_id,
                    status=STATUS_ADMITTED,
                    width=primary.width,
                    face_value=primary.face_value,
                    width_table_version=primary.width_table_version,
                    gate=primary_gate,
                    leading_ns=primary.leading_ns,
                    trailing_ns=primary.trailing_ns,
                    segments=primary.segments,
                    reason=None,
                    frame_event_ids=event_ids,
                    product_sha256=self.product.fingerprint,
                )
            )

        # A ruler conflict anywhere on the line isolates tokens whose
        # occlusion interval touches the conflicting mark's gate events;
        # the conflict takes precedence over the token's own verdict.
        touched = self._tokens_touching_conflicts(frames, conflicts)
        for i, admission in enumerate(admissions):
            if admission.token_id in touched:
                admissions[i] = TokenAdmission(
                    token_id=admission.token_id,
                    status=STATUS_QUARANTINED,
                    width=admission.width,
                    face_value=None,
                    width_table_version=None,
                    gate=admission.gate,
                    leading_ns=admission.leading_ns,
                    trailing_ns=admission.trailing_ns,
                    segments=admission.segments,
                    reason="mark_conflict",
                    frame_event_ids=admission.frame_event_ids,
                    product_sha256=self.product.fingerprint,
                )
        return admissions

    def _admit_one_gate(
        self,
        token_id: str,
        gate: str,
        edges: list[GaugeFrame],
        calibrations: Sequence[Calibration],
    ) -> TokenAdmission:
        event_ids = tuple(f.frame_event_id for f in edges)
        edge_pair = self._valid_edge_pair(edges)
        if isinstance(edge_pair, MarkConflict):
            return self._quarantine(token_id, gate, event_ids, edge_pair.reason, edges)
        leading_ns, trailing_ns = edge_pair
        if leading_ns >= trailing_ns:
            return self._quarantine(token_id, gate, event_ids, "edge_misaligned", edges)
        segments, missing = self._integrate(leading_ns, trailing_ns, calibrations)
        if missing is not None:
            return self._quarantine(
                token_id,
                gate,
                event_ids,
                "missing_speed",
                edges,
                segments=segments,
                leading_ns=leading_ns,
                trailing_ns=trailing_ns,
            )
        width = sum(
            (seg.speed * Fraction(seg.end_ns - seg.start_ns) for seg in segments),
            Fraction(0),
        )
        band = self.product.width_table.classify(width)
        if band is None:
            return self._quarantine(
                token_id,
                gate,
                event_ids,
                "unclassified",
                edges,
                segments=segments,
                width=width,
                leading_ns=leading_ns,
                trailing_ns=trailing_ns,
            )
        return TokenAdmission(
            token_id=token_id,
            status=STATUS_ADMITTED,
            width=width,
            face_value=band.face_value,
            width_table_version=self.product.width_table.version,
            gate=gate,
            leading_ns=leading_ns,
            trailing_ns=trailing_ns,
            segments=tuple(segments),
            reason=None,
            frame_event_ids=event_ids,
            product_sha256=self.product.fingerprint,
        )

    def _valid_edge_pair(
        self, edges: list[GaugeFrame]
    ) -> tuple[int, int] | MarkConflict:
        seen: dict[str, GaugeFrame] = {}
        for frame in edges:
            assert frame.edge is not None
            if frame.edge in seen:
                return MarkConflict(
                    mark_id=frame.token_id,
                    reason="edge_misaligned",
                    frame_event_ids=(seen[frame.edge].frame_event_id, frame.frame_event_id),
                )
            seen[frame.edge] = frame
        if EDGE_LEADING not in seen or EDGE_TRAILING not in seen:
            return MarkConflict(
                mark_id=edges[0].token_id,
                reason="edge_misaligned",
                frame_event_ids=tuple(f.frame_event_id for f in edges),
            )
        return seen[EDGE_LEADING].occurred_at_ns, seen[EDGE_TRAILING].occurred_at_ns

    def _integrate(
        self,
        leading_ns: int,
        trailing_ns: int,
        calibrations: Sequence[Calibration],
    ) -> tuple[list[WidthSegment], int | None]:
        """Split ``[leading, trailing)`` at calibration boundaries.

        Returns the covered segments and, if any piece has no in-force
        calibration, the first uncovered time point (the ``missing_speed``
        witness).
        """

        boundaries = {leading_ns, trailing_ns}
        for cal in calibrations:
            if leading_ns < cal.start_ns < trailing_ns:
                boundaries.add(cal.start_ns)
            if cal.ttl_end_ns is not None and leading_ns < cal.ttl_end_ns < trailing_ns:
                boundaries.add(cal.ttl_end_ns)
        pieces = sorted(boundaries)
        segments: list[WidthSegment] = []
        for start, end in zip(pieces, pieces[1:]):
            cal = self._calibration_at(start, calibrations)
            if cal is None:
                return segments, start
            segments.append(
                WidthSegment(
                    start_ns=start,
                    end_ns=end,
                    speed=cal.speed,
                    calibration_mark=cal.mark_id,
                )
            )
        return segments, None

    def _calibration_at(
        self, ns: int, calibrations: Sequence[Calibration]
    ) -> Calibration | None:
        active = [cal for cal in calibrations if cal.start_ns <= ns and cal.in_force(ns)]
        if not active:
            return None
        return max(active, key=lambda cal: cal.start_ns)

    def _tokens_touching_conflicts(
        self, frames: list[GaugeFrame], conflicts: Mapping[str, MarkConflict]
    ) -> set[str]:
        if not conflicts:
            return set()
        conflict_event_ids = {
            fid for conflict in conflicts.values() for fid in conflict.frame_event_ids
        }
        conflict_times = [
            f.occurred_at_ns
            for f in frames
            if f.frame_event_id in conflict_event_ids
        ]
        if not conflict_times:
            return set()
        lo, hi = min(conflict_times), max(conflict_times)
        touched: set[str] = set()
        for frame in frames:
            if frame.branch != BRANCH_TOKEN:
                continue
            if lo <= frame.occurred_at_ns <= hi:
                touched.add(frame.token_id)
        return touched

    def _quarantine(
        self,
        token_id: str,
        gate: str,
        event_ids: tuple[str, ...],
        reason: str,
        edges: list[GaugeFrame],
        *,
        segments: list[WidthSegment] | None = None,
        width: Fraction | None = None,
        leading_ns: int | None = None,
        trailing_ns: int | None = None,
    ) -> TokenAdmission:
        edge_ns = {f.edge: f.occurred_at_ns for f in edges}
        return TokenAdmission(
            token_id=token_id,
            status=STATUS_QUARANTINED,
            width=width,
            face_value=None,
            width_table_version=None,
            gate=gate,
            leading_ns=edge_ns.get(EDGE_LEADING, leading_ns),
            trailing_ns=edge_ns.get(EDGE_TRAILING, trailing_ns),
            segments=tuple(segments or ()),
            reason=reason,
            frame_event_ids=event_ids,
            product_sha256=self.product.fingerprint,
        )


def recheck_admission(
    product: GaugingProduct,
    frames: Iterable[GaugeFrame | Mapping[str, Any]],
    admission: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-derive one stored admission from its original frames on a clean box.

    The admission's ``product_sha256`` must match the product in hand and every
    cited frame must be present.  Returns a verdict with the recomputed width,
    face value and per-segment basis; this is the offline audit entry point and
    deliberately has no database dependency.
    """

    if admission.get("product_sha256") != product.fingerprint:
        return {
            "token_id": admission.get("token_id"),
            "ok": False,
            "reason": "product_mismatch",
            "expected_sha256": admission.get("product_sha256"),
            "actual_sha256": product.fingerprint,
        }
    wanted = set(admission.get("frame_event_ids", ()))
    station = GaugingStation(product)
    report = station.process(list(frames))
    found = next(
        (a for a in report.admissions if a.token_id == admission.get("token_id")),
        None,
    )
    if found is None:
        return {"token_id": admission.get("token_id"), "ok": False, "reason": "token_missing", "cited": sorted(wanted)}
    width_text = None if found.width is None else fraction_text(found.width)
    matches = (
        found.status == STATUS_ADMITTED
        and width_text == admission.get("width")
        and found.face_value == admission.get("face_value")
        and [f for f in found.frame_event_ids] == list(admission.get("frame_event_ids", ()))
    )
    return {
        "token_id": found.token_id,
        "ok": matches,
        "reason": None if matches else "width_basis_changed",
        "status": found.status,
        "recomputed_width": width_text,
        "stored_width": admission.get("width"),
        "recomputed_face_value": found.face_value,
        "stored_face_value": admission.get("face_value"),
        "segments": [s.to_json() for s in found.segments],
        "frame_event_ids": list(found.frame_event_ids),
        "frames_sha256": report.frames_sha256,
    }
