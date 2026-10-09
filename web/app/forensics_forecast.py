"""Explainable search estimates. Scores and percentiles are not calibrated odds."""

EVIDENCE_FILTERS = {
    "deduced_id": ("Deduced CCP ID", "context"),
    "id_uncertain": ("Uncertain CCP ID", "context"),
    "same_system": ("Present in the same system", "pilot"),
    "neighbor_system": ("Seen in a neighboring system", "pilot"),
    "same_ship_local": ("Same ship nearby", "pilot"),
    "unique_ship_local": ("Only observed user of this ship", "pilot"),
    "same_ship_before_loss": ("Same ship attacking before the loss", "pilot"),
    "recent_ship": ("Flew this ship in the preceding 30 days", "pilot"),
    "past_ship": ("Historical use of this ship", "pilot"),
    "historical_pvp": ("Prior PvP activity", "pilot"),
    "companions": ("Known combat companions", "pilot"),
    "disappeared": ("Disappears while the fight continues", "pilot"),
    "capsule_sequence": ("Ship / capsule sequence", "pilot"),
    "membership": ("Membership recorded at kill time", "pilot"),
    "inferred_skills": ("Current inferred ship capability", "pilot"),
    "final_blow_local": ("Nearby final blows in this ship", "pilot"),
    "ownership_local": ("Nearby ownership evidence", "pilot"),
    "recovered_neighbor": ("Evidence from a recovered neighbor", "pilot"),
    "npc_final_blow": ("NPC final-blow ship type", "context"),
    "object_victim": ("Structure or deployable victim", "context"),
    "incomplete_evidence": ("Incomplete evidence", "context"),
}


def signal(candidate):
    evidence = candidate.get("evidence", {})
    if evidence.get("manual"):
        return 0
    if candidate["id"] is None:
        return 3 if evidence.get("npc_type") else 0
    if evidence.get("capsule_sequence") or evidence.get("unique_ship_local"):
        return 3
    if (
        evidence.get("same_ship_local")
        or evidence.get("physical_local")
        or evidence.get("ownership_local")
    ):
        return 2
    if evidence.get("prior_ship_days") is not None:
        return 2 if evidence["prior_ship_days"] <= 30 else 1
    return 1 if evidence.get("historical_pvp") else 0


