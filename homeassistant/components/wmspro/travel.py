"""Movement model of a WMS cover, free of Home Assistant imports.

The model keeps the displayed position and tilt of one cover. Commands set
the state, a running drive is interpolated linearly, and a measured value
from a status poll replaces the calculated one while the cover stands still.

Scale: position and tilt use the Home Assistant scale, 0 is closed and 100
is open. All times are seconds, "now" is a monotonic clock value.
"""

from dataclasses import dataclass
from enum import StrEnum


class PositionSource(StrEnum):
    """Origin of the displayed position."""

    MEASURED = "measured"
    CALCULATED = "calculated"
    RESTORED = "restored"


@dataclass(frozen=True)
class TravelSettings:
    """Delays and tolerances of the model."""

    settle_after_travel: float = 2.0
    settle_after_stop: float = 2.0
    settle_tilt: float = 5.0
    settle_unknown_travel: float = 120.0
    position_tolerance: int = 3


@dataclass(frozen=True)
class Measurement:
    """Result of handing a measured value to the model."""

    applied: bool
    deviation: bool


def clamp_percent(value: float) -> float:
    """Limit a value to the range 0..100."""
    return max(0.0, min(100.0, float(value)))


def wms_percentage_to_position(percentage: object) -> int | None:
    """Convert a WMS percentage (0 open, 100 closed) to a position.

    Returns None if the value is missing or outside 0..100.
    """
    if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
        return None
    if percentage < 0 or percentage > 100:
        return None
    return int(100 - percentage + 0.5)


class CoverModel:
    """Displayed state of one cover."""

    def __init__(self, settings: TravelSettings | None = None) -> None:
        """Initialize an unknown cover state."""
        self.settings = settings or TravelSettings()
        self.position: float | None = None
        self.tilt: int | None = None
        self.source: PositionSource | None = None
        self._start: float | None = None
        self._target: float | None = None
        self._started_at: float | None = None
        self._duration: float | None = None
        self._pending_tilt: int | None = None

    # --- State ---

    @property
    def is_travelling(self) -> bool:
        """Return True while a drive is interpolated."""
        return self._target is not None

    @property
    def is_opening(self) -> bool:
        """Return True while an interpolated drive moves towards open."""
        return self.is_travelling and self._target > self._start

    @property
    def is_closing(self) -> bool:
        """Return True while an interpolated drive moves towards closed."""
        return self.is_travelling and self._target < self._start

    @property
    def position_int(self) -> int | None:
        """Return the displayed position as integer."""
        if self.position is None:
            return None
        return int(self.position + 0.5)

    def _clear_travel(self) -> None:
        self._start = None
        self._target = None
        self._started_at = None
        self._duration = None
        self._pending_tilt = None

    # --- Time ---

    def advance(self, now: float) -> bool:
        """Move the displayed position to the given time.

        Returns True if a drive reached its target with this call.
        """
        if not self.is_travelling:
            return False
        elapsed = max(0.0, now - self._started_at)
        if elapsed >= self._duration:
            self.position = self._target
            if self._pending_tilt is not None:
                self.tilt = self._pending_tilt
            self._clear_travel()
            return True
        self.position = self._start + (self._target - self._start) * (
            elapsed / self._duration
        )
        return False

    # --- Commands ---

    def start_move(
        self,
        now: float,
        target: float,
        time_open: float,
        time_close: float,
        tilt: int | None = None,
    ) -> float:
        """Handle an accepted drive command.

        Returns the delay in seconds until the settle poll.
        """
        self.advance(now)
        target = clamp_percent(target)
        start = self.position
        self._clear_travel()
        self.source = PositionSource.CALCULATED

        if start is None:
            self.position = target
            if tilt is not None:
                self.tilt = tilt
            return self.settings.settle_unknown_travel

        distance = abs(target - start)
        if distance < 0.5:
            self.position = target
            if tilt is not None:
                self.tilt = tilt
            return self.settings.settle_tilt

        full_time = time_open if target > start else time_close
        if full_time <= 0:
            self.position = target
            if tilt is not None:
                self.tilt = tilt
            return self.settings.settle_unknown_travel

        self._start = start
        self._target = target
        self._started_at = now
        self._duration = full_time * distance / 100
        self._pending_tilt = tilt
        return self._duration + self.settings.settle_after_travel

    def set_tilt(self, now: float, tilt: int) -> float:
        """Handle an accepted tilt-only command, it ends a running drive."""
        self.advance(now)
        self._clear_travel()
        self.tilt = int(clamp_percent(tilt) + 0.5)
        self.source = PositionSource.CALCULATED
        return self.settings.settle_tilt

    def stop(self, now: float) -> float:
        """Handle an accepted stop command, the displayed value freezes."""
        self.advance(now)
        self._clear_travel()
        if self.position is not None:
            self.source = PositionSource.CALCULATED
        return self.settings.settle_after_stop

    # --- Measurement and restore ---

    def apply_measurement(
        self, now: float, position: int | None, tilt: int | None
    ) -> Measurement:
        """Take over a measured value unless a drive is running."""
        self.advance(now)
        if self.is_travelling or position is None:
            return Measurement(applied=False, deviation=False)
        old = self.position
        self.position = clamp_percent(position)
        if tilt is not None:
            self.tilt = int(clamp_percent(tilt) + 0.5)
        self.source = PositionSource.MEASURED
        deviation = (
            old is not None
            and abs(self.position - old) > self.settings.position_tolerance
        )
        return Measurement(applied=True, deviation=deviation)

    def restore(
        self,
        position: float | None,
        tilt: int | None,
        target_position: float | None,
        target_tilt: int | None,
    ) -> None:
        """Take over the state stored before a restart.

        A value that is already known (measured at setup) wins.
        """
        if self.position is not None:
            return
        if target_position is not None:
            self.position = clamp_percent(target_position)
            self.tilt = target_tilt if target_tilt is not None else tilt
        elif position is not None:
            self.position = clamp_percent(position)
            self.tilt = tilt
        else:
            return
        self.source = PositionSource.RESTORED

    def snapshot(self) -> dict[str, int | None]:
        """Return the state to store for a restart."""
        return {
            "position": self.position_int,
            "tilt": self.tilt,
            "target_position": (
                int(self._target + 0.5) if self.is_travelling else None
            ),
            "target_tilt": self._pending_tilt if self.is_travelling else None,
        }
