"""Bounded, outcome-free model selection followed by full-population fitting.

Diagnostics compare K=2..30 and three deterministic starts. A uniform reservoir
is split into fitting, selection and confirmation subsets. The chosen K is
subsequently fitted and evaluated across the complete research population.
"""

MODEL_VERSION = 3
SEEDS = (20261011, 20261029, 20261117)
RESERVOIR_LIMIT = 12288
SILHOUETTE_TOLERANCE = 0.01
STABILITY_MINIMUM = 0.8


def normalize_clusters(value):
    text = str(value).strip().lower()
    if text == "auto":
        return "auto"
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError("Choose Auto or between 2 and 30 clusters.") from None
    if not 2 <= number <= 30:
        raise ValueError("Choose Auto or between 2 and 30 clusters.")
    return number


def nearest(points, centers):
    import numpy as np

    # Avoid allocating a batch × centers × components tensor.
    distances = (
        (points * points).sum(axis=1)[:, None]
        + (centers * centers).sum(axis=1)[None, :]
        - 2 * points @ centers.T
    )
    return np.argmin(distances, axis=1), np.maximum(distances.min(axis=1), 0)


def initial_centers(points, count, seed):
    import numpy as np

    rng = np.random.default_rng(seed)
    chosen = [int(rng.integers(len(points)))]
    distances = np.full(len(points), np.inf)
    while len(chosen) < count:
        distances = np.minimum(
            distances, ((points - points[chosen[-1]]) ** 2).sum(axis=1)
        )
        distances[chosen] = 0
        if distances.sum() <= 1e-12:
            raise ValueError(
                "Not enough distinct loss profiles for this cluster count."
            )
        chosen.append(int(rng.choice(len(points), p=distances / distances.sum())))
    return points[chosen].copy()


def pilot_fit(points, count, seed, check_stop):
    import numpy as np

    centers = initial_centers(points, count, seed)
    previous = None
    for iteration in range(25):
        check_stop()
        labels, distances = nearest(points, centers)
        sizes = np.bincount(labels, minlength=count)
        if previous is not None and np.array_equal(labels, previous):
            break
        sums = np.zeros_like(centers)
        np.add.at(sums, labels, points)
        nonempty = sizes > 0
        centers[nonempty] = sums[nonempty] / sizes[nonempty, None]
        farthest = np.argsort(distances)[::-1]
        for position, cluster in enumerate(np.flatnonzero(~nonempty)):
            centers[cluster] = points[farthest[position]]
        previous = labels
    labels, _ = nearest(points, centers)
    sizes = np.bincount(labels, minlength=count)
    if (sizes == 0).any():
        raise ValueError("One or more clusters collapsed during fitting.")
    return centers, sizes, iteration + 1


def pairwise_distances(points):
    import numpy as np

    norms = (points * points).sum(axis=1)
    distances = np.maximum(norms[:, None] + norms[None, :] - 2 * points @ points.T, 0)
    np.sqrt(distances, out=distances)
    np.fill_diagonal(distances, 0)
    return distances


def silhouette(distances, labels):
    import numpy as np

    _, inverse = np.unique(labels, return_inverse=True)
    sizes = np.bincount(inverse)
    if not 1 < len(sizes) < len(labels):
        return None
    sums = np.column_stack(
        [distances[:, inverse == cluster].sum(axis=1) for cluster in range(len(sizes))]
    )
    own = sums[np.arange(len(labels)), inverse] / np.maximum(sizes[inverse] - 1, 1)
    means = sums / sizes
    means[np.arange(len(labels)), inverse] = np.inf
    other = means.min(axis=1)
    denominator = np.maximum(own, other)
    values = np.divide(
        other - own, denominator, out=np.zeros_like(own), where=denominator > 0
    )
    values[sizes[inverse] == 1] = 0
    return float(np.clip(values, -1, 1).mean())


def ari_from_counts(counts):
    """Adjusted Rand index from a contingency table, including large populations."""
    import numpy as np

    counts = np.asarray(counts, dtype=float)
    n = counts.sum()
    if n < 2:
        return 1.0

    def pairs(values):
        return float((values * (values - 1) / 2).sum())

    shared = pairs(counts)
    left = pairs(counts.sum(axis=0))
    right = pairs(counts.sum(axis=1))
    expected = left * right / (n * (n - 1) / 2)
    denominator = (left + right) / 2 - expected
    return (shared - expected) / denominator if abs(denominator) > 1e-12 else 1.0


def adjusted_rand(left, right):
    import numpy as np

    _, a = np.unique(left, return_inverse=True)
    _, b = np.unique(right, return_inverse=True)
    width = int(b.max()) + 1
    counts = np.bincount(a * width + b, minlength=(int(a.max()) + 1) * width)
    return ari_from_counts(counts.reshape(-1, width))


def choose_candidate(candidates):
    valid = [candidate for candidate in candidates if candidate["valid"]]
    if not valid:
        return None, ["No tested cluster count could be evaluated reliably."]
    stable = [
        candidate
        for candidate in valid
        if candidate["stability_ari"] >= STABILITY_MINIMUM
    ]
    pool = stable or valid
    best_score = max(candidate["silhouette_mean"] for candidate in pool)
    chosen = min(
        (
            candidate
            for candidate in pool
            if candidate["silhouette_mean"] >= best_score - SILHOUETTE_TOLERANCE
        ),
        key=lambda candidate: candidate["k"],
    )
    warnings = []
    if not stable:
        warnings.append(
            "No tested candidate reached 0.80 stability; the selected grouping is tentative."
        )
    if chosen["silhouette_mean"] < 0.2:
        warnings.append("Separation between profiles is weak on the diagnostic subset.")
    if chosen["min_training_fraction"] < 0.005:
        warnings.append(
            "Some pilot groups represent fewer than 0.5% of the fitting subset."
        )
    return chosen, warnings


