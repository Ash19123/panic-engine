"""The principal analytical SQL queries (all parameterised with ``?``).

Every number the dashboard shows comes from these statements or from the stored
columns they read; nothing is hard-coded. Window functions need SQLite >= 3.25.

Percentile convention: nearest-rank, rank = ceil(p*n) computed with integer
arithmetic ((95*n + 99) / 100); median = mean of the two middle ranks.
"""

# 1. Completion rate per run (denominator = ALL agents with an outcome row)
COMPLETION_BY_RUN = """
SELECT r.run_id, s.scenario_name, r.routing_strategy, r.random_seed,
       COUNT(*)                                                     AS total_agents,
       SUM(o.final_state = 'EVACUATED')                             AS evacuated,
       SUM(o.final_state = 'UNABLE_TO_EVACUATE')                    AS failed,
       SUM(o.final_state IN ('WAITING','MOVING','QUEUED'))          AS unresolved,
       1.0 * SUM(o.final_state = 'EVACUATED') / COUNT(*)            AS completion_rate
FROM simulation_runs r
JOIN scenarios s       ON s.scenario_id = r.scenario_id
JOIN agent_outcomes o  ON o.run_id = r.run_id
WHERE r.status = 'COMPLETED'
GROUP BY r.run_id
ORDER BY r.run_id DESC
"""  # one row per finished run: counts of evacuated, failed and unresolved agents, and the completion rate

# 2. Average / median / 95th percentile evacuation time (window functions)
EVACUATION_TIME_STATS = """
WITH ranked AS (
    SELECT o.run_id, o.evacuation_time_seconds AS t,
           ROW_NUMBER() OVER (PARTITION BY o.run_id ORDER BY o.evacuation_time_seconds, o.agent_id) AS rn,
           COUNT(*)     OVER (PARTITION BY o.run_id)                                                AS n
    FROM agent_outcomes o
    WHERE o.final_state = 'EVACUATED'
)
SELECT run_id, MAX(n) AS evacuated, AVG(t) AS average_seconds,
       AVG(CASE WHEN rn IN ((n + 1) / 2, (n + 2) / 2) THEN t END) AS median_seconds,
       MAX(CASE WHEN rn = (95 * n + 99) / 100 THEN t END)         AS p95_seconds
FROM ranked
GROUP BY run_id
ORDER BY run_id DESC
"""  # ranks evacuated agents by time within each run, then reads off the average, the middle value and the 95th percentile

# 3. Outcome breakdown for one run
OUTCOME_BREAKDOWN = """
SELECT final_state, COUNT(*) AS agents,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM agent_outcomes WHERE run_id = ?), 2) AS percent
FROM agent_outcomes WHERE run_id = ? GROUP BY final_state ORDER BY agents DESC
"""  # for one run: how many agents ended in each state and the percentage (the run id is bound twice)

# 4. Average waiting time by destination
WAIT_BY_DESTINATION = """
SELECT d.destination_id, d.destination_name, d.hypothetical_status,
       COUNT(*) AS agents_served, AVG(o.accumulated_wait_seconds) AS avg_wait_seconds,
       MAX(o.accumulated_wait_seconds) AS max_wait_seconds, AVG(o.evacuation_time_seconds) AS avg_evacuation_seconds
FROM agent_outcomes o JOIN destinations d ON d.destination_id = o.destination_id
WHERE o.run_id = ? AND o.final_state = 'EVACUATED'
GROUP BY d.destination_id ORDER BY avg_wait_seconds DESC
"""  # per safe stop: people served, average and maximum wait, average evacuation time

# 5. Peak congestion per road segment
PEAK_CONGESTION_BY_EDGE = """
SELECT s.edge_id, e.road_class, e.capacity, e.distance_meters,
       MAX(s.occupancy) AS peak_occupancy, MAX(s.queue_length) AS peak_queue,
       1.0 * MAX(s.occupancy) / e.capacity AS peak_ratio,
       SUM(s.queue_length > 0) AS snapshots_with_queue
FROM edge_occupancy_snapshots s JOIN network_edges e ON e.edge_id = s.edge_id
WHERE s.run_id = ?
GROUP BY s.edge_id ORDER BY peak_ratio DESC, peak_queue DESC LIMIT ?
"""  # the fullest roads of a run: peak occupancy, peak queue and peak ratio

