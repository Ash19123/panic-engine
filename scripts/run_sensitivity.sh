#!/bin/sh
# One-at-a-time sensitivity sweep around the base drill (campus, 250 people, 180 s lead). Results: scripts/results/sens/*.json
PY=${PY:-python}
run() { name=$1; shift; $PY -m scripts.compare_strategies --seeds 1 2 3 4 5 6 7 8 9 10 --out scripts/results/sens/$name.json "$@" > scripts/results/sens/$name.log 2>&1; }
run walk_1.0 --lead 180 --walk 1.0 &
run walk_1.8 --lead 180 --walk 1.8 &
run delay_30 --lead 180 --delay 30 &
run delay_180 --lead 180 --delay 180 &
wait
run blast_100 --lead 180 --blast 100 &
run blast_200 --lead 180 --blast 200 &
run agents_150 --lead 180 --agents 150 &
run agents_400 --lead 180 --agents 400 &
wait
