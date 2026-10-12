"""Outcome-free profile PCA/clustering and independently evaluated return models."""

from collections import defaultdict
from psycopg2.extras import Json, execute_values
from .core import NUMERIC, FEATURE_NAMES, encode, wilson
from .database import stream
from .pca import decompose, OutcomeProjection
from .return_model import ReturnStudy
from .clustering import (
    MODEL_VERSION,
    RESERVOIR_LIMIT,
    STABILITY_MINIMUM,
    normalize_clusters,
    select_profiles,
    nearest,
    silhouette,
    pairwise_distances,
    ari_from_counts,
)


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


def yearly_associations(groups, baseline):
    """Compare each profile against the others in the same loss year only."""
    results = {}
    for year, (all_yes, all_n) in baseline.items():
        values = []
        for label, years in groups.items():
            yes, n = years.get(year, (0, 0))
            other_n, other_yes = all_n - n, all_yes - yes
            if n < 20 or other_n < 20:
                continue
            values.append(
                {
                    "value": label,
                    "n": n,
                    "returned": yes,
                    "rate": yes / n,
                    "interval": wilson(yes, n),
                    "other_n": other_n,
                    "other_returned": other_yes,
                    "other_rate": other_yes / other_n,
                    "other_interval": wilson(other_yes, other_n),
                    "difference_pp": 100 * (yes / n - other_yes / other_n),
                }
            )
        results[str(year)] = sorted(
            values, key=lambda v: abs(v["difference_pp"]), reverse=True
        )
    return results


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


