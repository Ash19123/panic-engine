#!/bin/sh
# Extra runs for the paper review: capacity sweep, randomised crowd-stress and closure runs, relay with spread-out people,
# and the bomb sensitivity sweep with stored crowds. usage: PY=python JOBS=8 sh scripts/run_review_runs.sh
PY=${PY:-python}
JOBS=${JOBS:-8}
S="--seeds 1 2 3 4 5 6 7 8 9 10"
cat <<LIST | xargs -P "$JOBS" -L 1 sh -c '
dir=$0; name=$1; shift; '"$PY"' -m scripts.compare_strategies '"$S"' --out scripts/results/$dir/$name.json "$@" > scripts/results/$dir/$name.log 2>&1'
areas campus_stress --map campus --no-bomb --limit 1800
areas campus_closure --map campus --no-bomb --limit 1800 --closure-radius 150 --closure-time 300
areas mangalagiri_stress --map mangalagiri --no-bomb --limit 1800
areas mangalagiri_closure --map mangalagiri --no-bomb --limit 1800 --closure-radius 150 --closure-time 300
areas grid_stress --map grid --no-bomb --agents 300 --limit 1800
review campus_stress_dist --map campus --no-bomb --limit 1800 --spread-origins
review campus_closure_dist --map campus --no-bomb --limit 1800 --spread-origins --closure-random --closure-radius 150 --closure-time 300
review mangalagiri_stress_dist --map mangalagiri --no-bomb --limit 1800 --spread-origins
review mangalagiri_closure_dist --map mangalagiri --no-bomb --limit 1800 --spread-origins --closure-random --closure-radius 150 --closure-time 300
review campus_stress_rho05 --map campus --no-bomb --limit 1800 --capacity-scale 2
review campus_stress_rho1 --map campus --no-bomb --limit 1800 --capacity-scale 4
review campus_stress_rho2 --map campus --no-bomb --limit 1800 --capacity-scale 8
review mangalagiri_stress_rho05 --map mangalagiri --no-bomb --limit 1800 --capacity-scale 2
review mangalagiri_stress_rho1 --map mangalagiri --no-bomb --limit 1800 --capacity-scale 4
review mangalagiri_stress_rho2 --map mangalagiri --no-bomb --limit 1800 --capacity-scale 8
review campus_phone_30_relay_j100 --map campus --lead 180 --phone-reach 0.3 --neighbour-radius 50 --neighbour-delay 20 --position-jitter 100
review mangalagiri_phone_30_relay_j100 --map mangalagiri --lead 60 --phone-reach 0.3 --neighbour-radius 50 --neighbour-delay 20 --position-jitter 100
sens walk_1.0 --lead 180 --walk 1.0
sens walk_1.8 --lead 180 --walk 1.8
sens delay_30 --lead 180 --delay 30
sens delay_180 --lead 180 --delay 180
sens blast_100 --lead 180 --blast 100
sens blast_200 --lead 180 --blast 200
sens agents_150 --lead 180 --agents 150
sens agents_400 --lead 180 --agents 400
LIST
echo ALLDONE
