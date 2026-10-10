from .db import db, t


TOOLS = (
    ("tools.ship_analysis", "Ship Analysis", "/ship-analysis", "Compare ships, ship groups and combat activity across entities.", "entities.view"),
    ("tools.super_evolution", "Super Evolution", "/superintel/evolution", "Compare the evolution of supercapital pilot intelligence on a graph.", "superintel.view"),
    ("tools.population", "Population comparison", "/tools/population", "Compare population and PvP activity across alliances and coalitions.", "entities.view"),
    ("tools.economics", "Economics comparison", "/tools/economics", "Compare monthly economic estimates and purchasing power across entities.", "entities.view"),
    ("tools.map", "Universe map", "/map", "Explore regions, constellations and solar systems.", "entities.view"),
    ("tools.map_2d", "2D EVE map", "/map/eve-2d", "Explore New Eden and Anoikis on an interactive map.", "entities.view"),
    ("tools.influence", "Sovereignty history", "/map/eve-2d?mode=influence", "Follow alliance and coalition territory through time.", "entities.view"),
    ("tools.heat", "Fight heat map", "/map/eve-2d?mode=heat", "Explore known and hidden combat activity with killboard filters.", "entities.view"),
    ("tools.economy", "Economic map", "/map/eve-2d?mode=economy", "Compare regional mining, production and NPC bounties from the MER.", "entities.view"),
    ("tools.monthly_analysis", "SuperINTEL monthly analysis", "/superintel/monthly-analysis", "Investigate monthly changes in supercapital pilot intelligence.", "superintel.view"),
)


def visible_tools(user):
    permissions = user.get("permissions", set())
    return [{"id": None, "menu_key": key, "label": label, "href": href,
             "description": description, "permission_key": permission, "icon": ""}
            for key, label, href, description, permission in TOOLS if permission in permissions]


def fetch_visible_menu_items(user, parent_id=None):
    permissions = user.get("permissions", set())

    with db() as conn:
        with conn.cursor() as cur:
            if parent_id is None:
                cur.execute(
                    f"""
                    SELECT id, menu_key, label, href, icon, permission_key
                    FROM {t('menu_items')}
                    WHERE parent_id IS NULL
                      AND is_active = TRUE
                    ORDER BY sort_order, label
                    """
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, menu_key, label, href, icon, permission_key
                    FROM {t('menu_items')}
                    WHERE parent_id = %s
                      AND is_active = TRUE
                    ORDER BY sort_order, label
                    """,
                    (parent_id,),
                )
            rows = cur.fetchall()

    items = []
    for row in rows:
        permission_key = row[5]
        if permission_key and permission_key not in permissions:
            continue
        items.append({
            "id": row[0],
            "menu_key": row[1],
            "label": row[2],
            "href": row[3],
            "icon": row[4] or "",
            "permission_key": permission_key,
        })
    return items


def fetch_menu_item_by_key(menu_key):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, menu_key, label, href, icon, permission_key
                FROM {t('menu_items')}
                WHERE menu_key = %s
                  AND is_active = TRUE
                """,
                (menu_key,),
            )
            row = cur.fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "menu_key": row[1],
        "label": row[2],
        "href": row[3],
        "icon": row[4] or "",
        "permission_key": row[5],
    }


def build_top_menu(user, active_module):
    items = fetch_visible_menu_items(user, parent_id=None)

    permissions = user.get("permissions", set())

    if "entities.view" in permissions and not any(item["menu_key"] == "killboard" for item in items):
        killboard_item = {
            "id": None,
            "menu_key": "killboard",
            "label": "Killboard",
            "href": "/killboard",
            "icon": "",
            "permission_key": "entities.view",
        }
        insert_at = len(items)
        for index, item in enumerate(items):
            if item.get("menu_key") == "admin":
                insert_at = index
                break
        items.insert(insert_at, killboard_item)

    if "entities.view" in permissions and not any(item["menu_key"] == "profiles" for item in items):
        items.append({
            "id": None,
            "menu_key": "profiles",
            "label": "Profiles",
            "href": "/profiles",
            "icon": "",
            "permission_key": "entities.view",
        })

    if visible_tools(user) and not any(item["menu_key"] == "tools" for item in items):
        items.append({"id": None, "menu_key": "tools", "label": "Tools", "href": "/tools",
                      "icon": "", "permission_key": None})

    for item in items:
        item["active"] = item["menu_key"] == active_module
    return items


