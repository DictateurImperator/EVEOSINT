"""Predictors describe t0 only; outcomes never enter clustering."""

from datetime import date, timedelta
import math

VERSION = 1
NUMERIC = (
    "age_days",
    "corp_tenure_days",
    "corp_members",
    "alliance_members",
    "prior_attacks",
    "prior_attack_days",
    "players_attacking",
    "npc_attackers",
    "fitted_items",
    "cargo_items",
    "implants",
    "corp_attacks_30",
    "corp_industry_loss_fraction",
    "alliance_attacks_30",
    "alliance_industry_loss_fraction",
)
CATEGORIES = {
    "space": ("HS", "LS", "NS", "WH", "Pochven", "unknown"),
    "ship_role": (
        "mining",
        "transport",
        "exploration",
        "combat",
        "logistics",
        "industrial_support",
        "pod",
        "other",
        "unknown",
    ),
    "opponent": ("players", "NPC/object", "mixed", "unknown"),
    "corp_kind": ("player", "NPC", "unknown"),
}
FEATURE_NAMES = [n for key in NUMERIC for n in (key, key + "_missing")] + [
    key + "=" + v for key, vals in CATEGORIES.items() for v in vals
]


def space(system, at=None):
    region = system.get("regionID")
    if region is None:
        return "unknown"
    region = int(region)
    # Current SDE cannot reconstruct the former empire status. Also exclude the
    # transition day itself, whose change time is not available in this dataset.
    if region == 10000070:
        return (
            "unknown"
            if at is not None and at.date() < date(2020, 10, 14)
            else "Pochven"
        )
    if 11000001 <= region <= 11000033:
        return "WH"
    sec = system.get("securityStatus")
    if sec is None:
        return "unknown"
    return "HS" if float(sec) >= 0.45 else "LS" if float(sec) > 0 else "NS"


def ship_role(group):
    g = (group or "").lower()
    if not g:
        return "unknown"
    if g == "capsule":
        return "pod"
    if g in {"mining barge", "exhumer", "mining frigate", "expedition frigate"}:
        return "mining"
    if g in {
        "industrial",
        "hauler",
        "freighter",
        "jump freighter",
        "blockade runner",
        "deep space transport",
        "transport ship",
    }:
        return "transport"
    if g in {"industrial command ship", "capital industrial ship"}:
        return "industrial_support"
    if "logistics" in g or g == "force auxiliary":
        return "logistics"
    # Covert ops are not always explorers; covert combat/recon remains unclassified by intent.
    if g == "covert ops":
        return "exploration"
    if g in {
        "frigate",
        "destroyer",
        "cruiser",
        "battlecruiser",
        "combat battlecruiser",
        "attack battlecruiser",
        "battleship",
        "assault frigate",
        "heavy assault cruiser",
        "interceptor",
        "interdictor",
        "heavy interdictor",
        "strategic cruiser",
        "tactical destroyer",
        "dreadnought",
        "carrier",
        "supercarrier",
        "titan",
        "stealth bomber",
        "recon ship",
        "force recon ship",
        "combat recon ship",
        "command ship",
        "black ops",
        "marauder",
        "corvette",
    }:
        return "combat"
    return "other"


def outcome(
    loss,
    first_return,
    first_pvp,
    first_attack,
    next_loss,
    cutoff,
    missing_days,
    return_role=None,
    initial_ship=None,
    next_ship=None,
    initial_system=None,
    next_system=None,
    losses_30=0,
    attacks_30=0,
    losses_90=0,
    attacks_90=0,
    initial_pvp=None,
):
    result = {}
    for horizon in (7, 30, 90):
        end = loss + timedelta(days=horizon)
        complete = end <= cutoff and not any(
            loss.date() <= d <= end.date() for d in missing_days
        )

        def label(event):
            return (
                True
                if event is not None and event <= end
                else False if complete else None
            )

        any_return = label(first_return)
        pvp = label(first_pvp)
        result[str(horizon)] = {
            "returned": any_return,
            "pvp_returned": pvp,
            "followup_complete": complete,
            "trajectory": (
                "returned_pvp"
                if pvp is True
                else (
                    "returned_without_observed_pvp"
                    if any_return is True and pvp is False
                    else (
                        "returned_pvp_followup_incomplete"
                        if any_return is True
                        else (
                            "no_observed_return"
                            if any_return is False
                            else "insufficient_followup"
                        )
                    )
                )
            ),
        }
    result.update(
        {
            "return_days": (
                (first_return - loss).total_seconds() / 86400 if first_return else None
            ),
            "first_pvp_days": (
                (first_pvp - loss).total_seconds() / 86400 if first_pvp else None
            ),
            "first_attack_days": (
                (first_attack - loss).total_seconds() / 86400 if first_attack else None
            ),
            "next_loss_days": (
                (next_loss - loss).total_seconds() / 86400 if next_loss else None
            ),
            "return_role": return_role,
            "same_ship": (
                None
                if initial_ship is None or next_ship is None
                else initial_ship == next_ship
            ),
            "same_system": (
                None
                if initial_system is None or next_system is None
                else initial_system == next_system
            ),
            "losses_30": losses_30,
            "attacks_30": attacks_30,
            "losses_90": losses_90,
            "attacks_90": attacks_90,
            "initial_pvp": initial_pvp,
        }
    )
    return result


def encode(features):
    values = []
    for key in NUMERIC:
        value = features.get(key)
        if value is None or not math.isfinite(float(value)):
            values.extend((0.0, 1.0))
        else:
            x = max(0.0, float(value))
            values.extend((x if key.endswith("_fraction") else math.log1p(x), 0.0))
    for key, cats in CATEGORIES.items():
        value = features.get(key, "unknown")
        value = value if value in cats else "unknown"
        # One categorical field has total unit weight regardless of its number of categories.
        values.extend(float(value == cat) for cat in cats)
    return values


def wilson(yes, n):
    if not n:
        return None
    z = 1.959963984540054
    p = yes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, centre - half), min(1.0, centre + half)]
