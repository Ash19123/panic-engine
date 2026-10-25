"""Frame-based playback over stored simulation snapshots.

Streamlit reruns the script on every interaction, so a continuously animating
Folium map is not reliable. Instead the page steps through genuine snapshot
frames (Play = advance one frame, sleep, rerun). The browser-side
TimestampedGeoJson animation in ``map_view`` is offered as an alternative.
"""
from __future__ import annotations  # modern type hints on older Python

from dataclasses import dataclass  # simple record class
from typing import Optional  # optional values


@dataclass
class PlaybackState:  # where the playback is
    run_id: Optional[int] = None  # which run is being played
    index: int = 0  # current frame number
    playing: bool = False  # is it playing?
    speed: float = 1.0          # frames per second

    def reset(self) -> None:  # go back to the start
        self.index = 0  # first frame
        self.playing = False  # stopped


def step(index: int, n_frames: int, delta: int = 1) -> int:  # move by delta frames
    """Clamp to the valid frame range."""
    if n_frames <= 0:  # no frames at all
        return 0  # nothing to show
    return max(0, min(n_frames - 1, index + delta))  # stay between the first and last frame


def advance_playing(state: PlaybackState, n_frames: int) -> PlaybackState:  # one play tick
    """One Play tick; stops automatically on the last frame."""
    state.index = step(state.index, n_frames, 1)  # next frame
    if state.index >= n_frames - 1:  # reached the end
        state.playing = False  # stop
    return state  # the updated state


def sleep_seconds(speed: float) -> float:  # how long to wait between frames
    return 1.0 / max(0.25, min(20.0, speed))  # the speed is limited to 0.25 to 20 frames per second
