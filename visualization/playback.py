"""Frame-based playback over stored simulation snapshots.

Streamlit reruns the script on every interaction, so a continuously animating
Folium map is not reliable. Instead the page steps through genuine snapshot
frames (Play = advance one frame, sleep, rerun). The browser-side
TimestampedGeoJson animation in ``map_view`` is offered as an alternative.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PlaybackState:
    run_id: Optional[int] = None
    index: int = 0
    playing: bool = False
    speed: float = 1.0          # frames per second

    def reset(self) -> None:
        self.index = 0
        self.playing = False


def step(index: int, n_frames: int, delta: int = 1) -> int:
    """Clamp to the valid frame range."""
    if n_frames <= 0:
        return 0
    return max(0, min(n_frames - 1, index + delta))


def advance_playing(state: PlaybackState, n_frames: int) -> PlaybackState:
    """One Play tick; stops automatically on the last frame."""
    state.index = step(state.index, n_frames, 1)
    if state.index >= n_frames - 1:
        state.playing = False
    return state


def sleep_seconds(speed: float) -> float:
    return 1.0 / max(0.25, min(20.0, speed))