def select_profiles(points, mode, progress, check_stop):
    import numpy as np

    mode = normalize_clusters(mode)
    if not len(points):
        raise ValueError("No loss profiles available for model selection.")
    rng = np.random.default_rng(SEEDS[0])
    points = points[rng.permutation(len(points))]
    metadata = {
        "version": MODEL_VERSION,
        "mode": "auto" if mode == "auto" else "manual",
        "selected_k": None,
        "seeds": list(SEEDS),
        "diagnostic_profiles": len(points),
        "candidates": [],
        "warnings": [],
        "selection_rule": "Prefer candidates with stability ARI ≥ 0.80; choose the smallest K within 0.01 of their best mean silhouette. If none are stable, use the same rule over all valid candidates and flag the choice.",
    }
    if len(points) < 12 or np.max(np.var(points, axis=0)) <= 1e-12:
        if mode != "auto":
            raise ValueError(
                "Too few distinct profiles for model comparison; choose Auto."
            )
        metadata.update(
            selected_k=1,
            fitting_profiles=len(points),
            selection_profiles=0,
            confirmation_profiles=0,
        )
        metadata["warnings"].append(
            "Too few profiles or no profile variation: retained one group without a silhouette score."
        )
        return (
            [np.mean(points, axis=0, keepdims=True)],
            metadata,
            points[:0],
            points[:0],
        )

    fitting_size = min(8192, len(points) * 2 // 3)
    selection_size = min(2048, (len(points) - fitting_size) // 2)
    fitting = points[:fitting_size]
    selection = points[fitting_size : fitting_size + selection_size]
    confirmation = points[fitting_size + selection_size :]
    metadata.update(
        fitting_profiles=len(fitting),
        selection_profiles=len(selection),
        confirmation_profiles=len(confirmation),
    )
    maximum = min(30, len(fitting) - 1)
    counts = list(range(2, maximum + 1)) if mode == "auto" else [mode]
    metadata["tested_range"] = [counts[0], counts[-1]]
    selection_distances = pairwise_distances(selection)
    fitted = {}
    for index, count in enumerate(counts):
        check_stop()
        progress(
            "selecting_profiles",
            tested_candidates=index,
            total_candidates=len(counts),
            candidate_k=count,
            cluster_selection=metadata,
        )
        row = {"k": count, "valid": False}
        centers = []
        labels = []
        scores = []
        fractions = []
        iterations = []
        try:
            for seed in SEEDS:
                check_stop()
                center, sizes, iteration = pilot_fit(fitting, count, seed, check_stop)
                label, _ = nearest(selection, center)
                score = silhouette(selection_distances, label)
                if score is None:
                    raise ValueError("Too few distinct groups in the selection subset.")
                centers.append(center)
                labels.append(label)
                scores.append(score)
                fractions.append(float(sizes.min() / len(fitting)))
                iterations.append(iteration)
            stability = float(
                np.mean(
                    [
                        adjusted_rand(labels[a], labels[b])
                        for a, b in ((0, 1), (0, 2), (1, 2))
                    ]
                )
            )
            row.update(
                valid=True,
                silhouette_mean=float(np.mean(scores)),
                silhouette_std=float(np.std(scores)),
                silhouette_seeds=scores,
                stability_ari=stability,
                min_training_fraction=min(fractions),
                fitting_iterations=max(iterations),
            )
            fitted[count] = centers
        except ValueError as exc:
            row["reason"] = str(exc)
        metadata["candidates"].append(row)
        progress(
            "selecting_profiles",
            tested_candidates=index + 1,
            total_candidates=len(counts),
            cluster_selection=metadata,
        )
    chosen, warnings = choose_candidate(metadata["candidates"])
    if chosen is None:
        if mode != "auto":
            raise ValueError(
                "The requested cluster count could not be evaluated: "
                + metadata["candidates"][0]["reason"]
            )
        metadata.update(selected_k=1, warnings=warnings)
        return (
            [np.mean(points, axis=0, keepdims=True)],
            metadata,
            selection,
            confirmation,
        )
    metadata["selected_k"] = chosen["k"]
    metadata["warnings"] = warnings
    if mode == "auto" and chosen["k"] == maximum:
        metadata["warnings"].append(
            "The selected count is the largest tested; finer structure may remain."
        )
    confirmation_distances = pairwise_distances(confirmation)
    confirmation_scores = [
        silhouette(confirmation_distances, nearest(confirmation, center)[0])
        for center in fitted[chosen["k"]]
    ]
    metadata["pilot_confirmation_scores"] = confirmation_scores
    known = [score for score in confirmation_scores if score is not None]
    if known and np.mean(known) < chosen["silhouette_mean"] - 0.1:
        metadata["warnings"].append(
            "Separation is lower on the independent confirmation subset; interpret the grouping cautiously."
        )
    progress("profiles_selected", candidate_k=chosen["k"], cluster_selection=metadata)
    return fitted[chosen["k"]], metadata, selection, confirmation
