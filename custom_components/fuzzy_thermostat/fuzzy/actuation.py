"""What to send a wrapped climate device, and whether it actually took it.

A service call returning is not the device holding the value. Some integrations cache
the unit's mode and refuse a setpoint while that cache still says ``off`` -- the Kumo
integration does exactly this, logging a warning and nothing more -- so "set the mode,
then set the temperature" can land the mode and silently lose the setpoint. The unit then
runs at whatever it remembered, while a controller that recorded the *request* as the
*state* reports a setpoint the device never took.

So this keeps two things apart: what was **requested**, and whether the device has
**confirmed** it by reporting it back. And it sends only what is actually wrong:

* mode and setpoint both wrong (or the setpoint unknown, as a unit reports while off):
  one call carrying both, so the integration applies the setpoint to the new mode;
* only the mode wrong: the mode alone -- a unit that already holds the setpoint must not
  get a second command (a second beep) for a value it has;
* only the setpoint wrong: the temperature alone -- the reported mode *is* the
  integration's cached mode, so a bare setpoint is safe.

An unconfirmed request is retried a bounded number of times, keeping the mode in the
retry for as long as the mode is still wrong, and then it becomes a visible fault rather
than a silent loop. A request the device *did* confirm and later left was changed by
someone else -- a remote, another automation -- and is respected until the target moves,
exactly as a person at the unit would expect. Only a power-on edge may set a mode; the
ordinary setpoint path never re-powers or re-modes a unit.

Pure: no Home Assistant imports, so all of this is unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Ledger:
    """What we asked the device for, and whether it has shown it back."""

    requested_setpoint: float | None = None
    # The mode we asked for, only when the request carried one (a power-on edge).
    # None for a plain setpoint request -- that is what lets a retry know whether it
    # is allowed to touch the mode at all.
    requested_mode: str | None = None
    requested_at: datetime | None = None
    confirmed: bool = False
    attempts: int = 0
    fault: str | None = None


@dataclass(frozen=True)
class Command:
    """One climate.set_temperature / set_hvac_mode call. None means leave it out."""

    temperature: float | None
    hvac_mode: str | None


def _near(a: float | None, b: float, step: float) -> bool:
    return a is not None and abs(a - b) < step / 2


def _command(target: float, want_mode: str, *, send_mode: bool, reported_setpoint, step):
    need_temp = not _near(reported_setpoint, target, step)
    if send_mode:
        return Command(temperature=target if need_temp else None, hvac_mode=want_mode)
    return Command(temperature=target, hvac_mode=None) if need_temp else None


def plan(
    ledger: Ledger,
    *,
    target: float,
    want_mode: str,
    reported_mode: str,
    reported_setpoint: float | None,
    step: float,
    now: datetime,
    may_set_mode: bool,
    confirm_after: timedelta,
    max_attempts: int,
) -> tuple[Command | None, Ledger]:
    """Decide the next command (or none) and the ledger that follows from it.

    ``may_set_mode`` is True only on our own power-on edge. ``max_attempts`` counts the
    original command, so 3 means the original and two retries.
    """
    # Whatever we did or did not send, a device that reports the target in our mode is
    # holding it. This is also the only thing that clears a fault.
    if reported_mode == want_mode and _near(reported_setpoint, target, step):
        return None, Ledger(
            requested_setpoint=target, requested_at=ledger.requested_at or now, confirmed=True
        )

    same_request = _near(ledger.requested_setpoint, target, step)

    if not same_request:
        if reported_mode != want_mode and not may_set_mode:
            # Someone else put the unit in another mode (or off). Not ours to change.
            return None, ledger
        send_mode = reported_mode != want_mode
        cmd = _command(target, want_mode, send_mode=send_mode,
                       reported_setpoint=reported_setpoint, step=step)
        return cmd, Ledger(
            requested_setpoint=target,
            requested_mode=want_mode if send_mode else None,
            requested_at=now,
            attempts=1,
        )

    if ledger.confirmed:
        # It held this once and has since been moved. That was a person or another
        # automation, not a lost command: respect it until our target moves.
        return None, ledger

    if ledger.requested_at is not None and now - ledger.requested_at < confirm_after:
        return None, ledger                     # give the device time to report back

    if reported_mode != want_mode and ledger.requested_mode is None:
        # A plain setpoint request, and the unit has since left our mode: an external
        # change, not a lost command.
        return None, ledger

    if ledger.attempts >= max_attempts:
        if ledger.fault is None:
            ledger = replace(
                ledger,
                fault=(
                    f"device reports {reported_mode} / {reported_setpoint} after "
                    f"{ledger.attempts} attempts to set {want_mode} / {target:g}"
                ),
            )
        return None, ledger

    cmd = _command(target, want_mode, send_mode=reported_mode != want_mode,
                   reported_setpoint=reported_setpoint, step=step)
    return cmd, replace(ledger, requested_at=now, attempts=ledger.attempts + 1)
