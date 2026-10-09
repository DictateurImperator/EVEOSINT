"""Deterministic hash, evidence ranking and validation; no HTTP or database side effects."""

import hashlib
import itertools
from collections import defaultdict
from datetime import datetime, timezone

VERSION = 3
MAX_IDS = 200
MAX_PILOTS = 200
CAPSULE_TYPES = frozenset({670, 33328})
OBJECT_CATEGORIES = frozenset({3, 22, 23, 40, 46, 65, 66})


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("A UTC offset is required.")
    return value.astimezone(timezone.utc)


def character(value):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(
            "Use a positive character ID, or None for an absent character."
        )
    return int(value)


def killmail_hash(victim, attacker, ship, timestamp):
    timestamp = utc(timestamp)
    if timestamp.microsecond:
        raise ValueError("The kill time must be known to whole seconds.")
    delta = timestamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    ticks = (delta.days * 86400 + delta.seconds) * 10000000 + 116444736000000000
    fields = (
        "None" if victim is None else str(character(victim)),
        "None" if attacker is None else str(character(attacker)),
        str(int(ship)),
        str(ticks),
    )
    return hashlib.sha1("".join(fields).encode("ascii")).hexdigest()


def ranked_candidates(candidates):
    """Percentages describe relative weights within this list, not calibrated certainty."""
    candidates = sorted(candidates, key=lambda c: (-c["weight"], c["id"] or 0))
    total = sum(c["weight"] for c in candidates)
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
        candidate["score"] = round(100 * candidate["weight"] / total, 2) if total else 0
    return candidates


def infer_ids(before, after, prior_count, same_second_count, between_count, known_ids):
    reasons = []
    if not before or not after:
        return {
            "candidates": [],
            "total": 0,
            "truncated": False,
            "reasons": [
                "No pair of isolated known anchors was found. Enter candidate IDs manually."
            ],
        }
    low, high = before["id"] + 1, after["id"] - 1
    contiguous = after["id"] - before["id"] == between_count + 1
    if contiguous:
        low = before["id"] + prior_count + 1
        high = low + same_second_count - 1
        reasons.append("Known anchors agree with the number of MER rows between them.")
    else:
        reasons.append(
            "The MER export has gaps or inconsistent anchors; all IDs between the anchors remain possible."
        )
    reasons.append(
        f"Anchors: {before['id']} at {before['time']}; {after['id']} at {after['time']}."
    )
    unavailable = {int(value) for value in known_ids if low <= int(value) <= high}
    total = max(0, high - low + 1 - len(unavailable))
    ids = itertools.islice(
        (i for i in range(low, high + 1) if i not in unavailable), MAX_IDS
    )
    candidates = [
        {"id": i, "name": str(i), "weight": 1, "reasons": list(reasons)} for i in ids
    ]
    return {
        "candidates": ranked_candidates(candidates),
        "total": total,
        "truncated": total > MAX_IDS,
        "reasons": reasons,
        "deduced": contiguous and total == 1,
        "lower": low,
        "upper": high,
    }


