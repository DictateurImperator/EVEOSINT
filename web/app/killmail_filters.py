"""Shared request parsing and lookup for the killboard filter builder."""
from .entities import get_killmail_ship_options, search_entities, search_killmail_locations


def killmail_filters_from_request(request):
    query = request.query_params
    return {
        "participation": query.get("participation", "both"),
        "date_from": query.get("date_from"),
        "date_to": query.get("date_to"),
        "datetime_from": query.get("datetime_from"),
        "datetime_to": query.get("datetime_to"),
        "affiliation_corporation_ids": query.getlist("affiliation_corporation_ids"),
        "affiliation_alliance_ids": query.getlist("affiliation_alliance_ids"),
        "involved_corporation_ids": query.getlist("involved_corporation_ids"),
        "involved_alliance_ids": query.getlist("involved_alliance_ids"),
        "involved_role": query.get("involved_role", "both"),
        "type_ids": query.getlist("type_ids"),
        "type_role": query.get("type_role", "both"),
        "module_type_ids": query.getlist("module_type_ids"),
        "builder_ship_include": query.getlist("builder_ship_include"),
        "builder_ship_exclude": query.getlist("builder_ship_exclude"),
        "builder_entity_include": query.getlist("builder_entity_include"),
        "builder_entity_exclude": query.getlist("builder_entity_exclude"),
        "builder_zone_include": query.getlist("builder_zone_include"),
        "builder_zone_exclude": query.getlist("builder_zone_exclude"),
        "heat_ship_include": query.getlist("heat_ship_include"),
        "heat_ship_exclude": query.getlist("heat_ship_exclude"),
        "heat_entity_include": query.getlist("heat_entity_include"),
        "heat_entity_exclude": query.getlist("heat_entity_exclude"),
        "scan_before": query.get("scan_before"),
        "scan_month": query.get("scan_month"),
        "scan_row": query.get("scan_row"),
    }


def search_killboard_filters(kind, query, limit=12, mer_only=False):
    query = str(query or "").strip()
    limit = max(1, min(int(limit or 12), 30))
    if len(query) < 2:
        return []

    if kind == "entity":
        allowed = {"corporation", "alliance"} if mer_only else {"character", "corporation", "alliance"}
        results = search_entities(query, limit=min(limit, 10), include_characters=not mer_only)
        return [item for item in results if item.get("entity_type") in allowed][:limit]
    if kind == "zone":
        return search_killmail_locations(query, limit=limit)

    if kind == "ship":
        needle = query.lower()
        items = []
        options = get_killmail_ship_options()
        groups = sorted({item.get("group_name") for item in options if item.get("group_name")})
        for group in groups:
            if needle in group.lower():
                items.append({"entity_type": "ship_group", "entity_id": group,
                              "name": group, "label": group + " · all types", "subtitle": "Ship / structure group"})
        for item in options:
            haystack = " ".join([
                str(item.get("entity_id") or ""),
                str(item.get("name") or ""),
                str(item.get("group_name") or ""),
                str(item.get("ship_display_size") or item.get("ship_size") or ""),
                str(item.get("selection_faction") or item.get("faction_name") or ""),
                str(item.get("category_name") or ""),
            ]).lower()
            if needle not in haystack:
                continue
            items.append({
                "entity_type": "ship",
                "entity_id": int(item["entity_id"]),
                "name": item.get("name") or f"Type {item['entity_id']}",
                "label": item.get("name") or f"Type {item['entity_id']}",
                "subtitle": item.get("group_name") or ("Structure" if item.get("is_structure") else "Ship"),
                "image_url": item.get("image_url"),
                "is_structure": bool(item.get("is_structure")),
            })
            if len(items) >= limit:
                break
        return items[:limit]

    return []
