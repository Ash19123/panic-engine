"""Paired comparison of the routing rules over the ten crowds (run: python -m scripts.paired_stats).

For every scenario file in scripts/results/areas the three rules were run on the same ten crowds, so the natural
statistic is the per-crowd difference d_k = x_k(A) - x_k(B). With n = 10 crowds the mean difference has the 95 %
confidence interval   mean(d) +- t(0.975, n-1) * s_d / sqrt(n),   s_d = sample standard deviation of d.
The t quantile is computed here by numerically inverting the Student t distribution function, so no table is needed.
"""
import functools  # cache the quantile
import glob  # find the result files
import json  # read and write
import math  # gamma function
import statistics as stats  # mean and sample standard deviation
from pathlib import Path  # file paths

RESULTS = Path(__file__).resolve().parent / "results"  # results folder
PAIRS = [("SHORTEST_PATH", "CONGESTION_AWARE"), ("SHORTEST_PATH", "LOAD_BALANCED"), ("CONGESTION_AWARE", "LOAD_BALANCED")]  # A - B


def t_cdf(x: float, df: int) -> float:  # Student t distribution function by Simpson integration of the density
    c = math.gamma((df + 1) / 2) / (math.sqrt(df * math.pi) * math.gamma(df / 2))  # normalising constant
    pdf = lambda u: c * (1 + u * u / df) ** (-(df + 1) / 2)  # density
    n = 4000  # even number of Simpson intervals
    h = x / n  # step from 0 to x
    s = pdf(0) + pdf(x) + sum((4 if i % 2 else 2) * pdf(i * h) for i in range(1, n))  # Simpson sum
    return 0.5 + s * h / 3  # P(T <= x) for x >= 0


@functools.lru_cache(maxsize=None)  # the same quantile is needed for every comparison
def t_quantile(p: float, df: int) -> float:  # inverse of t_cdf for p > 0.5, by bisection
    lo, hi = 0.0, 50.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def paired(a: list, b: list) -> dict:  # statistics of the differences a_k - b_k
    d = [x - y for x, y in zip(a, b)]  # per-crowd differences
    n = len(d)  # number of crowds
    m, s = stats.fmean(d), (stats.stdev(d) if n > 1 else 0.0)  # mean and sample sd of the differences
    half = t_quantile(0.975, n - 1) * s / math.sqrt(n)  # half-width of the 95 % interval
    return {"n": n, "mean_diff": m, "sd_diff": s, "ci95_low": m - half, "ci95_high": m + half,
            "crowds_a_greater": sum(1 for v in d if v > 1e-9), "crowds_b_greater": sum(1 for v in d if v < -1e-9),
            "crowds_equal": sum(1 for v in d if abs(v) <= 1e-9)}


def p_value(r: dict) -> float:  # two-sided p-value of "mean difference = 0" from the paired t statistic
    n, s = r["n"], r["sd_diff"]
    if s == 0:  # all differences equal: p = 1 if they are all zero, else the effect is exact
        return 1.0 if r["mean_diff"] == 0 else 0.0
    t = abs(r["mean_diff"]) / (s / math.sqrt(n))  # t statistic with n - 1 degrees of freedom
    return max(0.0, min(1.0, 2.0 * (1.0 - t_cdf(t, n - 1))))


def holm(ps: dict) -> dict:  # Holm step-down adjusted p-values for a family of tests
    order = sorted(ps, key=ps.get)  # smallest p first
    m, out, run = len(order), {}, 0.0
    for i, k in enumerate(order):
        run = max(run, min(1.0, (m - i) * ps[k]))  # adjusted p can never decrease along the sorted list
        out[k] = run
    return out


def metric(row: dict, name: str):  # one number of one crowd and one rule
    if name == "caught_pct":
        return 100.0 * row["caught"] / row["total"]
    if name == "evacuated_pct":
        return 100.0 * row["evacuated"] / row["total"]
    return row["median_since_alert_s"]  # median walking time from the alert


def main() -> None:
    out = {"t_quantile_0.975_df9": t_quantile(0.975, 9), "scenarios": {}}  # report
    files = [(Path(f).stem, f) for f in sorted(glob.glob(str(RESULTS / "areas" / "*.json")))]  # the ten-crowd scenarios of the paper
    files += [("review/" + Path(f).stem, f) for f in sorted(glob.glob(str(RESULTS / "review" / "*.json")))]  # review runs
    files += [("sens/" + Path(f).stem, f) for f in sorted(glob.glob(str(RESULTS / "sens" / "*.json")))]  # sensitivity runs
    for name, f in files:  # every scenario
        d = json.loads(Path(f).read_text(encoding="utf-8"))  # its saved results
        if "per_seed" not in d:  # old file without individual crowds
            continue
        out["scenarios"][name] = {}
        for m in ("caught_pct", "evacuated_pct", "median_since_alert_s"):  # the metrics
            for a, b in PAIRS:  # the rule pairs
                xa = [metric(r[a], m) for r in d["per_seed"]]
                xb = [metric(r[b], m) for r in d["per_seed"]]
                if any(v is None for v in xa + xb):  # a crowd with nobody evacuated has no median
                    continue
                out["scenarios"][name][f"{m}:{a}-{b}"] = paired(xa, xb)
                out["scenarios"][name][f"{m}:{a}-{b}"]["p"] = p_value(out["scenarios"][name][f"{m}:{a}-{b}"])
    # Holm correction over the family of the nine bomb-drill comparisons "shortest path - congestion aware, people caught"
    fam = {n: s["caught_pct:SHORTEST_PATH-CONGESTION_AWARE"]["p"] for n, s in out["scenarios"].items()
           if "_bomb_L" in n and "caught_pct:SHORTEST_PATH-CONGESTION_AWARE" in s}
    adj = holm(fam)
    out["holm_family_caught_SP_CA"] = {n: {"p": fam[n], "p_holm": adj[n]} for n in fam}
    (RESULTS / "paired.json").write_text(json.dumps(out, indent=2), encoding="utf-8")  # save for the paper
    print(f"t(0.975, 9) = {out['t_quantile_0.975_df9']:.4f} (table value 2.2622)")
    for name in ("campus_bomb_L60", "campus_bomb_L180", "campus_stress", "mangalagiri_bomb_L60", "mangalagiri_closure"):
        for key in ("caught_pct:SHORTEST_PATH-CONGESTION_AWARE", "evacuated_pct:SHORTEST_PATH-CONGESTION_AWARE"):
            r = out["scenarios"][name].get(key)
            if r:
                print(f"{name:24} {key:46} {r['mean_diff']:7.2f}  [{r['ci95_low']:6.2f}, {r['ci95_high']:6.2f}]")


if __name__ == "__main__":
    main()
