-- PANIC ENGINE schema, migration version 1.
-- Executed by database/init_db.py inside the migration mechanism; every
-- statement is idempotent (IF NOT EXISTS) so re-running is harmless.

-- ===================== Road network (shared, reusable) =====================
CREATE TABLE IF NOT EXISTS networks (
    network_id      TEXT PRIMARY KEY,                 -- fingerprint-derived, e.g. 'synthetic-3fa9c1d2e4'
    name            TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('OSM', 'SYNTHETIC')),
    is_synthetic    INTEGER NOT NULL CHECK (is_synthetic IN (0, 1)),
    center_lat      REAL,
    center_lon      REAL,
    radius_m        REAL,
    node_count      INTEGER NOT NULL,
    edge_count      INTEGER NOT NULL,
    metadata_json   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS network_nodes (
    node_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id          TEXT NOT NULL REFERENCES networks(network_id) ON DELETE CASCADE,
    source_osm_node_id  INTEGER,
    latitude            REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude           REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    attributes_json     TEXT,
    UNIQUE (network_id, source_osm_node_id)           -- NULLs (synthetic) may repeat
);
CREATE INDEX IF NOT EXISTS idx_nodes_network ON network_nodes(network_id);

-- Parallel edges are allowed: there is deliberately NO unique (source,target).
CREATE TABLE IF NOT EXISTS network_edges (
    edge_id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id                      TEXT NOT NULL REFERENCES networks(network_id) ON DELETE CASCADE,
    source_node_id                  INTEGER NOT NULL REFERENCES network_nodes(node_id) ON DELETE CASCADE,
    target_node_id                  INTEGER NOT NULL REFERENCES network_nodes(node_id) ON DELETE CASCADE,
    distance_meters                 REAL NOT NULL CHECK (distance_meters > 0),
    estimated_travel_time_seconds   REAL CHECK (estimated_travel_time_seconds > 0),
    capacity                        INTEGER NOT NULL CHECK (capacity >= 1),
    is_available                    INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
    road_class                      TEXT,
    osm_way_id                      INTEGER,
    attributes_json                 TEXT,
    geometry_json                   TEXT             -- optional polyline [[lat,lon],...] for real roads
);
CREATE INDEX IF NOT EXISTS idx_edges_network ON network_edges(network_id);
CREATE INDEX IF NOT EXISTS idx_edges_endpoints ON network_edges(network_id, source_node_id, target_node_id);

-- Candidate evacuation destinations. Every row says WHERE it came from; nothing
-- here is ever presented as an official shelter unless hypothetical_status says so.
CREATE TABLE IF NOT EXISTS destinations (
    destination_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id              TEXT NOT NULL REFERENCES networks(network_id) ON DELETE CASCADE,
    node_id                 INTEGER NOT NULL REFERENCES network_nodes(node_id),
    destination_name        TEXT NOT NULL,
    destination_type        TEXT NOT NULL DEFAULT 'SAFE_AREA',
    latitude                REAL NOT NULL,
    longitude               REAL NOT NULL,
    capacity                INTEGER NOT NULL CHECK (capacity >= 0),
    throughput_per_timestep INTEGER NOT NULL CHECK (throughput_per_timestep >= 1),
    source_type             TEXT NOT NULL CHECK (source_type IN ('USER_DEFINED', 'SYNTHETIC_DEMO', 'OSM_TAGGED')),
    hypothetical_status     TEXT NOT NULL DEFAULT 'HYPOTHETICAL' CHECK (hypothetical_status IN ('HYPOTHETICAL', 'VERIFIED'))
);
CREATE INDEX IF NOT EXISTS idx_dest_network ON destinations(network_id);

