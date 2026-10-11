"""Streaming PCA and full-population clustering; outcomes used only in the report."""

from collections import defaultdict
from psycopg2.extras import Json, execute_values
from .core import NUMERIC, FEATURE_NAMES, encode, wilson
from .database import stream


def adjusted_association(groups, baseline):
    effects = []
    for label, years in groups.items():
        n = yes = 0
        weighted = 0.0
        support = 0
        for year, (g_y, g_n) in years.items():
            b_y, b_n = baseline[year]
            other_n = b_n - g_n
            if g_n < 20 or other_n < 20:
                continue
            n += g_n
            yes += g_y
            support += 1
            weighted += g_n * (g_y / g_n - (b_y - g_y) / other_n)
        if n:
            effects.append(
                {
                    "value": label,
                    "n": n,
                    "returned": yes,
                    "rate": yes / n,
                    "interval": wilson(yes, n),
                    "year_adjusted_difference_pp": 100 * weighted / n,
                    "supported_years": support,
                }
            )
    return sorted(
        effects, key=lambda x: abs(x["year_adjusted_difference_pp"]), reverse=True
    )


def factor_values(f):
    age = f.get("age_days")
    activity = f.get("corp_attacks_30")
    attackers = f.get("players_attacking")
    implants = f.get("implants")
    return {
        "space": f.get("space", "unknown"),
        "ship_role": f.get("ship_role", "unknown"),
        "opponent": f.get("opponent", "unknown"),
        "corp_kind": f.get("corp_kind", "unknown"),
        "age": (
            "unknown"
            if age is None
            else (
                "<7 days"
                if age < 7
                else (
                    "7–30 days"
                    if age < 30
                    else (
                        "30–365 days"
                        if age < 365
                        else "1–5 years" if age < 1825 else "5+ years"
                    )
                )
            )
        ),
        "corp_attack_activity": (
            "unknown"
            if activity is None
            else (
                "0"
                if activity == 0
                else (
                    "1–10"
                    if activity <= 10
                    else (
                        "11–100"
                        if activity <= 100
                        else "101–1000" if activity <= 1000 else "1000+"
                    )
                )
            )
        ),
        "player_attackers": (
            "unknown"
            if attackers is None
            else (
                "0"
                if attackers == 0
                else (
                    "1"
                    if attackers == 1
                    else (
                        "2–5"
                        if attackers <= 5
                        else "6–20" if attackers <= 20 else "21+"
                    )
                )
            )
        ),
        "pod_implants": (
            "not a pod"
            if f.get("ship_role") != "pod"
            else (
                "unknown"
                if implants is None
                else "none recorded" if implants == 0 else "implants recorded"
            )
        ),
        "history_since_birth": (
            "covered" if f.get("observed_since_birth") else "not established"
        ),
        "space × ship_role": f.get("space", "unknown")
        + " × "
        + f.get("ship_role", "unknown"),
    }


def evolution_bucket():
    return {
        "returned": 0,
        "same_zone": 0,
        "zone_known": 0,
        "same_system": 0,
        "system_known": 0,
        "same_region": 0,
        "region_known": 0,
        "same_ship": 0,
        "ship_known": 0,
        "same_ship_role": 0,
        "ship_role_known": 0,
        "repeat_losses": 0,
        "attacks": 0,
        "zone_transitions": {},
        "ship_role_transitions": {},
    }


def add_evolution(bucket, features, outcomes, horizon):
    bucket["returned"] += 1
    for key in ("zone", "system", "region", "ship", "ship_role"):
        v = outcomes.get("same_" + key)
        if v is not None:
            bucket[key + "_known"] += 1
            bucket["same_" + key] += int(v)
    if horizon != 7:
        bucket["repeat_losses"] += int(outcomes.get("losses_" + str(horizon), 0) > 0)
        bucket["attacks"] += int(outcomes.get("attacks_" + str(horizon), 0) > 0)
    for key in ("zone", "ship_role"):
        initial = features.get("space" if key == "zone" else key, "unknown")
        later = outcomes.get("return_" + key, "unknown")
        if initial != "unknown" and later not in (None, "unknown"):
            label = initial + " → " + later
            counts = bucket[key + "_transitions"]
            counts[label] = counts.get(label, 0) + 1