# 6. Most frequently used road segments (needs EDGE_ENTERED events)
MOST_USED_EDGES = """
SELECT ev.edge_id, e.road_class, e.capacity, COUNT(*) AS traversals,
       COUNT(DISTINCT ev.agent_id) AS distinct_agents
FROM simulation_events ev JOIN network_edges e ON e.edge_id = ev.edge_id
WHERE ev.run_id = ? AND ev.event_type = 'EDGE_ENTERED'
GROUP BY ev.edge_id ORDER BY traversals DESC LIMIT ?
"""  # roads ranked by how often they were entered

# 7. Bottleneck severity ranking
BOTTLENECK_RANKING = """
SELECT b.edge_id, b.severity, b.severity_score AS total_wait_seconds, b.occupancy_ratio, b.peak_occupancy,
       e.capacity, b.queue_duration_seconds, b.agents_affected, b.traversal_count, b.detected_at_seconds,
       RANK() OVER (ORDER BY CASE b.severity WHEN 'SEVERE' THEN 3 WHEN 'MODERATE' THEN 2 ELSE 1 END DESC,
                    b.severity_score DESC) AS severity_rank,
       b.contributing_metrics_json
FROM bottleneck_reports b JOIN network_edges e ON e.edge_id = b.edge_id
WHERE b.run_id = ? ORDER BY severity_rank, b.edge_id
"""  # bottlenecks of a run ranked by severity class, then by waiting caused (RANK window function)

# 8. Routing-strategy comparison across all completed runs
STRATEGY_COMPARISON = """
SELECT routing_strategy, COUNT(*) AS runs, SUM(total_agents) AS agents,
       AVG(completion_rate) AS mean_completion_rate,
       AVG(median_evacuation_time_seconds) AS mean_median_evacuation_s,
       AVG(percentile_95_evacuation_time_seconds) AS mean_p95_evacuation_s,
       AVG(total_waiting_time_seconds) AS mean_total_wait_s,
       AVG(unresolved_agents + failed_agents) AS mean_not_evacuated,
       (SELECT COUNT(*) FROM bottleneck_reports b JOIN simulation_runs r2 USING (run_id)
         WHERE r2.routing_strategy = r.routing_strategy AND r2.status='COMPLETED') * 1.0 / COUNT(*) AS mean_bottlenecks
FROM simulation_runs r WHERE status = 'COMPLETED'
GROUP BY routing_strategy ORDER BY routing_strategy
"""  # averages of the main metrics for each routing strategy over all completed runs

# 9. Scenario history
SCENARIO_HISTORY = """
SELECT s.scenario_id, s.scenario_name, s.geographic_data_source, s.routing_strategy AS default_strategy,
       s.agent_count, s.random_seed, s.created_at, s.parent_scenario_id,
       COUNT(r.run_id) AS runs, MAX(r.started_at) AS last_run_at,
       (SELECT COUNT(*) FROM scenario_closures c WHERE c.scenario_id = s.scenario_id) AS closures
FROM scenarios s LEFT JOIN simulation_runs r ON r.scenario_id = s.scenario_id AND r.status = 'COMPLETED'
GROUP BY s.scenario_id ORDER BY s.scenario_id DESC
"""  # every scenario with its size, seed, parent, number of runs and number of road closures

# 10. Before / after (baseline vs intervention) with deltas
INTERVENTION_COMPARISON = """
SELECT i.intervention_id, i.name, i.comparison_mode, i.intervention_type,
       b.run_id AS baseline_run, b.routing_strategy AS baseline_strategy,
       a.run_id AS alternative_run, a.routing_strategy AS alternative_strategy,
       b.completion_rate AS base_completion, a.completion_rate AS alt_completion,
       a.completion_rate - b.completion_rate AS completion_delta,
       b.median_evacuation_time_seconds AS base_median_s, a.median_evacuation_time_seconds AS alt_median_s,
       b.percentile_95_evacuation_time_seconds AS base_p95_s, a.percentile_95_evacuation_time_seconds AS alt_p95_s,
       b.total_waiting_time_seconds AS base_wait_s, a.total_waiting_time_seconds AS alt_wait_s,
       CASE WHEN b.total_waiting_time_seconds > 0
            THEN (a.total_waiting_time_seconds - b.total_waiting_time_seconds) / b.total_waiting_time_seconds END AS wait_relative_change,
       b.unresolved_agents + b.failed_agents AS base_not_evacuated,
       a.unresolved_agents + a.failed_agents AS alt_not_evacuated,
       i.parameters_json, i.created_at
FROM interventions i
JOIN simulation_runs b ON b.run_id = i.baseline_run_id
JOIN simulation_runs a ON a.run_id = i.intervention_run_id
ORDER BY i.intervention_id DESC
"""  # each what-if experiment with baseline and alternative metrics side by side and the differences

