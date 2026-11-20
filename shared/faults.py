"""Thread-safe, process-local leases for bounded fault simulation.

This module has no service, telemetry, or database integration. A caller must
explicitly consult the lease manager before applying any fault behavior.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from typing import Callable
from uuid import uuid4


MAX_LEASE_SECONDS = 60.0

ALLOWED_SERVICE_IDS = frozenset({
    "api-gateway",
    "user-service",
    "order-service",
    "database-service",
})

FAULT_MODES_BY_SERVICE = MappingProxyType({
    "api-gateway": frozenset({"latency_injection", "service_unavailable"}),
    "user-service": frozenset({"latency_injection", "service_unavailable"}),
    "order-service": frozenset({"latency_injection", "service_unavailable"}),
    "database-service": frozenset({
        "database_connection_failure",
        "latency_injection",
        "service_unavailable",
    }),
})

_active_fault_manager: "FaultLeaseManager | None" = None


def set_active_fault_manager(manager: "FaultLeaseManager | None") -> None:
    """Set the fault manager owned by this service process."""
    global _active_fault_manager
    _active_fault_manager = manager


def get_active_fault_manager() -> "FaultLeaseManager | None":
    """Return this process's service-local fault manager, if initialized."""
    return _active_fault_manager


class FaultLeaseError(ValueError):
    """Base exception for invalid fault lease operations."""


class InvalidFaultTarget(FaultLeaseError):
    """Raised when a target is not one of the approved simulated services."""


class InvalidFaultMode(FaultLeaseError):
    """Raised when a mode is unknown or disallowed for the selected service."""


class InvalidLeaseDuration(FaultLeaseError):
    """Raised when a lease duration is not positive and at most 60 seconds."""


class FaultLeaseConflict(RuntimeError):
    """Raised when a service already has a non-expired fault lease."""


@dataclass(frozen=True, slots=True)
class FaultLease:
    """Immutable fault lease data; expiry uses a monotonic-clock deadline."""

    lease_id: str
    target: str
    mode: str
    duration_seconds: float
    _expires_at: float = field(repr=False)


class FaultLeaseManager:
    """Manage at most one automatically expiring fault lease per service.

    The manager is local to its process. All reads, starts, stops, and expiry
    cleanup are synchronized so concurrent callers observe consistent state.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = RLock()
        self._leases: dict[str, FaultLease] = {}

    def start(self, target: str, mode: str, duration_seconds: int | float) -> FaultLease:
        """Start a validated lease, rejecting conflicts and invalid inputs."""
        self._validate_target(target)
        self._validate_mode(target, mode)
        duration = self._validate_duration(duration_seconds)

        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            if target in self._leases:
                raise FaultLeaseConflict(f"A fault lease is already active for {target}")

            lease = FaultLease(
                lease_id=str(uuid4()),
                target=target,
                mode=mode,
                duration_seconds=duration,
                _expires_at=now + duration,
            )
            self._leases[target] = lease
            return lease

    def stop(self, target: str, lease_id: str | None = None) -> bool:
        """Stop the target's lease immediately.

        Supplying ``lease_id`` protects a newer lease from a delayed stop
        request for an older lease. Returns false when no matching lease is
        active.
        """
        self._validate_target(target)
        with self._lock:
            self._expire_locked(self._clock())
            lease = self._leases.get(target)
            if lease is None or (lease_id is not None and lease.lease_id != lease_id):
                return False
            del self._leases[target]
            return True

    def is_active(self, target: str, mode: str | None = None) -> bool:
        """Return whether a matching non-expired lease is active."""
        self._validate_target(target)
        if mode is not None:
            self._validate_mode(target, mode)
        with self._lock:
            self._expire_locked(self._clock())
            lease = self._leases.get(target)
            return lease is not None and (mode is None or lease.mode == mode)

    def active_lease(self, target: str) -> FaultLease | None:
        """Return the immutable active lease, or ``None`` if inactive/expired."""
        self._validate_target(target)
        with self._lock:
            self._expire_locked(self._clock())
            return self._leases.get(target)

    def status(self, target: str | None = None) -> dict[str, dict | None] | dict | None:
        """Return a snapshot with remaining durations, purging expired leases."""
        if target is not None:
            self._validate_target(target)
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            if target is not None:
                return self._status_for_locked(target, now)
            return {
                service_id: self._status_for_locked(service_id, now)
                for service_id in sorted(ALLOWED_SERVICE_IDS)
            }

    @staticmethod
    def _validate_target(target: str) -> None:
        if not isinstance(target, str) or target not in ALLOWED_SERVICE_IDS:
            raise InvalidFaultTarget(f"Unsupported fault target: {target!r}")

    @staticmethod
    def _validate_mode(target: str, mode: str) -> None:
        if not isinstance(mode, str) or mode not in FAULT_MODES_BY_SERVICE.get(target, ()):
            raise InvalidFaultMode(f"Unsupported fault mode {mode!r} for {target!r}")

    @staticmethod
    def _validate_duration(duration_seconds: int | float) -> float:
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
            raise InvalidLeaseDuration("Lease duration must be a finite number of seconds")
        duration = float(duration_seconds)
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_LEASE_SECONDS:
            raise InvalidLeaseDuration(
                f"Lease duration must be greater than 0 and at most {MAX_LEASE_SECONDS:g} seconds"
            )
        return duration

    def _expire_locked(self, now: float) -> None:
        expired = [
            target for target, lease in self._leases.items()
            if now >= lease._expires_at
        ]
        for target in expired:
            del self._leases[target]

    def _status_for_locked(self, target: str, now: float) -> dict | None:
        lease = self._leases.get(target)
        if lease is None:
            return None
        return {
            "lease_id": lease.lease_id,
            "target": lease.target,
            "mode": lease.mode,
            "duration_seconds": lease.duration_seconds,
            "remaining_seconds": max(0.0, lease._expires_at - now),
        }