def train(conn, writer, run, clusters, progress, check_stop, batch_size=10000):
    import numpy as np

    numeric = len(NUMERIC) * 2
    dimension = len(FEATURE_NAMES)
    scales = {}
    n = 0
    for rows in stream(conn, run, batch_size):
        check_stop()
        matrix = np.array([encode(r["features"]) for r in rows])
        years = np.array([r["cohort"] for r in rows])
        for year in np.unique(years):
            block = matrix[years == year]
            known = 1 - block[:, 1:numeric:2]
            x = block[:, 0:numeric:2]
            stats = scales.setdefault(
                int(year),
                [
                    np.zeros(len(NUMERIC)),
                    np.zeros(len(NUMERIC)),
                    np.zeros(len(NUMERIC)),
                ],
            )
            stats[0] += known.sum(axis=0)
            stats[1] += (x * known).sum(axis=0)
            stats[2] += (x * x * known).sum(axis=0)
        n += len(rows)
        progress("normalizing", players=n)
    if n < clusters:
        raise ValueError("Fewer loss-bearing characters than requested clusters.")
    with conn.cursor() as c:
        c.execute(
            "SELECT count(*) FROM research.first_loss_players WHERE run_id=%s", (run,)
        )
        if c.fetchone()[0] != n:
            raise ValueError(
                "Some loss-bearing characters have no features; study is incomplete."
            )
    conn.rollback()
    for year, (count, total, squares) in list(scales.items()):
        mean = np.divide(total, count, out=np.zeros_like(total), where=count > 0)
        variance = np.maximum(
            0,
            np.divide(squares, count, out=np.zeros_like(total), where=count > 0)
            - mean * mean,
        )
        scales[year] = (mean, np.maximum(np.sqrt(variance), 0.1))

    def matrix(rows):
        x = np.array([encode(r["features"]) for r in rows])
        years = np.array([r["cohort"] for r in rows])
        for year in np.unique(years):
            mask = years == year
            mean, std = scales[int(year)]
            block = x[mask]
            block[:, 0:numeric:2] = np.clip(
                (block[:, 0:numeric:2] - mean) / std, -5, 5
            ) * (1 - block[:, 1:numeric:2])
            x[mask] = block
        return x

    total = np.zeros(dimension)
    outer = np.zeros((dimension, dimension))
    rng = np.random.default_rng(20261011)
    # Random-key reservoir initializes centers only; every player trains and receives a label.
    reservoir = np.empty((0, dimension))
    keys = np.empty(0)
    seen = 0
    for rows in stream(conn, run, batch_size):
        check_stop()
        x = matrix(rows)
        total += x.sum(axis=0)
        outer += x.T @ x
        seen += len(rows)
        reservoir = np.vstack((reservoir, x))
        keys = np.concatenate((keys, rng.random(len(x))))
        keep = np.argsort(keys)[: max(2048, clusters)]
        reservoir = reservoir[keep]
        keys = keys[keep]
        progress("pca", players=seen, total_players=n)
    mean = total / n
    cov = outer / n - np.outer(mean, mean)
    values, vectors = np.linalg.eigh(cov)
    order = np.argsort(values)[::-1]
    values = np.maximum(values[order], 0)
    vectors = vectors[:, order]
    components = min(
        16,
        dimension,
        max(
            2,
            int(np.searchsorted(np.cumsum(values) / max(values.sum(), 1e-12), 0.9)) + 1,
        ),
    )
    axes = vectors[:, :components]
    pool = (reservoir - mean) @ axes
    chosen = [int(rng.integers(len(pool)))]
    dist = np.full(len(pool), np.inf)
    while len(chosen) < clusters:
        dist = np.minimum(dist, ((pool - pool[chosen[-1]]) ** 2).sum(axis=1))
        dist[chosen] = 0
        if dist.sum() > 0:
            chosen.append(int(rng.choice(len(pool), p=dist / dist.sum())))
        else:
            chosen.append(next(i for i in range(len(pool)) if i not in chosen))
    centers = pool[chosen].copy()
    counts = np.ones(clusters)

    def labels(z):
        return np.argmin(
            ((z[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2), axis=1
        )

    for epoch in range(3):
        seen = 0
        for rows in stream(conn, run, batch_size):
            check_stop()
            z = (matrix(rows) - mean) @ axes
            assigned = labels(z)
            for k in range(clusters):
                block = z[assigned == k]
                if len(block):
                    centers[k] = (counts[k] * centers[k] + block.sum(axis=0)) / (
                        counts[k] + len(block)
                    )
                    counts[k] += len(block)
            seen += len(rows)
            progress("clustering", epoch=epoch + 1, players=seen, total_players=n)
    summaries = {}
    by_year = {}
    by_cluster = {}
    factors = {}
    inertia = 0.0
    seen = 0

    def bucket(store, key):
        return store.setdefault(
            str(key),
            {
                "n": 0,
                "horizons": {
                    str(h): {
                        "eligible": 0,
                        "returned": 0,
                        "pvp": 0,
                        "returned_without_pvp": 0,
                        "incomplete_returned": 0,
                        "incomplete_pvp": 0,
                        "evolution": {
                            "pvp": evolution_bucket(),
                            "without_pvp": evolution_bucket(),
                        },
                    }
                    for h in (7, 30, 90)
                },
                "profile_counts": {
                    key: {} for key in ("space", "ship_role", "opponent", "corp_kind")
                },
            },
        )

    for rows in stream(conn, run, batch_size):
        check_stop()
        z = (matrix(rows) - mean) @ axes
        assigned = labels(z)
        inertia += float(((z - centers[assigned]) ** 2).sum())
        updates = []
        for row, cluster, point in zip(rows, assigned, z):
            cid = row["character_id"]
            f = row["features"]
            o = row["outcomes"]
            year = str(row["cohort"])
            updates.append((cid, int(cluster), Json([float(v) for v in point[:2]])))
            for store, key in [
                (summaries, "all"),
                (by_year, year),
                (by_cluster, int(cluster)),
            ]:
                b = bucket(store, key)
                b["n"] += 1
                for name, counts in b["profile_counts"].items():
                    value = f.get(name, "unknown")
                    counts[value] = counts.get(value, 0) + 1
                for horizon in (7, 30, 90):
                    obs = o[str(horizon)]
                    h = b["horizons"][str(horizon)]
                    if not obs["followup_complete"]:
                        h["incomplete_returned"] += int(obs["returned"] is True)
                        h["incomplete_pvp"] += int(obs["pvp_returned"] is True)
                        continue
                    h["eligible"] += 1
                    h["returned"] += int(obs["returned"])
                    h["pvp"] += int(obs["pvp_returned"])
                    h["returned_without_pvp"] += int(
                        obs["returned"] and not obs["pvp_returned"]
                    )
                    if obs["returned"]:
                        add_evolution(
                            h["evolution"][
                                "pvp" if obs["pvp_returned"] else "without_pvp"
                            ],
                            f,
                            o,
                            horizon,
                        )
            for horizon in (7, 30, 90):
                obs = o[str(horizon)]
                if not obs["followup_complete"]:
                    continue
                stat = factors.setdefault(
                    str(horizon),
                    {"baseline": defaultdict(lambda: [0, 0]), "groups": {}},
                )
                baseline = stat["baseline"][year]
                baseline[0] += int(obs["returned"])
                baseline[1] += 1
                for name, value in dict(factor_values(f), cluster=str(cluster)).items():
                    group = stat["groups"].setdefault(
                        name, defaultdict(lambda: defaultdict(lambda: [0, 0]))
                    )
                    count = group[value][year]
                    count[0] += int(obs["returned"])
                    count[1] += 1
        with writer.cursor() as c:
            execute_values(
                c,
                """UPDATE research.first_loss_players p SET cluster=v.cluster,projection=v.xy::jsonb
              FROM (VALUES %s) v(cid,cluster,xy) WHERE p.run_id=%s AND p.character_id=v.cid"""
                % ("%s", int(run)),
                updates,
                page_size=1000,
            )
        writer.commit()
        seen += len(rows)
        progress("assigning", players=seen, total_players=n)

    def finish(store):
        for b in store.values():
            for h in b["horizons"].values():
                h["return_interval"] = wilson(h["returned"], h["eligible"])
                h["return_rate"] = (
                    h["returned"] / h["eligible"] if h["eligible"] else None
                )
        return store

    report = {
        "players": n,
        "overall": finish(summaries)["all"],
        "years": finish(by_year),
        "clusters": finish(by_cluster),
        "factors": {
            h: {
                name: adjusted_association(groups, stat["baseline"])
                for name, groups in stat["groups"].items()
            }
            for h, stat in factors.items()
        },
        "interpretation": "Observed killmail return, not login retention. Associations adjusted for year only, not causal effects.",
        "pca_variance_retained": float(
            values[:components].sum() / max(values.sum(), 1e-12)
        ),
        "cluster_mean_squared_distance": inertia / n,
    }
    model = {
        "feature_names": FEATURE_NAMES,
        "cohort_scaling": {
            str(y): {"mean": m.tolist(), "std": s.tolist()}
            for y, (m, s) in scales.items()
        },
        "pca_mean": mean.tolist(),
        "pca_axes": axes.tolist(),
        "centers": centers.tolist(),
        "seed": 20261011,
        "training_players": n,
        "epochs": 3,
        "outcomes_used_for_training": False,
    }
    return model, report
