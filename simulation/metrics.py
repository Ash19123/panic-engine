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
from __future__ import annotations  # modern type hints on older Python

import math  # ceil for the percentile rank
from statistics import mean, median  # average and middle value
from typing import Dict, Iterable, List, Optional  # type hints

from simulation.agent import Agent  # the agent class
from simulation.models import AgentState  # the agent states


def nearest_rank_percentile(sorted_values: List[float], pct: float) -> Optional[float]:  # percentile of an already sorted list
    n = len(sorted_values)  # sample size
    if n == 0:  # empty sample
        return None  # no percentile exists
    rank = max(1, math.ceil(pct / 100.0 * n - 1e-12))  # nearest-rank position (1-based); the tiny term avoids float error
    return sorted_values[rank - 1]  # value at that rank


def compute_run_metrics(agents: Iterable[Agent]) -> Dict[str, Optional[float]]:  # summary numbers for one finished run
    agents = list(agents)  # allow any iterable, iterate more than once
    total = len(agents)  # all agents of the scenario
    times = sorted(a.evacuation_time for a in agents if a.state == AgentState.EVACUATED)  # sorted evacuation times
    failed = sum(1 for a in agents if a.state == AgentState.UNABLE_TO_EVACUATE)  # agents with no feasible route or stop
    evacuated = len(times)  # one time per evacuated agent
    waits = [a.wait_seconds for a in agents]  # time each agent spent queued
    return {  # the metrics dictionary
        "total_agents": total,  # denominator of the completion rate
        "evacuated": evacuated,  # reached a safe stop
        "failed": failed,  # unable to evacuate
        "unresolved": total - evacuated - failed,  # still on the way when the time limit hit
        "completion_rate": (evacuated / total) if total else 0.0,  # share evacuated (0 for an empty run)
        "average_evacuation_time": mean(times) if times else None,  # mean over evacuated agents only
        "median_evacuation_time": median(times) if times else None,  # median over evacuated agents only
        "p95_evacuation_time": nearest_rank_percentile(times, 95),  # 95th percentile over evacuated agents
        "maximum_wait_time": max(waits) if waits else 0.0,  # longest single wait
        "total_waiting_time": sum(waits),  # all waiting time added up
    }


def relative_change(baseline: Optional[float], other: Optional[float]) -> Optional[float]:  # percent-style change helper
    """(other - baseline) / baseline, or None when it is undefined (missing or zero baseline)."""
    if baseline is None or other is None or baseline == 0:  # cannot divide or compare
        return None  # undefined
    return (other - baseline) / baseline  # fractional change relative to the baseline
