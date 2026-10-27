#!/bin/sh
# No-hazard campus stress sensitivity (30-minute limit, as in scripts/results/campus_stress.json). Results: scripts/results/sens/stress_*.json
PY=${PY:-python}
run() { name=$1; shift; $PY -m scripts.compare_strategies --no-bomb --limit 1800 --seeds 1 2 3 4 5 6 7 8 9 10 --out scripts/results/sens/$name.json "$@" > scripts/results/sens/$name.log 2>&1; }
run stress_alpha_0.3 --alpha 0.3 &
run stress_alpha_1.2 --alpha 1.2 &
run stress_beta_1 --beta 1 &
run stress_beta_4 --beta 4 &
run stress_cap_150 --safe-cap 150 &
run stress_agents_400 --agents 400 &
wait
