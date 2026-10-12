"""PCA diagnostics from complete-population streaming moments."""

VARIANCE_TARGET = 0.90


def decompose(count, total, cross_products, names):
    """Return every axis and its variance; retain enough axes for the target.

    Outcome labels never enter these moments. Covariance uses the population
    denominator; variance ratios and eigenvectors are unaffected by this choice.
    """
    import numpy as np

    if count < 1:
        raise ValueError("PCA needs at least one profile.")
    dimension = len(names)
    mean = np.asarray(total, dtype=float) / count
    covariance = np.asarray(cross_products, dtype=float) / count - np.outer(mean, mean)
    covariance = (covariance + covariance.T) / 2
    values, axes = np.linalg.eigh(covariance)
    order = np.argsort(values)[::-1]
    values, axes = values[order], axes[:, order]
    tolerance = (
        np.finfo(float).eps * dimension * max(float(np.max(np.abs(values))), 1.0)
    )
    values = np.where(values > tolerance, values, 0.0)
    # Canonical signs make recorded coefficients reproducible. Signs still have
    # no inherent interpretation, and independently fitted yearly axes differ.
    for axis in range(dimension):
        anchor = np.argmax(np.abs(axes[:, axis]))
        if axes[anchor, axis] < 0:
            axes[:, axis] *= -1
    variance = float(values.sum())
    ratios = values / variance if variance else np.zeros(dimension)
    cumulative = np.minimum(np.cumsum(ratios), 1.0)
    retained = (
        min(dimension, int(np.searchsorted(cumulative, VARIANCE_TARGET - 1e-12)) + 1)
        if variance
        else 1
    )
    spectrum = []
    for index in range(dimension):
        ordered = np.argsort(axes[:, index] ** 2)[::-1][:6] if values[index] else []
        spectrum.append(
            {
                "component": index + 1,
                "variance_ratio": float(ratios[index]) if variance else None,
                "cumulative_ratio": float(cumulative[index]) if variance else None,
                "retained": bool(variance and index < retained),
                "top_features": [
                    {
                        "name": names[i],
                        "coefficient": float(axes[i, index]),
                        "squared_coefficient": float(axes[i, index] ** 2),
                    }
                    for i in ordered
                ],
            }
        )
    metadata = {
        "players": int(count),
        "dimensions": dimension,
        "rank": int((values > 0).sum()),
        "components_retained": retained,
        "variance_target": VARIANCE_TARGET,
        "retained_variance_ratio": (
            float(cumulative[retained - 1]) if variance else None
        ),
        "eigenvalues": values.tolist(),
        "spectrum": spectrum,
        "variables": variable_correlations(covariance, values, axes, names),
    }
    return mean, values, axes, metadata


def variable_correlations(covariance, eigenvalues, axes, names):
    """Correlation-circle coordinates, rather than raw vector coefficients."""
    import numpy as np

    variance = np.maximum(0, np.diag(covariance))
    result = []
    for index, name in enumerate(names):
        values = []
        for component, eigenvalue in enumerate(eigenvalues):
            correlation = (
                float(axes[index, component] * np.sqrt(eigenvalue / variance[index]))
                if variance[index] > 1e-12 and eigenvalue > 0
                else None
            )
            values.append(correlation)
        result.append(dict(name=name, correlations=values))
    return result


class OutcomeProjection:
    """Treat return as a supplementary label; never use it to form PCA axes."""

    def __init__(self, dimension):
        import numpy as np

        self.np = np
        self.dimension = dimension
        self.stats = {}
        self.sample = []
        self.keys = np.empty(0)
        self.rng = np.random.default_rng(20261013)

    def add(self, rows, coordinates):
        np = self.np
        years = np.array([str(r["cohort"]) for r in rows])
        for scope in ("all", "pvp", "npc"):
            initial = np.array(
                [
                    scope == "all"
                    or (scope == "pvp" and r["outcomes"].get("initial_pvp") is True)
                    or (
                        scope == "npc" and r["features"].get("opponent") == "NPC/object"
                    )
                    for r in rows
                ]
            )
            for horizon in (7, 30, 90):
                eligible = initial & np.array(
                    [r["outcomes"][str(horizon)]["followup_complete"] for r in rows]
                )
                yes = np.array(
                    [r["outcomes"][str(horizon)]["returned"] is True for r in rows]
                )
                for year in ["all", *np.unique(years)]:
                    mask = eligible if year == "all" else eligible & (years == year)
                    if not mask.any():
                        continue
                    z, returned = coordinates[mask], yes[mask]
                    stat = self.stats.setdefault(
                        (scope, str(horizon), str(year)),
                        [
                            0,
                            0,
                            np.zeros(self.dimension),
                            np.zeros(self.dimension),
                            np.zeros(self.dimension),
                        ],
                    )
                    stat[0] += len(z)
                    stat[1] += int(returned.sum())
                    stat[2] += z.sum(axis=0)
                    stat[3] += (z**2).sum(axis=0)
                    stat[4] += z[returned].sum(axis=0)
        self.sample.extend(
            dict(
                character_id=r["character_id"],
                year=str(r["cohort"]),
                coordinates=z.tolist(),
                ship_role=r["features"].get("ship_role", "unknown"),
                initial_pvp=r["outcomes"].get("initial_pvp"),
                opponent=r["features"].get("opponent"),
                outcomes={
                    str(h): dict(
                        complete=r["outcomes"][str(h)]["followup_complete"],
                        returned=r["outcomes"][str(h)]["returned"],
                    )
                    for h in (7, 30, 90)
                },
            )
            for r, z in zip(rows, coordinates)
        )
        self.keys = np.concatenate((self.keys, self.rng.random(len(rows))))
        indices = np.argsort(self.keys)[:2048]
        self.sample = [self.sample[i] for i in indices]
        self.keys = self.keys[indices]

    def finish(self):
        np = self.np
        result = dict(
            scopes={},
            sample=self.sample,
            sample_method="Uniform reservoir of at most 2048 characters for display only; all eligible characters enter axis-return statistics.",
            basis="Shared global PCA; outcomes are supplementary labels.",
        )
        for (scope, horizon, year), (
            n,
            yes,
            total,
            squares,
            positive,
        ) in self.stats.items():
            mean = total / n
            variance = np.maximum(0, squares / n - mean**2)
            p = yes / n
            covariance = positive / n - mean * p
            axes = []
            for component in range(self.dimension):
                correlation = (
                    float(
                        covariance[component]
                        / np.sqrt(variance[component] * p * (1 - p))
                    )
                    if variance[component] > 1e-12 and 0 < p < 1
                    else None
                )
                axes.append(
                    dict(
                        component=component + 1,
                        correlation=correlation,
                        association_r_squared=(
                            correlation**2 if correlation is not None else None
                        ),
                        returned_mean=float(positive[component] / yes) if yes else None,
                        no_return_mean=(
                            float((total[component] - positive[component]) / (n - yes))
                            if n > yes
                            else None
                        ),
                    )
                )
            result["scopes"].setdefault(scope, {}).setdefault(horizon, {})[year] = dict(
                n=n, returned=yes, axes=axes
            )
        return result
