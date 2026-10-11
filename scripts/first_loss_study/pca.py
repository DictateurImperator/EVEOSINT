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
    }
    return mean, values, axes, metadata
