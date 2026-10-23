"""Profile navigation shared unchanged by private and published pages."""

def entity_profile_menu(profile, requested_tab="killboard"):
    profile_menu = []
    if profile and profile.get("entity_type") == "system":
        base_url = f"/system/{profile['entity_id']}"
        profile_menu = [
            {
                "id": None,
                "menu_key": "killboard",
                "label": "Killboard",
                "href": base_url,
                "icon": "☠",
                "permission_key": "entities.view",
                "active": requested_tab == "killboard",
            },
            {
                "id": None,
                "menu_key": "history",
                "label": "History",
                "href": f"{base_url}?tab=history",
                "icon": "🕘",
                "permission_key": "entities.view",
                "active": requested_tab == "history",
            },
        ]
    elif profile and profile.get("entity_type") in {"character", "corporation", "alliance"}:
        base_url = f"/{profile['entity_type']}/{profile['entity_id']}"
        profile_menu = [
            {
                "id": None,
                "menu_key": "killboard",
                "label": "Killboard",
                "href": base_url,
                "icon": "☠",
                "permission_key": "entities.view",
                "active": requested_tab == "killboard",
            },
        ]
        if profile.get("entity_type") in {"alliance", "corporation"}:
            profile_menu.append({
                "id": None,
                "menu_key": "population",
                "label": "Population",
                "href": f"{base_url}?tab=population",
                "icon": "📈",
                "permission_key": "entities.view",
                "active": requested_tab == "population",
            })
        profile_menu.insert(1, {
            "id": None,
            "menu_key": "stats",
            "label": "Combat stats",
            "href": f"{base_url}?tab=stats",
            "icon": "▥",
            "permission_key": "entities.view",
            "active": requested_tab == "stats",
        })
        if profile.get("entity_type") in {"character", "corporation", "alliance"}:
            profile_menu.append({
                "id": None,
                "menu_key": "history",
                "label": "History",
                "href": f"{base_url}?tab=history",
                "icon": "🕘",
                "permission_key": "entities.view",
                "active": requested_tab == "history",
            })
        profile_menu.append({
            "id": None,
            "menu_key": "pilotable-ships",
            "label": "Pilotable Ships",
            "href": f"{base_url}?tab=pilotable-ships",
            "icon": "🚀",
            "permission_key": "entities.view",
            "active": requested_tab == "pilotable-ships",
        })

    return profile_menu

def coalition_profile_menu(coalition_id, requested_tab="killboard"):
    base_url = f"/coalition/{int(coalition_id)}"
    profile_menu = [
        {
            "id": None,
            "menu_key": "killboard",
            "label": "Killboard",
            "href": base_url,
            "icon": "☠",
            "permission_key": "entities.view",
            "active": requested_tab == "killboard",
        },
        {
            "id": None,
            "menu_key": "population",
            "label": "Population",
            "href": f"{base_url}?tab=population",
            "icon": "📈",
            "permission_key": "entities.view",
            "active": requested_tab == "population",
        },
        {
            "id": None,
            "menu_key": "history",
            "label": "History",
            "href": f"{base_url}?tab=history",
            "icon": "🕘",
            "permission_key": "entities.view",
            "active": requested_tab == "history",
        },
        {
            "id": None,
            "menu_key": "pilotable-ships",
            "label": "Pilotable Ships",
            "href": f"{base_url}?tab=pilotable-ships",
            "icon": "🚀",
            "permission_key": "entities.view",
            "active": requested_tab == "pilotable-ships",
        },
    ]

    return profile_menu
