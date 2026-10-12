"""Supervised return analysis, independent of PCA and cluster selection.

All eligible characters enter a reproducible 80/20 train/test split. Logistic
models control loss year and the other initial-loss variables. Streaming IRLS
keeps only small Hessians in memory; evaluation uses every held-out character.
"""

import hashlib
from collections import defaultdict
import numpy as np
from .core import NUMERIC, CATEGORIES, encode

SEED = 20261012
RIDGE = 0.001
AGE_LEVELS = ("<7 days", "7-30 days", "30-365 days", "1-5 years", "5+ years", "unknown")
CATS = dict(age=AGE_LEVELS, **CATEGORIES)
REFERENCES = dict(
    age="<7 days",
    space="HS",
    ship_role="combat",
    opponent="players",
    corp_kind="player",
)
SCOPES = ("all", "pvp", "npc")
HORIZONS = (7, 30, 90)
CONTINUOUS = tuple(name for name in NUMERIC if name != "age_days")


def age_level(features):
    age = features.get("age_days")
    if age is None:
        return "unknown"
    return AGE_LEVELS[
        0 if age < 7 else 1 if age < 30 else 2 if age < 365 else 3 if age < 1825 else 4
    ]


def is_test(character_id):
    # Do not split by ID modulo: character IDs themselves can encode era.
    digest = hashlib.blake2b(f"{SEED}:{character_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 5 == 0


def sigmoid(value):
    return 1 / (1 + np.exp(-np.clip(value, -40, 40)))


def heldout(row):
    if "_return_test" not in row:
        row["_return_test"] = is_test(row["character_id"])
    return row["_return_test"]


def importance_rows(moments, n, enabled):
    values = []
    for name, (total, squares) in moments.items():
        delta = total / n
        se = np.sqrt(max(0, squares / n - delta**2) / max(1, n - 1))
        values.append(
            dict(
                factor=name,
                delta_brier=float(delta),
                interval=[float(delta - 1.96 * se), float(delta + 1.96 * se)],
            )
        )
    gain = sum(max(0, v["delta_brier"]) for v in values)
    for v in values:
        v["importance_percent"] = (
            100 * max(0, v["delta_brier"]) / gain if enabled and gain > 1e-12 else None
        )
    return sorted(values, key=lambda v: v["delta_brier"], reverse=True)


class ReturnStudy:
    def __init__(self):
        self.scales = {}
        self.models = {}
        self.iterations = 0
        self.test_count = 0
        self.groups = {}
        self.names = []
        for name in CONTINUOUS:
            self.groups[name] = [len(self.names), len(self.names) + 1]
            self.names.extend((name, name + "_missing"))
        for name, levels in CATS.items():
            self.groups[name] = []
            for level in levels:
                if level != REFERENCES[name]:
                    self.groups[name].append(len(self.names))
                    self.names.append(name + "=" + level)
        self.index = {name: i for i, name in enumerate(self.names)}
        self.numeric_sources = [NUMERIC.index(name) for name in CONTINUOUS]

    def normalize(self, rows):
        """Learn preprocessing from training characters only, before any labels."""
        for row in rows:
            if heldout(row):
                self.test_count += 1
                continue
            values = np.asarray(encode(row["features"]), dtype=float)[
                : len(NUMERIC) * 2
            ].reshape(-1, 2)
            count, total, squares = self.scales.setdefault(
                int(row["cohort"]), np.zeros((3, len(NUMERIC)))
            )
            known = 1 - values[:, 1]
            count += known
            total += values[:, 0] * known
            squares += values[:, 0] ** 2 * known

    def prepare(self):
        self.years = sorted(self.scales)
        for year, (count, total, squares) in list(self.scales.items()):
            mean = np.divide(total, count, out=np.zeros_like(total), where=count > 0)
            variance = np.maximum(
                0,
                np.divide(squares, count, out=np.zeros_like(total), where=count > 0)
                - mean**2,
            )
            self.scales[year] = (mean, np.maximum(np.sqrt(variance), 0.1))
        self.design_names = (
            self.names + ["intercept"] + ["year=" + str(y) for y in self.years[1:]]
        )
        self.dimension = len(self.design_names)
        self.allocate_test(self.test_count)
        for scope in SCOPES:
            for horizon in HORIZONS:
                self.models[(scope, horizon)] = {
                    "beta": np.zeros(self.dimension),
                    "n": 0,
                    "hessian": np.zeros((self.dimension, self.dimension)),
                    "gradient": np.zeros(self.dimension),
                    "baseline": {},
                    "counts": defaultdict(lambda: defaultdict(lambda: [0, 0])),
                    "counts_by_year": defaultdict(
                        lambda: defaultdict(lambda: defaultdict(lambda: [0, 0]))
                    ),
                    "evaluation": {},
                    "sandwich": np.zeros((self.dimension, self.dimension)),
                    "fisher": np.zeros((self.dimension, self.dimension)),
                }

    def design(self, rows):
        x = np.zeros((len(rows), self.dimension))
        years = {year: i for i, year in enumerate(self.years)}
        for index, row in enumerate(rows):
            f = row["features"]
            raw = np.asarray(encode(f))[: len(NUMERIC) * 2].reshape(-1, 2)
            mean, std = self.scales.get(
                int(row["cohort"]), (np.zeros(len(NUMERIC)), np.ones(len(NUMERIC)))
            )
            scaled = np.clip((raw[:, 0] - mean) / std, -5, 5) * (1 - raw[:, 1])
            for j, source in enumerate(self.numeric_sources):
                x[index, 2 * j : 2 * j + 2] = (scaled[source], raw[source, 1])
            for name in CATS:
                level = age_level(f) if name == "age" else f.get(name, "unknown")
                encoded = name + "=" + level
                column = self.index.get(encoded)
                if column is not None:
                    x[index, column] = 1
            x[index, len(self.names)] = 1
            year = years.get(int(row["cohort"]), 0)
            if year:
                x[index, len(self.names) + year] = 1
        return x

    def masks(self, rows, scope, horizon, training):
        return np.array(
            [
                heldout(r) != training
                and r["outcomes"][str(horizon)]["followup_complete"]
                and (
                    scope == "all"
                    or (scope == "pvp" and r["outcomes"].get("initial_pvp") is True)
                    or (
                        scope == "npc" and r["features"].get("opponent") == "NPC/object"
                    )
                )
                for r in rows
            ],
            dtype=bool,
        )

    def fit_batch(self, rows):
        x = self.design(rows)
        if self.iterations == 0:
            self.collect_test(rows, x)
        for (scope, horizon), model in self.models.items():
            if model.get("unavailable"):
                continue
            mask = self.masks(rows, scope, horizon, True)
            if not mask.any():
                continue
            block = x[mask]
            selected = [r for r, use in zip(rows, mask) if use]
            y = np.array(
                [r["outcomes"][str(horizon)]["returned"] for r in selected], dtype=float
            )
            p = sigmoid(block @ model["beta"])
            model["n"] += len(block)
            model["gradient"] += block.T @ (y - p)
            model["hessian"] += block.T @ (block * (p * (1 - p))[:, None])
            if self.iterations == 0:
                for r, returned in zip(selected, y):
                    year = str(r["cohort"])
                    stat = model["baseline"].setdefault(year, [0, 0])
                    stat[0] += int(returned)
                    stat[1] += 1
                    for name in CATS:
                        level = (
                            age_level(r["features"])
                            if name == "age"
                            else r["features"].get(name, "unknown")
                        )
                        stat = model["counts"][name][level]
                        stat[0] += int(returned)
                        stat[1] += 1
                        stat = model["counts_by_year"][year][name][level]
                        stat[0] += int(returned)
                        stat[1] += 1

    def step(self):
        largest = 0.0
        for model in self.models.values():
            if model.get("unavailable"):
                continue
            n = model["n"]
            returned = sum(v[0] for v in model["baseline"].values())
            if n < 50 or returned == 0 or returned == n:
                model["unavailable"] = (
                    "At least 50 complete training characters and both return outcomes are required."
                )
                continue
            penalty = np.ones(self.dimension) * RIDGE
            penalty[len(self.names)] = 0
            matrix = model["hessian"] / n + np.diag(penalty)
            gradient = model["gradient"] / n - penalty * model["beta"]
            delta = np.linalg.solve(matrix, gradient)
            # Trust bound prevents a rare/separated dummy from destabilizing IRLS.
            delta *= min(1.0, 2 / max(float(np.max(np.abs(delta))), 1e-12))
            model["beta"] += delta
            largest = max(largest, float(np.max(np.abs(delta))))
            model["training_n"] = n
            model["bread"] = np.linalg.inv(matrix) / n
            model["n"] = 0
            model["gradient"].fill(0)
            model["hessian"].fill(0)
        self.iterations += 1
        self.converged = largest < 0.0001
        return self.converged

    def score_batch(self, rows):
        x = self.design(rows)
        for (scope, horizon), model in self.models.items():
            if model.get("unavailable"):
                continue
            train = self.masks(rows, scope, horizon, True)
            if train.any():
                block = x[train]
                y = np.array(
                    [
                        r["outcomes"][str(horizon)]["returned"]
                        for r, use in zip(rows, train)
                        if use
                    ],
                    dtype=float,
                )
                probability = sigmoid(block @ model["beta"])
                residual = y - probability
                model["sandwich"] += block.T @ (block * residual[:, None] ** 2)
                model["fisher"] += block.T @ (
                    block * (probability * (1 - probability))[:, None]
                )
            test = self.masks(rows, scope, horizon, False)
            if not test.any():
                continue
            selected = [r for r, use in zip(rows, test) if use]
            block = x[test]
            years = np.array([int(r["cohort"]) for r in selected])
            y = np.array(
                [r["outcomes"][str(horizon)]["returned"] for r in selected], dtype=float
            )
            beta = model["beta"]
            linear = block @ beta
            p = sigmoid(linear)
            for year in np.unique(years):
                used = years == year
                bx, by, bp, bl = block[used], y[used], p[used], linear[used]
                yes, n = model["baseline"].get(str(year), (0, 0))
                base = (
                    yes / n
                    if n
                    else sum(v[0] for v in model["baseline"].values())
                    / model["training_n"]
                )
                brier = (bp - by) ** 2
                stats = model["evaluation"].setdefault(str(year), self.new_stats())
                stats["n"] += len(by)
                stats["returned"] += int(by.sum())
                stats["brier"] += float(brier.sum())
                stats["baseline_brier"] += float(((base - by) ** 2).sum())
                stats["log_loss"] += float(
                    -(
                        by * np.log(np.maximum(bp, 1e-12))
                        + (1 - by) * np.log(np.maximum(1 - bp, 1e-12))
                    ).sum()
                )
                stats["auc_positive"] += np.bincount(
                    np.minimum((bp[by == 1] * 1024).astype(int), 1023), minlength=1024
                )
                stats["auc_negative"] += np.bincount(
                    np.minimum((bp[by == 0] * 1024).astype(int), 1023), minlength=1024
                )
                # Average probability contrasts; delta-method gradient is stored
                # for a training-only robust covariance, not a PCA coefficient.
                for name, levels in CATS.items():
                    group_columns = self.groups[name]
                    neutral = bl - bx[:, group_columns] @ beta[group_columns]
                    reference_p = sigmoid(neutral)
                    reference_grad = bx.copy()
                    reference_grad[:, group_columns] = 0
                    reference_grad *= (reference_p * (1 - reference_p))[:, None]
                    for level in levels:
                        if level == REFERENCES[name]:
                            continue
                        support = model["counts_by_year"][str(year)][name]
                        if support[level][1] < 20 or support[REFERENCES[name]][1] < 20:
                            continue
                        column = self.names.index(name + "=" + level)
                        changed = sigmoid(neutral + beta[column])
                        grad = bx.copy()
                        grad[:, group_columns] = 0
                        grad[:, column] = 1
                        grad *= (changed * (1 - changed))[:, None]
                        key = name + "=" + level
                        stat = stats["contrasts"].setdefault(
                            key, [0, 0.0, np.zeros(self.dimension)]
                        )
                        stat[0] += len(by)
                        stat[1] += float((changed - reference_p).sum())
                        stat[2] += (grad - reference_grad).sum(axis=0)
                for name in CONTINUOUS:
                    value, missing = self.groups[name]
                    known = bx[:, missing] == 0
                    if not known.any():
                        continue
                    increment = np.minimum(1, 5 - bx[known, value])
                    if name.endswith("_fraction"):
                        mean, std = self.scales[int(year)]
                        source = NUMERIC.index(name)
                        upper = (1 - mean[source]) / std[source]
                        increment = np.maximum(
                            0, np.minimum(increment, upper - bx[known, value])
                        )
                    changed = sigmoid(bl[known] + beta[value] * increment)
                    grad = bx[known].copy()
                    grad[:, value] += increment
                    grad *= (changed * (1 - changed))[:, None]
                    original_grad = bx[known] * (bp[known] * (1 - bp[known]))[:, None]
                    key = name
                    stat = stats["numeric_contrasts"].setdefault(
                        key, [0, 0.0, np.zeros(self.dimension)]
                    )
                    stat[0] += int(known.sum())
                    stat[1] += float((changed - bp[known]).sum())
                    stat[2] += (grad - original_grad).sum(axis=0)

    def new_stats(self):
        return dict(
            n=0,
            returned=0,
            brier=0.0,
            baseline_brier=0.0,
            log_loss=0.0,
            auc_positive=np.zeros(1024, dtype=np.int64),
            auc_negative=np.zeros(1024, dtype=np.int64),
            importance={name: [0.0, 0.0] for name in self.groups},
            contrasts={},
            numeric_contrasts={},
        )

    def allocate_test(self, count):
        self.test_x = np.empty((count, self.dimension), dtype=float)
        self.test_year = np.empty(count, dtype=np.int16)
        self.test_scope = np.empty((count, 2), dtype=bool)
        self.test_complete = np.empty((count, 3), dtype=bool)
        self.test_y = np.empty((count, 3), dtype=bool)
        self.test_position = 0

    def collect_test(self, rows, x=None, complete_only=False):
        chosen = [
            r
            for r in rows
            if heldout(r)
            and (
                not complete_only
                or any(r["outcomes"][str(h)]["followup_complete"] for h in HORIZONS)
            )
        ]
        if not chosen:
            return
        block = self.design(chosen) if x is None else x[[heldout(r) for r in rows]]
        start = self.test_position
        end = start + len(chosen)
        if end > len(self.test_year):
            raise ValueError("Held-out population changed during the study.")
        self.test_x[start:end] = block
        self.test_year[start:end] = [r["cohort"] for r in chosen]
        self.test_scope[start:end] = [
            (
                r["outcomes"].get("initial_pvp") is True,
                r["features"].get("opponent") == "NPC/object",
            )
            for r in chosen
        ]
        self.test_complete[start:end] = [
            [r["outcomes"][str(h)]["followup_complete"] for h in HORIZONS]
            for r in chosen
        ]
        self.test_y[start:end] = [
            [r["outcomes"][str(h)]["returned"] is True for h in HORIZONS]
            for r in chosen
        ]
        self.test_position = end

    def global_importance(self, progress=lambda *a, **k: None, check_stop=lambda: None):
        if self.test_position != len(self.test_year):
            raise ValueError("Not every held-out character was collected.")
        for number, ((scope, horizon), model) in enumerate(self.models.items(), 1):
            check_stop()
            if model.get("unavailable"):
                continue
            valid = self.test_complete[:, HORIZONS.index(horizon)].copy()
            if scope != "all":
                valid &= self.test_scope[:, 0 if scope == "pvp" else 1]
            for year in np.unique(self.test_year[valid]):
                check_stop()
                used = valid & (self.test_year == year)
                bx = self.test_x[used]
                by = self.test_y[used, HORIZONS.index(horizon)].astype(float)
                beta = model["beta"]
                linear = bx @ beta
                error = (sigmoid(linear) - by) ** 2
                stats = model["evaluation"].get(str(int(year)))
                if not stats or stats["n"] != len(by):
                    raise ValueError(
                        "Held-out evaluation and permutation populations differ."
                    )
                for name, columns in self.groups.items():
                    check_stop()
                    part = bx[:, columns] @ beta[columns]
                    rng = np.random.default_rng(SEED + horizon + int(year) + columns[0])
                    differences = np.zeros(len(by))
                    for repeat in range(3):
                        differences += (
                            sigmoid(linear - part + part[rng.permutation(len(by))]) - by
                        ) ** 2 - error
                    differences /= 3
                    stats["importance"][name] = [
                        float(differences.sum()),
                        float((differences**2).sum()),
                    ]
            progress(
                "return_importance",
                processed_models=number,
                total_models=len(self.models),
                players=int(valid.sum()),
                model_scope=scope,
                horizon=horizon,
                message="Comparing every held-out character within the complete loss-year population.",
            )

    def summarize(self, progress=lambda *a, **k: None, check_stop=lambda: None):
        self.global_importance(progress, check_stop)
        result = dict(
            method="Year-adjusted multivariable ridge logistic regression",
            ridge=RIDGE,
            iterations=self.iterations,
            converged=self.converged,
            seed=SEED,
            split="Deterministic character-level 80% training / 20% held-out evaluation",
            importance_method="Grouped permutation across every held-out character of the same loss year; 3 repeats; increase in held-out Brier score",
            scopes={},
            references=REFERENCES,
            interpretation="Adjusted associations and predictive importance, not shares of a player's causal decision.",
        )
        archived = dict(
            outcomes_used_for_training=True,
            feature_names=self.design_names,
            scaling={
                str(y): dict(mean=m.tolist(), std=s.tolist())
                for y, (m, s) in self.scales.items()
            },
            models={},
        )
        for (scope, horizon), model in self.models.items():
            output = result["scopes"].setdefault(scope, {})
            if model.get("unavailable"):
                output[str(horizon)] = dict(unavailable=model["unavailable"])
                continue
            penalty = np.ones(self.dimension) * RIDGE
            penalty[len(self.names)] = 0
            bread = np.linalg.inv(
                model["fisher"] + np.diag(penalty * model["training_n"])
            )
            covariance = bread @ model["sandwich"] @ bread
            # A count constant within year cannot have an estimable numerical
            # effect. Ridge may otherwise assign it part of the year effect.
            model["numeric_supported"] = {}
            for name in CONTINUOUS:
                column, missing = self.groups[name]
                controls = [missing, *range(len(self.names), self.dimension)]
                control_matrix = model["fisher"][np.ix_(controls, controls)]
                cross = model["fisher"][column, controls]
                residual = (
                    model["fisher"][column, column]
                    - cross @ np.linalg.pinv(control_matrix) @ cross
                )
                model["numeric_supported"][name] = bool(
                    residual > 1e-9 * max(1, model["fisher"][column, column])
                )
            pooled = self.new_stats()
            for stats in model["evaluation"].values():
                for key in (
                    "n",
                    "returned",
                    "brier",
                    "baseline_brier",
                    "log_loss",
                    "auc_positive",
                    "auc_negative",
                ):
                    pooled[key] += stats[key]
                for key, values in stats["importance"].items():
                    pooled["importance"][key] = [
                        a + b for a, b in zip(pooled["importance"][key], values)
                    ]
                for key, values in stats["contrasts"].items():
                    aggregate = pooled["contrasts"].setdefault(
                        key, [0, 0.0, np.zeros(self.dimension)]
                    )
                    aggregate[0] += values[0]
                    aggregate[1] += values[1]
                    aggregate[2] += values[2]
                for key, values in stats["numeric_contrasts"].items():
                    aggregate = pooled["numeric_contrasts"].setdefault(
                        key, [0, 0.0, np.zeros(self.dimension)]
                    )
                    aggregate[0] += values[0]
                    aggregate[1] += values[1]
                    aggregate[2] += values[2]
            output[str(horizon)] = self.finish(pooled, model, covariance)
            output[str(horizon)]["years"] = {
                year: self.finish(stats, model, covariance)
                for year, stats in model["evaluation"].items()
            }
            archived["models"][scope + ":" + str(horizon)] = dict(
                coefficients=model["beta"].tolist(),
                robust_covariance=covariance.tolist(),
                training_n=model["training_n"],
            )
        return archived, result

    def finish(self, stats, model, covariance):
        n = stats["n"]
        if n < 20:
            return dict(
                unavailable="Fewer than 20 complete held-out characters.", test_n=n
            )
        brier, baseline = stats["brier"] / n, stats["baseline_brier"] / n
        importance = importance_rows(
            stats["importance"], n, brier < baseline and self.converged
        )
        effects = []
        for encoded, (supported, difference, gradient) in stats["contrasts"].items():
            name, level = encoded.split("=", 1)
            reference = REFERENCES[name]
            counts = model["counts"][name]
            if counts[level][1] < 20 or counts[reference][1] < 20:
                continue
            g = gradient / supported
            se = float(np.sqrt(max(0, g @ covariance @ g)))
            effect = 100 * difference / supported
            effects.append(
                dict(
                    factor=name,
                    profile=level,
                    reference=reference,
                    effect_pp=effect,
                    interval_pp=[effect - 196 * se, effect + 196 * se],
                    evaluated_n=supported,
                    profile_train_n=counts[level][1],
                    reference_train_n=counts[reference][1],
                )
            )
        for name, (known, difference, gradient) in stats["numeric_contrasts"].items():
            if known < 20 or not model["numeric_supported"].get(name):
                continue
            g = gradient / known
            se = float(np.sqrt(max(0, g @ covariance @ g)))
            effect = 100 * difference / known
            effects.append(
                dict(
                    factor=name,
                    profile="+1 within-year training standard deviation (transformed value)",
                    reference="Recorded initial value",
                    effect_pp=effect,
                    interval_pp=[effect - 196 * se, effect + 196 * se],
                    evaluated_n=known,
                )
            )
        positive, negative = stats["auc_positive"], stats["auc_negative"]
        pairs = int(positive.sum()) * int(negative.sum())
        auc = (
            float(
                (positive * (np.cumsum(negative) - negative + 0.5 * negative)).sum()
                / pairs
            )
            if pairs
            else None
        )
        warnings = []
        if not self.converged:
            warnings.append(
                "IRLS did not converge; effects are provisional and normalized importance is withheld."
            )
        if brier >= baseline:
            warnings.append(
                "The model does not outperform the loss-year baseline on held-out characters; normalized importance is withheld."
            )
        return dict(
            training_n=model["training_n"],
            test_n=n,
            returned=stats["returned"],
            brier=brier,
            baseline_brier=baseline,
            brier_skill=1 - brier / baseline if baseline else None,
            log_loss=stats["log_loss"] / n,
            auc=auc,
            auc_probability_bins=1024,
            warnings=warnings,
            effects=sorted(effects, key=lambda v: abs(v["effect_pp"]), reverse=True),
            importance=sorted(importance, key=lambda v: v["delta_brier"], reverse=True),
        )


def refresh_global_importance(
    conn,
    run,
    archive,
    summary,
    batch_size=5000,
    progress=lambda *a, **k: None,
    check_stop=lambda: None,
):
    """Reassess saved coefficients without refitting or writing database rows."""
    from .database import stream

    study = ReturnStudy()
    study.scales = {
        int(y): (np.asarray(v["mean"]), np.asarray(v["std"]))
        for y, v in archive["scaling"].items()
    }
    study.years = sorted(study.scales)
    study.design_names = (
        study.names + ["intercept"] + ["year=" + str(y) for y in study.years[1:]]
    )
    if study.design_names != archive["feature_names"]:
        raise ValueError("Stored predictor design does not match this evaluator.")
    study.dimension = len(study.design_names)
    bound = summary.get("scopes", {}).get("all", {}).get("7", {}).get("test_n")
    if bound is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM research.first_loss_players WHERE run_id=%s",
                (run,),
            )
            bound = cur.fetchone()[0]
        conn.rollback()
    study.allocate_test(bound)
    seen = 0
    for rows in stream(conn, run, batch_size):
        check_stop()
        study.collect_test(rows, complete_only=True)
        seen += len(rows)
        progress(
            "collecting_return_importance",
            players=seen,
            heldout=study.test_position,
            message="Collecting every complete held-out profile for whole-year permutations.",
        )
    for name in ("test_x", "test_year", "test_scope", "test_complete", "test_y"):
        setattr(study, name, getattr(study, name)[: study.test_position])
    for key, saved in archive["models"].items():
        scope, horizon = key.split(":")
        report = summary["scopes"][scope][horizon]
        if report.get("unavailable"):
            continue
        study.models[(scope, int(horizon))] = dict(
            beta=np.asarray(saved["coefficients"]),
            evaluation={
                y: {**study.new_stats(), "n": v["test_n"]}
                for y, v in report.get("years", {}).items()
                if v.get("test_n")
            },
        )
    study.global_importance(progress, check_stop)
    for (scope, horizon), model in study.models.items():
        report = summary["scopes"][scope][str(horizon)]
        moments = {name: [0.0, 0.0] for name in study.groups}
        for year, stats in model["evaluation"].items():
            for name, (total, squares) in stats["importance"].items():
                moments[name][0] += total
                moments[name][1] += squares
            yearly = report["years"][year]
            if not yearly.get("unavailable"):
                yearly["importance"] = importance_rows(
                    stats["importance"],
                    yearly["test_n"],
                    summary["converged"] and yearly["brier"] < yearly["baseline_brier"],
                )
        report["importance"] = importance_rows(
            moments,
            report["test_n"],
            summary["converged"] and report["brier"] < report["baseline_brier"],
        )
    summary["importance_method"] = (
        "Grouped permutation across every held-out character of the same loss year; 3 repeats; increase in held-out Brier score"
    )
