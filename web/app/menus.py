from .db import db, t


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

    for item in items:
        item["active"] = item["menu_key"] == active_module
    return items


def build_context_menu(user, active_module, active_menu_key):
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
        if not any(item.get("menu_key") == "admin.nginx" for item in items):
            nginx_item = {
                "id": None,
                "menu_key": "admin.nginx",
                "label": "Nginx",
                "href": "/admin/nginx",
                "icon": "⇄",
                "permission_key": "admin.system.view",
            }
            insert_at = len(items)
            for index, item in enumerate(items):
                if item.get("menu_key") == "admin.system":
                    insert_at = index + 1
                    break
            items.insert(insert_at, nginx_item)

    if active_module == "admin" and "admin.jobs.view" in user.get("permissions", set()):
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