def pilot_candidates(
    snapshot,
    role,
    members,
    observations,
    capable,
    companions,
    category=None,
    truncated=False,
    prior_usage=None,
    npc_loss=False,
    past_pvp=None,
    names=None,
):
    at = utc(snapshot["kill_datetime"])
    corp = snapshot.get(
        "victim_corporation_id" if role == "victim" else "killer_corporation_id"
    )
    ship = snapshot.get(
        "victim_ship_type_id" if role == "victim" else "killer_ship_type_id"
    )
    system = snapshot.get("solar_system_id")
    grouped = defaultdict(list)
    for event in observations:
        # Mask the target even during offline evaluation on a known kill.
        if event["time"] == at and event.get("target"):
            continue
        if event.get("character_id") and event.get("corporation_id") == corp:
            grouped[int(event["character_id"])].append(event)
    past_pvp = past_pvp or {}
    candidate_ids = set(members) | set(grouped) | set(past_pvp)
    candidates = []
    active_here = {
        cid
        for cid, events in grouped.items()
        if any(e["system_id"] == system for e in events)
    }
    ship_users = {
        cid
        for cid, events in grouped.items()
        if any(
            e["system_id"] == system
            and ship
            and e.get("ship_type_id") == ship
            and abs((e["time"] - at).total_seconds()) <= 1800
            for e in events
        )
    }
    fight_continues = (
        len(
            {
                e["killmail_id"]
                for e in observations
                if e["system_id"] == system
                and 300 < (e["time"] - at).total_seconds() <= 1800
                and e.get("role") == "attacker"
            }
        )
        >= 2
    )
    shared_counts = defaultdict(int)
    for pair, count in companions.items():
        for cid in pair:
            if any(other in active_here for other in pair if other != cid):
                shared_counts[cid] += count
    capsule = (
        snapshot.get(
            "victim_ship_group_id" if role == "victim" else "killer_ship_group_id"
        )
        == 29
        or ship in CAPSULE_TYPES
    )
    for cid in sorted(candidate_ids):
        events = grouped[cid]
        weight = 1.0
        reasons = []
        pvp_priority = cid in past_pvp or any(
            e.get("role") == "attacker" for e in events
        )
        if cid in past_pvp:
            age = max(0, (at - past_pvp[cid]).total_seconds() / 86400)
            reasons.append(
                f"Known combatant of this corporation: observed attacking {age:.1f} days before this kill."
            )
            weight += 3 / (1 + age / 30)
            if cid not in members:
                reasons.append(
                    "Historical combat affiliation is an indication; membership at this exact time is not recorded."
                )
        if cid in members:
            reasons.append("Corporation membership at the kill time is recorded.")
            weight += 1
        if events:
            reasons.append(
                f"{len(events)} known killmail appearances with this corporation in the time window."
            )
            weight += min(3, len(events) * 0.25)
        local = [e for e in events if e["system_id"] == system]
        nearby = [e for e in events if e["system_id"] != system]
        if local:
            reasons.append(
                "Associated with known killmails in this system around the fight; owned objects alone do not prove physical presence."
            )
            nearest = min(abs((e["time"] - at).total_seconds()) / 60 for e in local)
            weight += 4 / (1 + nearest / 15)
        if nearby:
            reasons.append("Observed in a neighboring system; travel remains possible.")
            weight += 1
        matching = [
            e
            for e in local
            if ship
            and e.get("ship_type_id") == ship
            and abs((e["time"] - at).total_seconds()) <= 1800
        ]
        if matching:
            reasons.append(
                "Associated with the relevant object within 30 minutes in this system."
                if category in OBJECT_CATEGORIES
                else "Observed using the relevant ship within 30 minutes in this system."
            )
            nearest = min(abs((e["time"] - at).total_seconds()) / 60 for e in matching)
            weight += 6 / (1 + nearest / 10)
            if len(ship_users) == 1:
                reasons.append(
                    "Only one observed pilot of this corporation uses this ship in the nearby fight; unobserved pilots remain possible."
                )
                weight += 8
        used = (prior_usage or {}).get((cid, ship))
        if used:
            age = max(0, (at - used).total_seconds() / 86400)
            reasons.append(
                f"Previously appeared as owner/operator of this object {age:.1f} days before this kill."
                if category in OBJECT_CATEGORIES
                else f"Previously flew this ship {age:.1f} days before this kill, on a known killmail."
            )
            weight += 5 / (1 + age / 30)
        if role == "victim" and any(
            e["time"] < at and e.get("role") == "attacker" for e in matching
        ):
            reasons.append(
                "Attacked in this same ship and corporation shortly before the hidden loss in this system; this rule applies to every ship type."
            )
            weight += 4
            if npc_loss:
                reasons.append(
                    "The subsequent hidden loss has an NPC final-blow ship type, consistent with a loss after combat."
                )
                weight += 2
        if cid in capable:
            reasons.append(
                "Inferred capability includes this ship; this is not proof of capability at that historical date."
            )
            weight += 1
        before = [e for e in local if e["time"] < at and e.get("role") == "attacker"]
        after = [e for e in local if e["time"] > at and e.get("role") == "attacker"]
        losses = [
            e
            for e in local
            if e.get("role") == "victim"
            and ship
            and e.get("ship_type_id") == ship
            and abs((e["time"] - at).total_seconds()) <= 1800
        ]
        if (
            role == "victim"
            and before
            and min((at - e["time"]).total_seconds() for e in before) <= 1200
            and not after
            and fight_continues
        ):
            reasons.append(
                "Disappears from observed kills while the fight continues; delayed killmail attribution remains possible."
            )
            weight += 4
        if losses and category not in OBJECT_CATEGORIES:
            reasons.append(
                "A nearby loss of this ship is already known; reshipping remains possible."
            )
            weight *= 0.5
        elif losses:
            reasons.append(
                "Nearby losses of the same owned object support ownership; losing a deployable or structure does not imply the owner died."
            )
            weight += min(6, len(losses) * 1.5)
        earlier_ship_losses = [
            e
            for e in local
            if e.get("role") == "victim"
            and e.get("ship_type_id") not in CAPSULE_TYPES
            and e.get("ship_type_id")
            and e.get("ship_category_id", 6) == 6
            and 0 < (at - e["time"]).total_seconds() <= 600
        ]
        later_capsule_losses = [
            e
            for e in local
            if e.get("role") == "victim"
            and (e.get("ship_group_id") == 29 or e.get("ship_type_id") in CAPSULE_TYPES)
            and 0 < (e["time"] - at).total_seconds() <= 600
        ]
        if capsule and earlier_ship_losses:
            seconds = min((at - e["time"]).total_seconds() for e in earlier_ship_losses)
            reasons.append(
                f"Known ship loss {seconds:.0f}s before this capsule-related kill in the same corporation and system; delayed final blows and reshipping remain possible."
            )
            weight += 16 / (1 + seconds / 120)
        if role == "victim" and not capsule and category == 6 and later_capsule_losses:
            seconds = min(
                (e["time"] - at).total_seconds() for e in later_capsule_losses
            )
            reasons.append(
                f"Known capsule loss {seconds:.0f}s after the hidden ship loss in the same corporation and system."
            )
            weight += 16 / (1 + seconds / 120)
        if role == "attacker" and any(e.get("final_blow") for e in matching):
            reasons.append(
                "Delivered final blows with this ship during the nearby fight."
            )
            weight += 3
        shared = shared_counts[cid]
        if shared:
            reasons.append(
                f"{shared} historical co-participations with pilots observed in this fight."
            )
            weight += min(4, shared * 0.2)
        if role == "attacker" and not pvp_priority:
            reasons.append(
                "No recorded attack in the preceding 90 days or nearby fight; left unselected initially, but available to widen the search."
            )
            weight *= 0.2
        candidates.append(
            {
                "id": cid,
                "name": (names or {}).get(cid, members.get(cid, f"Character {cid}")),
                "weight": round(weight, 4),
                "reasons": reasons,
                "pvp_priority": pvp_priority,
                "evidence": {
                    "same_system": any(
                        e.get("role") == "attacker" or e.get("ship_category_id", 6) == 6
                        for e in local
                    ),
                    "neighbor_system": any(
                        e.get("role") == "attacker" or e.get("ship_category_id", 6) == 6
                        for e in nearby
                    ),
                    "recent_ship": bool(used)
                    and (at - used).total_seconds() <= 30 * 86400,
                    "past_ship": bool(used),
                    "companions": shared > 0,
                    "disappeared": role == "victim"
                    and bool(before)
                    and min((at - e["time"]).total_seconds() for e in before) <= 1200
                    and not after
                    and fight_continues,
                    "inferred_skills": cid in capable,
                    "final_blow_local": role == "attacker"
                    and any(e.get("final_blow") for e in matching),
                    "same_ship_before_loss": role == "victim"
                    and any(
                        e["time"] < at and e.get("role") == "attacker" for e in matching
                    ),
                    "recovered_neighbor": any(e.get("from_recovered") for e in events),
                    "membership": cid in members,
                    "historical_pvp": cid in past_pvp,
                    "same_ship_local": bool(matching),
                    "unique_ship_local": bool(matching) and len(ship_users) == 1,
                    "physical_local": any(
                        e.get("role") == "attacker" or e.get("ship_category_id", 6) == 6
                        for e in local
                    ),
                    "prior_ship_days": max(0, (at - used).total_seconds() / 86400)
                    if used
                    else None,
                    "capsule_sequence": bool(
                        (capsule and earlier_ship_losses)
                        or (
                            role == "victim"
                            and not capsule
                            and category == 6
                            and later_capsule_losses
                        )
                    ),
                    "ownership_local": bool(losses) and category in OBJECT_CATEGORIES,
                },
            }
        )
    candidates.sort(key=lambda c: (-c["weight"], c["id"]))
    truncated = truncated or len(candidates) > MAX_PILOTS
    candidates = candidates[:MAX_PILOTS]
    # Structures/deployables may expose ownership, but ownership must not be
    # substituted for a piloting character. NPC corp membership alone proves nothing.
    absent_weight = (
        12 if category == 11 else (8 if category in OBJECT_CATEGORIES else 1)
    )
    candidates.append(
        {
            "id": None,
            "name": "No character (NPC / unpiloted object)",
            "weight": absent_weight,
            "evidence": {
                "npc_type": category == 11,
                "object_type": category in OBJECT_CATEGORIES,
            },
            "reasons": [
                "Explicit absent-character hypothesis; unknown MER character fields do not prove absence.",
                "Structures, deployables, abandoned ships and NPC final blows need this alternative.",
            ],
        }
    )
    return {
        "candidates": ranked_candidates(candidates),
        "category": category,
        "local_pilots": len(active_here),
        "same_ship_pilots": len(ship_users),
        "truncated": truncated,
        "reasons": [
            "Scores are relative likelihoods among listed candidates, not calibrated probabilities."
        ],
    }