# 11. Crowd-occupancy time series (+ running peak via window function)
OCCUPANCY_TIME_SERIES = """
SELECT simulation_time_seconds AS t, SUM(occupancy) AS agents_on_roads, SUM(queue_length) AS agents_queued_for_edges,
       MAX(SUM(occupancy)) OVER (ORDER BY simulation_time_seconds) AS running_peak_on_roads
FROM edge_occupancy_snapshots WHERE run_id = ?
GROUP BY simulation_time_seconds ORDER BY simulation_time_seconds
"""  # people on roads and people queued at each snapshot time, plus the highest value so far

# 12. Repeated runs of one scenario name + strategy under different seeds
SEED_VARIABILITY = """
SELECT s.scenario_name, r.routing_strategy, COUNT(*) AS runs, COUNT(DISTINCT r.random_seed) AS distinct_seeds,
       AVG(r.completion_rate) AS mean_completion, MIN(r.completion_rate) AS min_completion,
       MAX(r.completion_rate) AS max_completion,
       MAX(0.0, AVG(r.completion_rate * r.completion_rate) - AVG(r.completion_rate) * AVG(r.completion_rate)) AS var_completion,
       AVG(r.median_evacuation_time_seconds) AS mean_median_s
FROM simulation_runs r JOIN scenarios s ON s.scenario_id = r.scenario_id
WHERE r.status = 'COMPLETED'
GROUP BY s.scenario_name, r.routing_strategy HAVING COUNT(*) >= 2
ORDER BY s.scenario_name, r.routing_strategy
"""  # mean, minimum, maximum and variance of the completion rate for scenarios run with several seeds

# Counterfactual: per-edge congestion difference between two runs
EDGE_CONGESTION_DIFF = """
WITH a AS (SELECT edge_id, MAX(occupancy) AS peak_occ, MAX(queue_length) AS peak_q, SUM(queue_length) AS q_sum
           FROM edge_occupancy_snapshots WHERE run_id = :base GROUP BY edge_id),
     b AS (SELECT edge_id, MAX(occupancy) AS peak_occ, MAX(queue_length) AS peak_q, SUM(queue_length) AS q_sum
           FROM edge_occupancy_snapshots WHERE run_id = :alt GROUP BY edge_id),
     ids AS (SELECT edge_id FROM a UNION SELECT edge_id FROM b)
SELECT ids.edge_id, e.capacity,
       COALESCE(a.peak_occ,0) AS base_peak_occ, COALESCE(b.peak_occ,0) AS alt_peak_occ,
       COALESCE(b.peak_occ,0) - COALESCE(a.peak_occ,0) AS peak_occ_change,
       1.0 * COALESCE(a.peak_occ,0) / e.capacity AS base_ratio, 1.0 * COALESCE(b.peak_occ,0) / e.capacity AS alt_ratio,
       COALESCE(a.q_sum,0) AS base_queue_snapshots, COALESCE(b.q_sum,0) AS alt_queue_snapshots
FROM ids JOIN network_edges e ON e.edge_id = ids.edge_id
LEFT JOIN a ON a.edge_id = ids.edge_id LEFT JOIN b ON b.edge_id = ids.edge_id
ORDER BY ABS(peak_occ_change) DESC, ids.edge_id LIMIT :lim
"""  # for every road used in either run: peak occupancy in each run and the change, largest changes first

# Bottleneck severity over time for one edge (how congestion evolved)
EDGE_TIMELINE = """
SELECT simulation_time_seconds AS t, occupancy, queue_length FROM edge_occupancy_snapshots
WHERE run_id = ? AND edge_id = ? ORDER BY simulation_time_seconds
"""  # occupancy and queue of one road at each snapshot time

