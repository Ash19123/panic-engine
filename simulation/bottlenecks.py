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
from __future__ import annotations  # modern type hints on older Python

from dataclasses import dataclass, field  # simple record classes
from typing import Dict, List  # type hints

from simulation.congestion import CongestionState  # per-road statistics collected during the run


@dataclass
class BottleneckThresholds:  # the limits that decide what counts as a bottleneck
    min_occupancy_ratio: float = 0.8  # road must reach at least 80% of capacity
    min_queue_seconds: float = 30.0  # people must have queued for at least 30 s in total
    min_agents_affected: int = 5  # at least 5 different people must have waited
    moderate_queue_seconds: float = 120.0  # queue time that makes it MODERATE
    severe_queue_seconds: float = 300.0  # queue time needed for SEVERE
    severe_agents_affected: int = 25  # people affected needed for SEVERE


@dataclass
class BottleneckReport:  # one detected bottleneck
    edge_id: int  # which road
    peak_occupancy: int  # most people on the road at once
    occupancy_ratio: float  # peak occupancy divided by capacity
    queue_duration_seconds: float  # total time someone was queued for this road
    agents_affected: int  # distinct people who had to wait
    total_wait_seconds: float  # agent-seconds of waiting caused by this road
    traversal_count: int  # how many times the road was entered
    detected_at_seconds: float  # when the first queue appeared
    severity: str  # MILD, MODERATE or SEVERE
    severity_score: float  # ranking score (waiting agent-seconds)
    contributing_metrics: Dict[str, float] = field(default_factory=dict)  # every number behind the decision


def classify(queue_seconds: float, agents: int, th: BottleneckThresholds) -> str:  # pick the severity label
    if queue_seconds >= th.severe_queue_seconds and agents >= th.severe_agents_affected:  # long queue AND many people
        return "SEVERE"  # worst class
    if queue_seconds >= th.moderate_queue_seconds:  # a long queue
        return "MODERATE"  # middle class
    return "MILD"  # flagged but small


def detect_bottlenecks(state: CongestionState, th: BottleneckThresholds = BottleneckThresholds()) -> List[BottleneckReport]:  # scan all roads
    reports: List[BottleneckReport] = []  # detected bottlenecks
    for eid, queue_s in state.queue_seconds.items():  # roads that ever had a queue
        cap = state.capacity[eid]  # capacity of this road
        peak = state.peak_occupancy.get(eid, 0)  # highest occupancy it reached
        ratio = peak / cap  # peak as a share of capacity
        affected = len(state.affected_agents[eid])  # distinct agents who waited for it
        if ratio < th.min_occupancy_ratio or queue_s < th.min_queue_seconds or affected < th.min_agents_affected:  # fails a test
            continue  # not a bottleneck
        wait = state.wait_seconds[eid]  # total agent-seconds of waiting
        reports.append(BottleneckReport(  # build the report
            edge_id=eid, peak_occupancy=peak, occupancy_ratio=ratio, queue_duration_seconds=queue_s,  # identity and queue numbers
            agents_affected=affected, total_wait_seconds=wait, traversal_count=state.traversals[eid],  # impact numbers
            detected_at_seconds=state.first_queue_time.get(eid, 0.0), severity=classify(queue_s, affected, th),  # when and how bad
            severity_score=wait,  # rank by waiting caused
            contributing_metrics={"capacity": cap, "peak_occupancy": peak, "occupancy_ratio": round(ratio, 4),  # evidence: capacity and peak
                                  "queue_seconds": queue_s, "agents_affected": affected,  # evidence: queue time and people
                                  "total_wait_seconds": wait, "traversals": state.traversals[eid],  # evidence: waiting and use
                                  "wait_per_traversal": round(wait / max(1, state.traversals[eid]), 3)}))  # evidence: average wait per entry
    reports.sort(key=lambda r: (-r.severity_score, r.edge_id))  # worst first, ties by road id
    return reports  # the ranked list