def normalize_choices(choices):
    result = {}
    for key in ("ids", "victims", "attackers"):
        values = choices.get(key)
        if not isinstance(values, list) or len(values) > 200:
            raise ValueError(f"{key}: choose at most 200 candidates.")
        normalized = []
        for value in values:
            if key == "ids" and value is None:
                raise ValueError("A killmail ID cannot be absent.")
            parsed = character(value)
            if parsed not in normalized:
                normalized.append(parsed)
        result[key] = normalized
    return result


def combinations(choices, hypotheses):
    scores = {}
    for key, side in (("ids", "ids"), ("victims", "victim"), ("attackers", "attacker")):
        scores[key] = {c["id"]: c["score"] for c in hypotheses[side]["candidates"]}
    count = len(choices["ids"]) * len(choices["victims"]) * len(choices["attackers"])
    if count > 100000:
        raise ValueError(
            "Choose fewer candidates: the investigation is limited to 100,000 combinations per plan."
        )
    values = itertools.product(choices["ids"], choices["victims"], choices["attackers"])

    def weight(item):
        return (
            scores["ids"].get(item[0], 1)
            * scores["victims"].get(item[1], 1)
            * scores["attackers"].get(item[2], 1)
            / 10000
        )

    # The combined score orders trials; correlated evidence means it is not a probability.
    return sorted(
        ((i, v, a, weight((i, v, a))) for i, v, a in values), key=lambda item: -item[3]
    )


