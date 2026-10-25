from simulation.agent import Agent  # the agent class
from simulation.metrics import compute_run_metrics, nearest_rank_percentile, relative_change  # functions under test
from simulation.models import AgentState  # agent states


def agent(i, state, evac=None, wait=0.0):  # build an agent in a given final state
    a = Agent(i, 1, 0)  # agent starting at node 1
    a.state = state  # force the state
    a.evacuation_time = evac  # evacuation time, if any
    a.wait_seconds = wait  # time spent waiting
    return a  # the agent


def test_nearest_rank_percentile():  # percentile edge cases
    xs = list(range(1, 101))  # 1 to 100
    assert nearest_rank_percentile(xs, 95) == 95  # the 95th of 100 values is 95
    assert nearest_rank_percentile([5.0], 95) == 5.0  # a single value
    assert nearest_rank_percentile([], 95) is None  # empty sample
    assert nearest_rank_percentile([1, 2, 3, 4], 50) == 2  # nearest rank, not interpolation


def test_completion_rate_uses_all_agents_and_nobody_dropped():  # the denominator is every agent
    agents = [agent(1, AgentState.EVACUATED, 10, 1), agent(2, AgentState.EVACUATED, 30, 3),  # two evacuated
              agent(3, AgentState.UNABLE_TO_EVACUATE), agent(4, AgentState.MOVING), agent(5, AgentState.QUEUED, wait=9)]  # one failed, two unresolved
    m = compute_run_metrics(agents)  # compute
    assert m["total_agents"] == 5 and m["evacuated"] == 2 and m["failed"] == 1 and m["unresolved"] == 2  # counts add up
    assert m["completion_rate"] == 0.4  # 2 of 5
    assert m["average_evacuation_time"] == 20 and m["median_evacuation_time"] == 20  # times of evacuated agents only
    assert m["total_waiting_time"] == 13 and m["maximum_wait_time"] == 9  # waiting over all agents


def test_no_evacuees_gives_none_not_zero():  # missing is not zero
    m = compute_run_metrics([agent(1, AgentState.MOVING)])  # nobody evacuated
    assert m["median_evacuation_time"] is None and m["completion_rate"] == 0  # median undefined, rate zero


def test_relative_change():  # change helper
    assert relative_change(200, 150) == -0.25  # a 25% drop
    assert relative_change(0, 5) is None  # zero baseline is undefined
    assert relative_change(None, 5) is None  # missing baseline is undefined