def train(
    conn, writer, run, clusters, progress, check_stop, batch_size=10000, *, persist=True
):
    import numpy as np

    mode = normalize_clusters(clusters)
    numeric = len(NUMERIC) * 2
    dimension = len(FEATURE_NAMES)
    scales = {}
    return_study = ReturnStudy()
    quality = {
        "invalid_birthdays": 0,
        "missing_features": {name: 0 for name in NUMERIC},
    }
    n = 0
    for rows in stream(conn, run, batch_size):
        check_stop()
        matrix = np.array([encode(r["features"]) for r in rows])
        return_study.normalize(rows)
        quality["invalid_birthdays"] += sum(
            r["features"].get("age_quality") == "invalid_birthday" for r in rows
        )
        for i, name in enumerate(NUMERIC):
            quality["missing_features"][name] += int(matrix[:, i * 2 + 1].sum())
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
        progress(
            "normalizing",
            players=n,
            month=None,
            total_months=None,
            processed_months=None,
        )
    if n == 0 or (mode != "auto" and n < mode):
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
    return_study.prepare()
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

    cohort_moments = {}
    rng = np.random.default_rng(20261011)
    # Uniform diagnostic reservoir; every player contributes to PCA and final fitting.
    reservoir = np.empty((0, dimension))
    keys = np.empty(0)
    seen = 0
    for rows in stream(conn, run, batch_size):
        check_stop()
        x = matrix(rows)
        return_study.fit_batch(rows)
        years = np.array([r["cohort"] for r in rows])
        for year in np.unique(years):
            block = x[years == year]
            stats = cohort_moments.setdefault(
                int(year), [0, np.zeros(dimension), np.zeros((dimension, dimension))]
            )
            stats[0] += len(block)
            stats[1] += block.sum(axis=0)
            stats[2] += block.T @ block
        seen += len(rows)
        reservoir = np.vstack((reservoir, x))
        keys = np.concatenate((keys, rng.random(len(x))))
        keep = np.argsort(keys)[:RESERVOIR_LIMIT]
        reservoir = reservoir[keep]
        keys = keys[keep]
        progress("pca", players=seen, total_players=n)
    return_study.step()
    total = sum((stats[1] for stats in cohort_moments.values()), np.zeros(dimension))
    outer = sum(
        (stats[2] for stats in cohort_moments.values()),
        np.zeros((dimension, dimension)),
    )
    mean, _, vectors, global_pca = decompose(n, total, outer, FEATURE_NAMES)
    yearly_pca = {}
    yearly_axes = {}
    for year, (count, total, cross) in sorted(cohort_moments.items()):
        local_mean, _, local_axes, description = decompose(
            count, total, cross, FEATURE_NAMES
        )
        yearly_pca[str(year)] = description
        yearly_axes[str(year)] = {
            "mean": local_mean.tolist(),
            "axes": local_axes.tolist(),
        }
    components = global_pca["components_retained"]
    progress(
        "pca_ready",
        players=n,
        pca_components=components,
        projection_dimensions=dimension,
        pca_variance_retained=global_pca["retained_variance_ratio"],
    )
    axes = vectors[:, :components]
    pool = (reservoir - mean) @ axes
    solutions, selection, diagnostic, confirmation = select_profiles(
        pool, mode, progress, check_stop
    )
    selection["population_players"] = n
    clusters = selection["selected_k"]
    counts = [np.ones(clusters) for _ in solutions]

    for epoch in range(3):
        seen = 0
        for rows in stream(conn, run, batch_size):
            check_stop()
            z = (matrix(rows) - mean) @ axes
            return_study.fit_batch(rows)
            for replica, centers in enumerate(solutions):
                assigned, _ = nearest(z, centers)
                sizes = np.bincount(assigned, minlength=clusters)
                sums = np.zeros_like(centers)
                np.add.at(sums, assigned, z)
                used = sizes > 0
                weight = counts[replica]
                centers[used] = (weight[used, None] * centers[used] + sums[used]) / (
                    weight[used] + sizes[used]
                )[:, None]
                weight += sizes
            seen += len(rows)
            progress(
                "clustering",
                epoch=epoch + 1,
                players=seen,
                total_players=n,
                selected_clusters=clusters,
                cluster_selection=selection,
            )
        return_study.step()
    # Initial IRLS passes reuse PCA/clustering reads. Additional passes read
    # prepared rows only, until convergence or the bounded iteration limit.
    while not return_study.converged and return_study.iterations < 16:
        seen = 0
        for rows in stream(conn, run, batch_size):
            check_stop()
            return_study.fit_batch(rows)
            seen += len(rows)
            progress(
                "return_model",
                players=seen,
                total_players=n,
                iteration=return_study.iterations + 1,
                message="Fitting adjusted return factors on training characters; held-out outcomes remain excluded.",
            )
        return_study.step()
    # Choose the final start using profile separation only, never return outcomes.
    check_stop()
    diagnostic_distances = pairwise_distances(diagnostic)
    final_scores = [
        (
            silhouette(diagnostic_distances, nearest(diagnostic, centers)[0])
            if len(diagnostic)
            else None
        )
        for centers in solutions
    ]
    best_start = max(
        range(len(solutions)),
        key=lambda i: final_scores[i] if final_scores[i] is not None else -2,
    )
    centers = solutions[best_start]
    selection["final_silhouette_scores"] = final_scores
    selection["final_silhouette"] = final_scores[best_start]
    selection["chosen_seed"] = selection["seeds"][best_start]
    if (
        selection["final_silhouette"] is not None
        and selection["final_silhouette"] < 0.2
    ):
        selection["warnings"].append(
            "Full-population fitting still has weak separation on the diagnostic subset."
        )
    confirmation_distances = pairwise_distances(confirmation)
    selection["final_confirmation_silhouette"] = (
        silhouette(confirmation_distances, nearest(confirmation, centers)[0])
        if len(confirmation)
        else None
    )
    pairs = [
        (a, b) for a in range(len(solutions)) for b in range(a + 1, len(solutions))
    ]
    contingency = [np.zeros((clusters, clusters), dtype=np.int64) for _ in pairs]
    population_sizes = np.zeros(clusters, dtype=np.int64)
    summaries = {}
    outcome_projection = OutcomeProjection(dimension)
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
        coordinates = (matrix(rows) - mean) @ vectors
        return_study.score_batch(rows)
        outcome_projection.add(rows, coordinates)
        z = coordinates[:, :components]
        assignments = [nearest(z, center)[0] for center in solutions]
        assigned = assignments[best_start]
        for counts, (a, b) in zip(contingency, pairs):
            counts += np.bincount(
                assignments[a] * clusters + assignments[b],
                minlength=clusters * clusters,
            ).reshape(clusters, clusters)
        population_sizes += np.bincount(assigned, minlength=clusters)
        inertia += float(((z - centers[assigned]) ** 2).sum())
        updates = []
        for row, cluster, point in zip(rows, assigned, coordinates):
            cid = row["character_id"]
            f = row["features"]
            o = row["outcomes"]
            year = str(row["cohort"])
            if persist:
                updates.append(
                    (
                        cid,
                        int(cluster),
                        Json([float(v) for v in point]),
                        Json(f) if row.get("features_corrected") else None,
                    )
                )
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
        if persist:
            with writer.cursor() as c:
                execute_values(
                    c,
                    """UPDATE research.first_loss_players p SET cluster=v.cluster,projection=v.coordinates::jsonb,
                      features=COALESCE(v.features::jsonb,p.features)
                  FROM (VALUES %s) v(cid,cluster,coordinates,features) WHERE p.run_id=%s AND p.character_id=v.cid"""
                    % ("%s", int(run)),
                    updates,
                    page_size=1000,
                )
            writer.commit()
        seen += len(rows)
        progress("assigning", players=seen, total_players=n)

    selection["population_stability_ari"] = (
        float(np.mean([ari_from_counts(counts) for counts in contingency]))
        if contingency
        else None
    )
    selection["population_cluster_sizes"] = population_sizes.tolist()
    selection["effective_k"] = int((population_sizes > 0).sum())
    if (
        selection["population_stability_ari"] is not None
        and selection["population_stability_ari"] < STABILITY_MINIMUM
    ):
        selection["warnings"].append(
            "Grouping varies between starts on the complete population (ARI below 0.80)."
        )
    if selection["effective_k"] != clusters:
        selection["warnings"].append(
            f"Final assignments occupy {selection['effective_k']} of {clusters} requested groups."
        )

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
        "data_quality": quality,
        "overall": finish(summaries)["all"],
        "years": finish(by_year),
        "clusters": finish(by_cluster),
        "cluster_selection": selection,
        "pca": {
            "global": global_pca,
            "years": yearly_pca,
            "coordinate_dimensions": dimension,
            "coordinates_persisted": persist,
            "coordinate_basis": "Global PCA axes shared by all cohorts. Yearly PCA axes are independently fitted diagnostics.",
        },
        "pca_return": outcome_projection.finish(),
        "factors_by_year": {},
        "factors": {
            h: {
                name: adjusted_association(groups, stat["baseline"])
                for name, groups in stat["groups"].items()
            }
            for h, stat in factors.items()
        },
        "interpretation": "Observed killmail return, not login retention. Associations adjusted for year only, not causal effects.",
        "pca_variance_retained": global_pca["retained_variance_ratio"],
        "cluster_mean_squared_distance": inertia / n,
    }
    for horizon, stat in factors.items():
        annual = {year: {} for year in stat["baseline"]}
        for name, groups in stat["groups"].items():
            for year, effects in yearly_associations(groups, stat["baseline"]).items():
                annual[year][name] = effects
        report["factors_by_year"][horizon] = annual
    model = {
        "version": MODEL_VERSION,
        "feature_names": FEATURE_NAMES,
        "cohort_scaling": {
            str(y): {"mean": m.tolist(), "std": s.tolist()}
            for y, (m, s) in scales.items()
        },
        "pca_mean": mean.tolist(),
        "pca_axes": axes.tolist(),
        "pca_full_axes": vectors.tolist(),
        "pca_by_year": yearly_axes,
        "pca_diagnostics": report["pca"],
        "projection_dimensions": dimension,
        "centers": centers.tolist(),
        "seed": selection["chosen_seed"],
        "cluster_selection": selection,
        "training_players": n,
        "epochs": 3,
        "outcomes_used_for_training": False,
        "outcomes_used_for_profile_training": False,
    }
    model["return_model"], report["return_model"] = return_study.summarize(
        progress, check_stop
    )
    return model, report