def matches_snapshot(payload, snapshot, kill_id, victim, attacker):
    try:
        final = [a for a in payload["attackers"] if a.get("final_blow") is True]
        if len(final) != 1:
            return False
        if int(payload["killmail_id"]) != kill_id or utc(
            payload["killmail_time"]
        ) != utc(snapshot["kill_datetime"]):
            return False
        if int(payload["victim"]["ship_type_id"]) != int(
            snapshot["victim_ship_type_id"]
        ):
            return False
        if (
            character(payload["victim"].get("character_id")) != victim
            or character(final[0].get("character_id")) != attacker
        ):
            return False
        if (
            snapshot.get("solar_system_id")
            and payload.get("solar_system_id") != snapshot["solar_system_id"]
        ):
            return False
        for side, fields in (
            (payload["victim"], ("victim_corporation_id", "victim_alliance_id")),
            (final[0], ("killer_corporation_id", "killer_alliance_id")),
        ):
            for source, target in zip(fields, ("corporation_id", "alliance_id")):
                if (
                    snapshot.get(source) is not None
                    and target in side
                    and side.get(target) != snapshot[source]
                ):
                    return False
        if (
            snapshot.get("killer_ship_type_id") is not None
            and "ship_type_id" in final[0]
            and final[0].get("ship_type_id") != snapshot["killer_ship_type_id"]
        ):
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False