# Evacuation-time histogram buckets
EVACUATION_TIMES = """
SELECT evacuation_time_seconds AS t, accumulated_wait_seconds AS wait FROM agent_outcomes
WHERE run_id = ? AND final_state = 'EVACUATED'
"""  # one row per evacuated agent: evacuation time and waiting time (used for histograms)

# Overview dashboard aggregates
OVERVIEW = """
SELECT (SELECT COUNT(*) FROM simulation_runs WHERE status='COMPLETED') AS completed_runs,
       (SELECT COUNT(*) FROM simulation_runs) AS all_runs,
       (SELECT COALESCE(SUM(total_agents),0) FROM simulation_runs WHERE status='COMPLETED') AS agents_simulated,
       (SELECT COUNT(*) FROM scenarios) AS scenarios,
       (SELECT SUM(evacuated_agents)*1.0/SUM(total_agents) FROM simulation_runs WHERE status='COMPLETED') AS pooled_completion_rate,
       (SELECT AVG(median_evacuation_time_seconds) FROM simulation_runs WHERE status='COMPLETED') AS mean_of_run_medians
"""  # one row of headline totals: runs, agents, scenarios, pooled completion rate
LATEST_RUN = """
SELECT r.run_id, r.status, r.routing_strategy, r.started_at, r.completion_rate, s.scenario_name
FROM simulation_runs r JOIN scenarios s USING (scenario_id) ORDER BY r.run_id DESC LIMIT 1
"""  # the most recent run
HIGHEST_CONGESTION = """
SELECT s.run_id, s.edge_id, 1.0 * MAX(s.occupancy) / e.capacity AS ratio, MAX(s.queue_length) AS peak_queue
FROM edge_occupancy_snapshots s JOIN network_edges e USING (edge_id)
GROUP BY s.run_id, s.edge_id ORDER BY ratio DESC, peak_queue DESC LIMIT 1
"""  # the single most congested road in any run

# Hazard (bomb alert drill) outcomes for one run - all derived from the event log
HAZARD_SUMMARY = """
SELECT
  (SELECT COUNT(*) FROM simulation_events WHERE run_id = :r AND event_type = 'ALERT_SENT')    AS alerts_sent,
  (SELECT MIN(simulation_time_seconds) FROM simulation_events
     WHERE run_id = :r AND event_type = 'ALERT_SENT')                                          AS first_alert_s,
  (SELECT MIN(simulation_time_seconds) FROM simulation_events
     WHERE run_id = :r AND event_type = 'HAZARD_STRIKE')                                       AS first_strike_s,
  (SELECT COUNT(*) FROM simulation_events WHERE run_id = :r AND event_type = 'AGENT_FAILED'
     AND event_details LIKE 'caught in blast%')                                                AS caught_in_blast,
  (SELECT COUNT(*) FROM simulation_events e WHERE e.run_id = :r AND e.event_type = 'AGENT_EVACUATED'
     AND e.simulation_time_seconds <= COALESCE((SELECT MIN(simulation_time_seconds) FROM simulation_events
        WHERE run_id = :r AND event_type = 'HAZARD_STRIKE'), 1e12))                            AS safe_before_strike,
  (SELECT AVG(ev.simulation_time_seconds - al.simulation_time_seconds)
     FROM simulation_events ev JOIN simulation_events al
       ON al.run_id = ev.run_id AND al.agent_id = ev.agent_id AND al.event_type = 'ALERT_SENT'
     WHERE ev.run_id = :r AND ev.event_type = 'AGENT_EVACUATED')                               AS avg_alert_to_safe_s
"""  # one row: alerts sent, first alert time, strike time, people caught, people safe before the strike, average alert-to-safe time


# How the alert reached people in a hazard run, and what happened to each group
ALERT_CHANNELS = """
SELECT COALESCE(a.alert_channel, 'OUTSIDE ALERT AREA') AS channel,
       COUNT(*)                                          AS people,
       SUM(o.final_state = 'EVACUATED')                  AS safe,
       SUM(o.final_state = 'UNABLE_TO_EVACUATE')         AS caught_or_failed,
       SUM(o.final_state IN ('WAITING','MOVING','QUEUED')) AS still_waiting_or_walking
FROM agent_outcomes o JOIN agents a ON a.agent_id = o.agent_id
WHERE o.run_id = ?
GROUP BY channel ORDER BY people DESC
"""  # people per alert channel with how many ended safe, caught or failed, and still on the way