-- ============================ Scenarios ====================================
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_name           TEXT NOT NULL,
    description             TEXT,
    study_area              TEXT,
    geographic_data_source  TEXT NOT NULL,
    network_id              TEXT NOT NULL REFERENCES networks(network_id),
    network_fingerprint     TEXT NOT NULL,
    routing_strategy        TEXT NOT NULL CHECK (routing_strategy IN ('SHORTEST_PATH', 'CONGESTION_AWARE', 'LOAD_BALANCED')),
    agent_count             INTEGER NOT NULL CHECK (agent_count > 0),
    random_seed             INTEGER NOT NULL,
    timestep_seconds        REAL NOT NULL CHECK (timestep_seconds > 0),
    simulation_time_limit   REAL NOT NULL CHECK (simulation_time_limit > 0),
    configuration_json      TEXT,                     -- secondary model parameters (ModelParams)
    parent_scenario_id      INTEGER REFERENCES scenarios(scenario_id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_scenarios_network ON scenarios(network_id);

CREATE TABLE IF NOT EXISTS scenario_origins (
    scenario_id  INTEGER NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    node_id      INTEGER NOT NULL REFERENCES network_nodes(node_id),
    weight       REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    label        TEXT,
    source_type  TEXT NOT NULL DEFAULT 'USER_DEFINED',
    PRIMARY KEY (scenario_id, node_id)
);

-- Per-scenario overrides of destination capacity / throughput / availability.
CREATE TABLE IF NOT EXISTS scenario_destinations (
    scenario_id              INTEGER NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    destination_id           INTEGER NOT NULL REFERENCES destinations(destination_id),
    capacity                 INTEGER NOT NULL CHECK (capacity >= 0),
    throughput_per_timestep  INTEGER NOT NULL CHECK (throughput_per_timestep >= 1),
    is_available             INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
    PRIMARY KEY (scenario_id, destination_id)
);

CREATE TABLE IF NOT EXISTS scenario_closures (
    closure_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id         INTEGER NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    edge_id             INTEGER NOT NULL REFERENCES network_edges(edge_id),
    close_time_seconds  REAL NOT NULL DEFAULT 0 CHECK (close_time_seconds >= 0),
    UNIQUE (scenario_id, edge_id)
);

-- The synthetic population (identical across all runs of a scenario, which is
-- what makes paired routing comparisons fair). Run-specific outcomes live in
-- agent_outcomes so that one agent row can have many outcomes.
CREATE TABLE IF NOT EXISTS agents (
    agent_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id             INTEGER NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    origin_node_id          INTEGER NOT NULL REFERENCES network_nodes(node_id),
    departure_time_seconds  REAL NOT NULL CHECK (departure_time_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS idx_agents_scenario ON agents(scenario_id);

-- ============================ Runs and results =============================
CREATE TABLE IF NOT EXISTS simulation_runs (
    run_id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id                     INTEGER NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    label                           TEXT,
    started_at                      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at                    TEXT,
    status                          TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    failure_reason                  TEXT,
    routing_strategy                TEXT NOT NULL CHECK (routing_strategy IN ('SHORTEST_PATH', 'CONGESTION_AWARE', 'LOAD_BALANCED')),
    random_seed                     INTEGER NOT NULL,
    engine_version                  TEXT,
    total_simulated_seconds         REAL,
    total_agents                    INTEGER,
    evacuated_agents                INTEGER,
    unresolved_agents               INTEGER,   -- still en route / waiting when the time limit hit
    failed_agents                   INTEGER,   -- UNABLE_TO_EVACUATE (no feasible route/destination)
    average_evacuation_time_seconds REAL,
    median_evacuation_time_seconds  REAL,
    percentile_95_evacuation_time_seconds REAL,
    maximum_wait_time_seconds       REAL,
    total_waiting_time_seconds      REAL,
    completion_rate                 REAL,      -- evacuated / total_agents (ALL agents in the denominator)
    configuration_snapshot          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON simulation_runs(scenario_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON simulation_runs(status);

CREATE TABLE IF NOT EXISTS agent_outcomes (
    run_id                   INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    agent_id                 INTEGER NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    destination_id           INTEGER REFERENCES destinations(destination_id),
    final_state              TEXT NOT NULL CHECK (final_state IN ('WAITING', 'MOVING', 'QUEUED', 'EVACUATED', 'UNABLE_TO_EVACUATE')),
    current_node_id          INTEGER REFERENCES network_nodes(node_id),
    current_edge_id          INTEGER REFERENCES network_edges(edge_id),
    departure_time_seconds   REAL NOT NULL,
    evacuation_time_seconds  REAL,
    accumulated_wait_seconds REAL NOT NULL DEFAULT 0 CHECK (accumulated_wait_seconds >= 0),
    route_change_count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, agent_id),
    CHECK (final_state <> 'EVACUATED' OR evacuation_time_seconds IS NOT NULL),
    CHECK (final_state = 'EVACUATED' OR evacuation_time_seconds IS NULL)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_outcomes_state ON agent_outcomes(run_id, final_state);
CREATE INDEX IF NOT EXISTS idx_outcomes_dest ON agent_outcomes(run_id, destination_id);

CREATE TABLE IF NOT EXISTS simulation_events (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    agent_id                INTEGER REFERENCES agents(agent_id),
    simulation_time_seconds REAL NOT NULL,
    event_type              TEXT NOT NULL CHECK (event_type IN (
        'AGENT_DEPARTED', 'EDGE_ENTERED', 'EDGE_EXITED', 'QUEUE_JOINED', 'QUEUE_LEFT',
        'ROUTE_CHANGED', 'ROAD_BLOCKED', 'AGENT_EVACUATED', 'AGENT_UNRESOLVED', 'AGENT_FAILED')),
    node_id                 INTEGER REFERENCES network_nodes(node_id),
    edge_id                 INTEGER REFERENCES network_edges(edge_id),
    destination_id          INTEGER REFERENCES destinations(destination_id),
    wait_time_seconds       REAL,
    event_details           TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run_type  ON simulation_events(run_id, event_type);
CREATE INDEX IF NOT EXISTS idx_events_run_agent ON simulation_events(run_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_events_run_time  ON simulation_events(run_id, simulation_time_seconds);
CREATE INDEX IF NOT EXISTS idx_events_run_edge  ON simulation_events(run_id, edge_id);

-- Sparse: only edges with occupancy > 0 or a queue are stored at each snapshot.
CREATE TABLE IF NOT EXISTS edge_occupancy_snapshots (
    run_id                  INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    simulation_time_seconds REAL NOT NULL,
    edge_id                 INTEGER NOT NULL REFERENCES network_edges(edge_id),
    occupancy               INTEGER NOT NULL CHECK (occupancy >= 0),
    queue_length            INTEGER NOT NULL DEFAULT 0 CHECK (queue_length >= 0),
    PRIMARY KEY (run_id, simulation_time_seconds, edge_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_occ_edge ON edge_occupancy_snapshots(run_id, edge_id);

-- Agent positions for replay (optional; controlled by ModelParams).
CREATE TABLE IF NOT EXISTS agent_position_snapshots (
    run_id                  INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    simulation_time_seconds REAL NOT NULL,
    agent_id                INTEGER NOT NULL REFERENCES agents(agent_id),
    state                   TEXT NOT NULL,
    edge_id                 INTEGER REFERENCES network_edges(edge_id),
    latitude                REAL NOT NULL,
    longitude               REAL NOT NULL,
    PRIMARY KEY (run_id, simulation_time_seconds, agent_id)
) WITHOUT ROWID;

-- Whole-population counts per frame (so replay counters stay exact even if the
-- stored position sample is a subset).
CREATE TABLE IF NOT EXISTS run_frame_stats (
    run_id                  INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    simulation_time_seconds REAL NOT NULL,
    waiting_count           INTEGER NOT NULL,
    moving_count            INTEGER NOT NULL,
    queued_count            INTEGER NOT NULL,
    evacuated_count         INTEGER NOT NULL,
    failed_count            INTEGER NOT NULL,
    PRIMARY KEY (run_id, simulation_time_seconds)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bottleneck_reports (
    report_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    edge_id                  INTEGER NOT NULL REFERENCES network_edges(edge_id),
    peak_occupancy           INTEGER NOT NULL,
    occupancy_ratio          REAL NOT NULL,            -- peak_occupancy / capacity
    queue_duration_seconds   REAL NOT NULL,
    agents_affected          INTEGER NOT NULL,
    total_wait_seconds       REAL NOT NULL,
    traversal_count          INTEGER NOT NULL,         -- contrast: heavy use is not the same as a bottleneck
    detected_at_seconds      REAL NOT NULL,
    severity                 TEXT NOT NULL CHECK (severity IN ('MILD', 'MODERATE', 'SEVERE')),
    severity_score           REAL NOT NULL,
    contributing_metrics_json TEXT,
    UNIQUE (run_id, edge_id)
);

CREATE TABLE IF NOT EXISTS interventions (
    intervention_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    description          TEXT,
    comparison_mode      TEXT NOT NULL CHECK (comparison_mode IN ('PAIRED_STRATEGY', 'INTERVENTION')),
    intervention_type    TEXT NOT NULL CHECK (intervention_type IN (
        'NONE', 'ROAD_CLOSURE', 'CAPACITY_CHANGE', 'DESTINATION_CHANGE', 'ROUTING_POLICY', 'COMBINED')),
    baseline_run_id      INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    intervention_run_id  INTEGER REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    parameters_json      TEXT,                          -- explicit record of what changed vs. the baseline
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
