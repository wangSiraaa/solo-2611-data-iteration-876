"""Belt-motion kinematics used to synthesise physically consistent gate frames.

The gauging engine itself only sees event timestamps; this helper generates the
front-gate edge time for a token edge whose rear-gate time is chosen by hand.
An edge seen at the rear gate at time ``t`` crossed the front gate at the
unique earlier time ``t0`` for which the belt travelled exactly the gate
spacing during ``[t0, t)``.  Because this is the same physical motion at every
gate, tokens constructed this way produce identical widths at both gates --
exactly the property the engine's cross-gate check relies on.

Time is integer nanoseconds.  A speed piece is ``(start_ns, speed_per_ns)`` and
stays in force until the next piece starts; distance travelled is the integral
of speed over the half-open time interval.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Sequence


def speed_at(pieces: Sequence[tuple[int, Fraction]], ns: int) -> Fraction:
    active = Fraction(0)
    for start, speed in pieces:
        if start <= ns:
            active = speed
        else:
            break
    return active


def distance(pieces: Sequence[tuple[int, Fraction]], start_ns: int, end_ns: int) -> Fraction:
    """Belt distance travelled over ``[start_ns, end_ns)``."""

    if end_ns < start_ns:
        raise ValueError("end precedes start")
    bounds = {start_ns, end_ns}
    for start, _ in pieces:
        if start_ns < start < end_ns:
            bounds.add(start)
    ordered = sorted(bounds)
    total = Fraction(0)
    for lo, hi in zip(ordered, ordered[1:]):
        total += speed_at(pieces, lo) * Fraction(hi - lo)
    return total


def travel_end(pieces: Sequence[tuple[int, Fraction]], start_ns: int, target: Fraction) -> int:
    """Smallest ``end_ns`` with ``distance(start_ns, end_ns) == target``.

    The constructed datasets only need targets reached exactly on integer
    nanosecond boundaries; a fractional arrival is an error rather than a
    silent rounding.
    """

    elapsed = 0
    covered = Fraction(0)
    while covered < target:
        speed = speed_at(pieces, start_ns + elapsed)
        if speed <= 0:
            raise ValueError("belt stalled before reaching the target distance")
        remaining = target - covered
        step_ns = remaining / speed
        if step_ns.denominator != 1:
            raise ValueError(
                f"target distance {target} is not reached on an integer ns boundary "
                f"(need {step_ns} ns at speed {speed})"
            )
        elapsed += int(step_ns)
        covered = distance(pieces, start_ns, start_ns + elapsed)
    return start_ns + elapsed


def front_edge_time(
    pieces: Sequence[tuple[int, Fraction]],
    rear_edge_ns: int,
    gate_spacing: Fraction,
) -> int:
    """Front-gate time of the same token edge observed at the rear gate.

    Searches backwards over the piecewise-constant speed line, accumulating
    distance until the gate spacing is covered, then verifies the forward
    integral.
    """

    covered = Fraction(0)
    t = rear_edge_ns
    while covered < gate_spacing:
        t -= 1
        covered += speed_at(pieces, t)
    if covered != gate_spacing:
        raise ValueError(
            f"gate spacing {gate_spacing} cannot be crossed on integer ns boundaries "
            f"ending at {rear_edge_ns} (accumulated {covered})"
        )
    forward = distance(pieces, t, rear_edge_ns)
    if forward != gate_spacing:
        raise ValueError("front-edge inversion failed the forward check")
    return t