def forecast(hypotheses, choices, remaining, *, recovered=False, analysis_error=None):
    """Rank cost conditional on the answer being in the remaining selected plan.

    A separately assessed evidence level accounts for missing pilots/IDs, weak
    clues and incomplete collection. Never interpret normalized weights as the
    probability of a successful CCP recovery.
    """
    maximum = len(remaining)
    result = {
        "version": 1,
        "maximum_trials": maximum,
        "expected_trials": None,
        "typical_trials": None,
        "upper_trials": None,
        "assessment": "blocked",
        "priority": 0,
        "reasons": [],
        "conditional": True,
        "model": "relative_score_order",
    }
    result["candidate_evidence"] = {
        side: [
            {
                "id": c["id"],
                "flags": [
                    key
                    for key, (_, scope) in EVIDENCE_FILTERS.items()
                    if scope == "pilot" and c.get("evidence", {}).get(key)
                ],
            }
            for c in hypotheses[side]["candidates"]
            if c["id"] in choices.get(key, [])
        ]
        for side, key in (("victim", "victims"), ("attacker", "attackers"))
    }
    from .forensics_engine import OBJECT_CATEGORIES

    result["context_flags"] = [
        "deduced_id" if hypotheses["ids"].get("deduced") else "id_uncertain"
    ]
    if hypotheses["attacker"].get("category") == 11:
        result["context_flags"].append("npc_final_blow")
    if hypotheses["victim"].get("category") in OBJECT_CATEGORIES:
        result["context_flags"].append("object_victim")
    if hypotheses.get("warnings") or any(
        hypotheses[side].get("truncated") for side in ("ids", "victim", "attacker")
    ):
        result["context_flags"].append("incomplete_evidence")
    reasons = result["reasons"]
    if recovered:
        result.update(
            assessment="confirmed", expected_trials=0, typical_trials=0, upper_trials=0
        )
        reasons.append("CCP has confirmed this killmail.")
        return result
    if analysis_error:
        reasons.append(analysis_error)
        return result
    if not choices.get("ids"):
        reasons.append("No CCP killmail ID is selected; an ID hypothesis is required.")
        return result
    if not maximum:
        result["assessment"] = (
            "exhausted"
            if all(choices.get(k) for k in ("victims", "attackers"))
            else "blocked"
        )
        reasons.append(
            "The selected combinations are exhausted; widen the candidate lists."
            if result["assessment"] == "exhausted"
            else "Select a victim and final-blow hypothesis."
        )
        return result
    # Unrounded positive weights avoid an artificial zero for large candidate lists.
    lookup = {
        key: {
            c["id"]: max(float(c.get("weight", 1)), 0.000001)
            for c in hypotheses[side]["candidates"]
        }
        for key, side in (
            ("ids", "ids"),
            ("victims", "victim"),
            ("attackers", "attacker"),
        )
    }
    weights = [
        lookup["ids"].get(i, 1)
        * lookup["victims"].get(v, 1)
        * lookup["attackers"].get(a, 1)
        for i, v, a, _ in remaining
    ]
    total = sum(weights)
    result["expected_trials"] = round(
        sum(rank * w for rank, w in enumerate(weights, 1)) / total, 2
    )
    cumulative = 0
    for rank, weight in enumerate(weights, 1):
        cumulative += weight
        if result["typical_trials"] is None and cumulative >= total * 0.5:
            result["typical_trials"] = rank
        if cumulative >= total * 0.8:
            result["upper_trials"] = rank
            break
    strengths = []
    for side, key, label in (
        ("victim", "victims", "Victim"),
        ("attacker", "attackers", "Final blow"),
    ):
        info = hypotheses[side]
        viable = {item[1 if side == "victim" else 2] for item in remaining}
        selected = [c for c in info["candidates"] if c["id"] in viable]
        best = selected[0] if selected else None
        strength = signal(best) if best else 0
        if info["candidates"] and best and best["id"] != info["candidates"][0]["id"]:
            strength = min(strength, 1)
            reasons.append(
                f"{label}: the highest-ranked automatic candidate is excluded."
            )
        strengths.append(strength)
        descriptions = (
            "weak clues or a manual/absent-character guess",
            "past PvP activity",
            "local combat, ownership or recent use of this ship",
            "a ship/capsule sequence or a unique local ship user",
        )
        description = (
            "NPC ship type supports the absent-character alternative"
            if best and best["id"] is None and strength == 3
            else descriptions[strength]
        )
        reasons.append(f"{label}: {description}.")
        selected_weight = sum(c.get("weight", 1) for c in selected)
        all_weight = sum(c.get("weight", 1) for c in info["candidates"])
        if all_weight and selected_weight / all_weight < 0.25:
            strengths[-1] = min(strengths[-1], 1)
            reasons.append(
                f"{label}: most candidate weight is outside the selected plan; widening may be needed."
            )
    priority = min(strengths)
    if not hypotheses["ids"].get("deduced"):
        priority = min(priority, 2)
        reasons.append(
            "The CCP ID remains a hypothesis among multiple candidates or incomplete anchors."
        )
    if hypotheses.get("warnings") or any(
        hypotheses[side].get("truncated") for side in ("ids", "victim", "attacker")
    ):
        priority = min(priority, 1)
        reasons.append("Evidence or candidate coverage is incomplete.")
    priority = max(1, priority)
    result.update(
        priority=priority, assessment={3: "strong", 2: "moderate", 1: "weak"}[priority]
    )
    reasons.append(
        f"Selected plan: {len(choices['ids'])} IDs × {len(choices['victims'])} victims × {len(choices['attackers'])} final-blow hypotheses; {maximum} combinations remain."
    )
    reasons.append(
        "Trial range uses the 50th–80th percentiles of relative scores, conditional on the correct answer being selected. Evidence levels are heuristic, not success probabilities."
    )
    return result
