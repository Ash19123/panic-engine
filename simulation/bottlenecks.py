"""Bottleneck detection.

A bottleneck is a *capacity-constrained* segment: one where agents are actually
held up. Heavy traffic alone does not qualify - a wide arterial can carry
thousands of agents without any queue and is not reported.

An edge is flagged when ALL of these hold (thresholds are configurable):

1. its peak occupancy ratio (peak occupancy / capacity)  >= ``min_occupancy_ratio``
2. agents queued to enter it for >= ``min_queue_seconds`` in total (persistence)
3. >= ``min_agents_affected`` distinct agents had to wait for it

Severity:
  SEVERE    queue time >= severe_queue_seconds AND agents affected >= severe_agents_affected
  MODERATE  queue time >= moderate_queue_seconds
  MILD      otherwise (still flagged)
``severity_score`` (total agent-seconds of waiting caused by the edge) ranks edges
within a run. ``contributing_metrics`` records every number that drove the decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from simulation.congestion import CongestionState


@dataclass
class BottleneckThresholds:
    min_occupancy_ratio: float = 0.8
    min_queue_seconds: float = 30.0
    min_agents_affected: int = 5
    moderate_queue_seconds: float = 120.0
    severe_queue_seconds: float = 300.0
    severe_agents_affected: int = 25


@dataclass
class BottleneckReport:
    edge_id: int
    peak_occupancy: int
    occupancy_ratio: float
    queue_duration_seconds: float
    agents_affected: int
    total_wait_seconds: float
    traversal_count: int
    detected_at_seconds: float
    severity: str
    severity_score: float
    contributing_metrics: Dict[str, float] = field(default_factory=dict)


def classify(queue_seconds: float, agents: int, th: BottleneckThresholds) -> str:
    if queue_seconds >= th.severe_queue_seconds and agents >= th.severe_agents_affected:
        return "SEVERE"
    if queue_seconds >= th.moderate_queue_seconds:
        return "MODERATE"
    return "MILD"


def detect_bottlenecks(state: CongestionState, th: BottleneckThresholds = BottleneckThresholds()) -> List[BottleneckReport]:
    reports: List[BottleneckReport] = []
    for eid, queue_s in state.queue_seconds.items():
        cap = state.capacity[eid]
        peak = state.peak_occupancy.get(eid, 0)
        ratio = peak / cap
        affected = len(state.affected_agents[eid])
        if ratio < th.min_occupancy_ratio or queue_s < th.min_queue_seconds or affected < th.min_agents_affected:
            continue
        wait = state.wait_seconds[eid]
        reports.append(BottleneckReport(
            edge_id=eid, peak_occupancy=peak, occupancy_ratio=ratio, queue_duration_seconds=queue_s,
            agents_affected=affected, total_wait_seconds=wait, traversal_count=state.traversals[eid],
            detected_at_seconds=state.first_queue_time.get(eid, 0.0), severity=classify(queue_s, affected, th),
            severity_score=wait,
            contributing_metrics={"capacity": cap, "peak_occupancy": peak, "occupancy_ratio": round(ratio, 4),
                                  "queue_seconds": queue_s, "agents_affected": affected,
                                  "total_wait_seconds": wait, "traversals": state.traversals[eid],
                                  "wait_per_traversal": round(wait / max(1, state.traversals[eid]), 3)}))
    reports.sort(key=lambda r: (-r.severity_score, r.edge_id))
    return reports
