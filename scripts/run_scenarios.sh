#!/bin/sh
# Every scenario of the paper, ten crowds each, on both study areas. Results (with every single seed): scripts/results/areas/*.json
# usage: PY=python JOBS=8 sh scripts/run_scenarios.sh
PY=${PY:-python}
JOBS=${JOBS:-8}
S="--seeds 1 2 3 4 5 6 7 8 9 10"
cat <<LIST | xargs -P "$JOBS" -L 1 sh -c '
name=$0; '"$PY"' -m scripts.compare_strategies '"$S"' --out scripts/results/areas/$name.json "$@" > scripts/results/areas/$name.log 2>&1'
campus_bomb_L60 --map campus --lead 60
campus_bomb_L180 --map campus --lead 180
campus_bomb_L300 --map campus --lead 300
campus_bomb_L400 --map campus --lead 400
campus_stress --map campus --no-bomb --limit 1800
campus_closure --map campus --no-bomb --limit 1800 --closure-radius 150 --closure-time 300
campus_phone_100 --map campus --lead 180 --phone-reach 1.0 --channels
campus_phone_60 --map campus --lead 180 --phone-reach 0.6
campus_phone_30 --map campus --lead 180 --phone-reach 0.3
campus_phone_30_relay --map campus --lead 180 --phone-reach 0.3 --neighbour-radius 50 --neighbour-delay 20
mangalagiri_bomb_L30 --map mangalagiri --lead 30
mangalagiri_bomb_L60 --map mangalagiri --lead 60
mangalagiri_bomb_L120 --map mangalagiri --lead 120
mangalagiri_bomb_L180 --map mangalagiri --lead 180
mangalagiri_bomb_L300 --map mangalagiri --lead 300
mangalagiri_stress --map mangalagiri --no-bomb --limit 1800
mangalagiri_closure --map mangalagiri --no-bomb --limit 1800 --closure-radius 150 --closure-time 300
mangalagiri_phone_100 --map mangalagiri --lead 60 --phone-reach 1.0 --channels
mangalagiri_phone_60 --map mangalagiri --lead 60 --phone-reach 0.6
mangalagiri_phone_30 --map mangalagiri --lead 60 --phone-reach 0.3
mangalagiri_phone_30_relay --map mangalagiri --lead 60 --phone-reach 0.3 --neighbour-radius 50 --neighbour-delay 20
grid_stress --map grid --no-bomb --agents 300 --limit 1800
LIST
