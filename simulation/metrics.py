"""Run-level metrics computed in Python (the SQL module recomputes them for cross-checking).

Metric semantics
----------------
* ``total_agents``     every agent of the scenario - the denominator of ``completion_rate``.
* ``evacuated``        agents admitted by a destination (state EVACUATED).
* ``failed``           UNABLE_TO_EVACUATE: no feasible route or no destination with capacity.
* ``unresolved``       agents still WAITING / MOVING / QUEUED when the time limit hit.
  evacuated + failed + unresolved == total_agents, always.
* ``completion_rate``  evacuated / total_agents. Unresolved and failed agents are never dropped.
* Evacuation-time statistics (mean, median, p95) are over EVACUATED agents only and are
  therefore conditional on evacuating; read them together with ``completion_rate``.
  Evacuation time = seconds from the alert (t = 0) to admission, so it includes the
  agent's departure delay.
* Percentile = nearest-rank: the value at rank ceil(0.95 * n) of the sorted sample.
* Wait statistics are over ALL agents (time spent QUEUED, excluding the pre-departure delay).
"""
from __future__ import annotations

import math
from statistics import mean, median
from typing import Dict, Iterable, List, Optional

from simulation.agent import Agent
from simulation.models import AgentState


def nearest_rank_percentile(sorted_values: List[float], pct: float) -> Optional[float]:
    n = len(sorted_values)
    if n == 0:
        return None
    rank = max(1, math.ceil(pct / 100.0 * n - 1e-12))
    return sorted_values[rank - 1]


def compute_run_metrics(agents: Iterable[Agent]) -> Dict[str, Optional[float]]:
    agents = list(agents)
    total = len(agents)
    times = sorted(a.evacuation_time for a in agents if a.state == AgentState.EVACUATED)
    failed = sum(1 for a in agents if a.state == AgentState.UNABLE_TO_EVACUATE)
    evacuated = len(times)
    waits = [a.wait_seconds for a in agents]
    return {
        "total_agents": total,
        "evacuated": evacuated,
        "failed": failed,
        "unresolved": total - evacuated - failed,
        "completion_rate": (evacuated / total) if total else 0.0,
        "average_evacuation_time": mean(times) if times else None,
        "median_evacuation_time": median(times) if times else None,
        "p95_evacuation_time": nearest_rank_percentile(times, 95),
        "maximum_wait_time": max(waits) if waits else 0.0,
        "total_waiting_time": sum(waits),
    }


def relative_change(baseline: Optional[float], other: Optional[float]) -> Optional[float]:
    """(other - baseline) / baseline, or None when it is undefined (missing or zero baseline)."""
    if baseline is None or other is None or baseline == 0:
        return None
    return (other - baseline) / baseline
