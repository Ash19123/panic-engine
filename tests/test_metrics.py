from simulation.agent import Agent
from simulation.metrics import compute_run_metrics, nearest_rank_percentile, relative_change
from simulation.models import AgentState


def agent(i, state, evac=None, wait=0.0):
    a = Agent(i, 1, 0)
    a.state = state
    a.evacuation_time = evac
    a.wait_seconds = wait
    return a


def test_nearest_rank_percentile():
    xs = list(range(1, 101))
    assert nearest_rank_percentile(xs, 95) == 95
    assert nearest_rank_percentile([5.0], 95) == 5.0
    assert nearest_rank_percentile([], 95) is None
    assert nearest_rank_percentile([1, 2, 3, 4], 50) == 2


def test_completion_rate_uses_all_agents_and_nobody_dropped():
    agents = [agent(1, AgentState.EVACUATED, 10, 1), agent(2, AgentState.EVACUATED, 30, 3),
              agent(3, AgentState.UNABLE_TO_EVACUATE), agent(4, AgentState.MOVING), agent(5, AgentState.QUEUED, wait=9)]
    m = compute_run_metrics(agents)
    assert m["total_agents"] == 5 and m["evacuated"] == 2 and m["failed"] == 1 and m["unresolved"] == 2
    assert m["completion_rate"] == 0.4
    assert m["average_evacuation_time"] == 20 and m["median_evacuation_time"] == 20
    assert m["total_waiting_time"] == 13 and m["maximum_wait_time"] == 9


def test_no_evacuees_gives_none_not_zero():
    m = compute_run_metrics([agent(1, AgentState.MOVING)])
    assert m["median_evacuation_time"] is None and m["completion_rate"] == 0


def test_relative_change():
    assert relative_change(200, 150) == -0.25
    assert relative_change(0, 5) is None
    assert relative_change(None, 5) is None