def build_context_menu(user, active_module, active_menu_key):
    if active_module == "tools":
        items = [{"menu_key": "tools", "label": "All tools", "href": "/tools", "icon": ""}] + visible_tools(user)
        for item in items:
            item["active"] = item["menu_key"] == active_menu_key
        return {"title": "Tools", "items": items}

    parent = fetch_menu_item_by_key(active_module)
    if not parent:
        return {"title": "Menu", "items": []}

    items = fetch_visible_menu_items(user, parent_id=parent["id"])

    if active_module == "entities":
        geography_keys = {
            "entities.system",
            "entities.constellation",
            "entities.region",
            "entities.map",
        }
        merged = []
        map_added = False
        for item in items:
            if item["menu_key"] in geography_keys:
                if not map_added:
                    merged.append({
                        "id": item.get("id"),
                        "menu_key": "entities.map",
                        "label": "MAP",
                        "href": "/map",
                        "icon": "▧",
                        "permission_key": "entities.view",
                    })
                    map_added = True
                continue
            merged.append(item)

        if not map_added:
            merged.append({
                "id": None,
                "menu_key": "entities.map",
                "label": "MAP",
                "href": "/map",
                "icon": "▧",
                "permission_key": "entities.view",
            })
        items = merged

    if active_module == "superintel" and "superintel.view" in user.get("permissions", set()):
        if not any(item.get("menu_key") == "superintel.detailed" for item in items):
            detailed_item = {
                "id": None,
                "menu_key": "superintel.detailed",
                "label": "Detailed Dashboard",
                "href": "/superintel/detailed",
                "icon": "≡",
                "permission_key": "superintel.view",
            }
            insert_at = len(items)
            for index, item in enumerate(items):
                if item.get("menu_key") == "superintel.dashboard":
                    insert_at = index + 1
                    break
            items.insert(insert_at, detailed_item)

    if active_module == "admin" and "admin.system.view" in user.get("permissions", set()):
        if not any(item.get("menu_key") == "admin.git" for item in items):
            git_item = {
                "id": None,
                "menu_key": "admin.git",
                "label": "Git",
                "href": "/admin/git",
                "icon": "⌘",
                "permission_key": "admin.system.view",
            }
            insert_at = len(items)
            for index, item in enumerate(items):
                if item.get("menu_key") == "admin.system":
                    insert_at = index + 1
                    break
            items.insert(insert_at, git_item)

    if active_module == "admin" and "admin.system.view" in user.get("permissions", set()):
        if not any(item.get("menu_key") == "admin.web_logs" for item in items):
            web_logs_item = {
                "id": None,
                "menu_key": "admin.web_logs",
                "label": "Web Logs",
                "href": "/admin/web-logs",
                "icon": "≡",
                "permission_key": "admin.system.view",
            }
            insert_at = len(items)
            for index, item in enumerate(items):
                if item.get("menu_key") == "admin.system":
                    insert_at = index + 1
                    break
            items.insert(insert_at, web_logs_item)

    if active_module == "admin" and "admin.jobs.view" in user.get("permissions", set()):
        if not any(item.get("menu_key") == "admin.killmail_statistics" for item in items):
            items.append({
                "id": None, "menu_key": "admin.killmail_statistics", "label": "Killmail Statistics",
                "href": "/admin/killmail-statistics", "icon": "▤", "permission_key": "admin.jobs.view",
            })
        if not any(item.get("menu_key") == "admin.update_pipeline" for item in items):
            pipeline_item = {
                "id": None,
                "menu_key": "admin.update_pipeline",
                "label": "Update Pipeline",
                "href": "/admin/update-pipeline",
                "icon": "⟳",
                "permission_key": "admin.jobs.view",
            }
            insert_at = len(items)
            for index, item in enumerate(items):
                if item.get("menu_key") == "admin.jobs":
                    insert_at = index + 1
                    break
            items.insert(insert_at, pipeline_item)

        if not any(item.get("menu_key") == "admin.debug" for item in items):
            debug_item = {
                "id": None,
                "menu_key": "admin.debug",
                "label": "Debug",
                "href": "/admin/debug",
                "icon": "⌁",
                "permission_key": "admin.jobs.view",
            }
            insert_at = len(items)
            for index, item in enumerate(items):
                if item.get("menu_key") == "admin.update_pipeline":
                    insert_at = index + 1
                    break
            items.insert(insert_at, debug_item)

    if active_module == "admin" and "admin" in user.get("roles", []):
        if not any(item.get("menu_key") == "admin.sov_colors" for item in items):
            sov_colors_item = {
                "id": None,
                "menu_key": "admin.sov_colors",
                "label": "SOV Colors",
                "href": "/admin/sov-colors",
                "icon": "◐",
                "permission_key": None,
            }
            insert_at = len(items)
            for index, item in enumerate(items):
                if item.get("menu_key") == "admin.mer":
                    insert_at = index + 1
                    break
            items.insert(insert_at, sov_colors_item)

    for item in items:
        item["active"] = (
            item["menu_key"] == active_menu_key
            or (item["menu_key"] == "entities.map" and active_menu_key in {
                "entities.system",
                "entities.constellation",
                "entities.region",
            })
        )

    return {"title": parent["label"], "items": items}
