import colorsys
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .audit import audit_log
from .auth import require_login
from .db import db
from .layout import app_context
from .main_objects import templates

router = APIRouter()

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
HSL_COLOR_RE = re.compile(
    r"^hsl\(\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s*\)$",
    re.I,
)


def _is_admin(user):
    return bool(user) and "admin" in user.get("roles", [])


def _ensure_override_table(cur):
    cur.execute("CREATE SCHEMA IF NOT EXISTS sovereignty")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sovereignty.influence_color_overrides (
            entity_type TEXT NOT NULL
                CHECK (entity_type IN ('alliance', 'coalition')),
            entity_id BIGINT NOT NULL,
            color TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by BIGINT,
            PRIMARY KEY (entity_type, entity_id)
        )
    """)


def _hex_to_hsl(value):
    if not HEX_COLOR_RE.fullmatch(value or ""):
        raise ValueError("invalid_color")

    value = value.lstrip("#")
    red = int(value[0:2], 16) / 255.0
    green = int(value[2:4], 16) / 255.0
    blue = int(value[4:6], 16) / 255.0
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return (
        f"hsl({hue * 360.0:.1f} {saturation * 100.0:.1f}% "
        f"{lightness * 100.0:.1f}%)"
    )


def _stored_color_to_hex(value):
    value = str(value or "").strip()
    if HEX_COLOR_RE.fullmatch(value):
        return value.lower()

    match = HSL_COLOR_RE.fullmatch(value)
    if not match:
        return "#808080"

    hue = (float(match.group(1)) % 360.0) / 360.0
    saturation = max(0.0, min(1.0, float(match.group(2)) / 100.0))
    lightness = max(0.0, min(1.0, float(match.group(3)) / 100.0))
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return "#{:02x}{:02x}{:02x}".format(
        round(red * 255),
        round(green * 255),
        round(blue * 255),
    )


def _load_color_rows():
    with db() as conn:
        with conn.cursor() as cur:
            _ensure_override_table(cur)

            cur.execute("""
                SELECT to_regclass('sovereignty.reconciled_map')
            """)
            reconciled_exists = cur.fetchone()[0] is not None

            alliance_rows = []
            if reconciled_exists:
                cur.execute("""
                    WITH latest_system_state AS (
                        SELECT DISTINCT ON (system_id)
                            system_id,
                            alliance_id
                        FROM sovereignty.reconciled_map
                        ORDER BY system_id, day DESC
                    )
                    SELECT DISTINCT
                        a.alliance_id,
                        a.name
                    FROM latest_system_state s
                    JOIN entities.alliances a
                      ON a.alliance_id = s.alliance_id
                    WHERE s.alliance_id IS NOT NULL
                    ORDER BY lower(a.name), a.alliance_id
                """)
                alliance_rows = cur.fetchall()

            cur.execute("""
                SELECT coalition_id, name
                FROM entities.coalitions
                WHERE is_active = TRUE
                ORDER BY lower(name), coalition_id
            """)
            coalition_rows = cur.fetchall()

            cur.execute("""
                SELECT entity_type, entity_id, color
                FROM sovereignty.influence_color_overrides
            """)
            override_colors = {
                (str(entity_type), int(entity_id)): color
                for entity_type, entity_id, color in cur.fetchall()
            }

            cur.execute("""
                SELECT to_regclass('sovereignty.influence_color_assignments')
            """)
            assignments_exist = cur.fetchone()[0] is not None
            automatic_colors = {}
            if assignments_exist:
                cur.execute("""
                    SELECT DISTINCT ON (entity_type, entity_id)
                        entity_type,
                        entity_id,
                        color
                    FROM sovereignty.influence_color_assignments
                    ORDER BY
                        entity_type,
                        entity_id,
                        (valid_to IS NULL) DESC,
                        assignment_id DESC
                """)
                automatic_colors = {
                    (str(entity_type), int(entity_id)): color
                    for entity_type, entity_id, color in cur.fetchall()
                }

        conn.commit()

    alliances = []
    for entity_id, name in alliance_rows:
        entity_id = int(entity_id)
        key = ("alliance", entity_id)
        stored = override_colors.get(key) or automatic_colors.get(key)
        alliances.append({
            "id": entity_id,
            "name": name or f"Alliance {entity_id}",
            "color": _stored_color_to_hex(stored),
            "overridden": key in override_colors,
        })

    coalitions = []
    for entity_id, name in coalition_rows:
        entity_id = int(entity_id)
        key = ("coalition", entity_id)
        stored = override_colors.get(key) or automatic_colors.get(key)
        coalitions.append({
            "id": entity_id,
            "name": name or f"Coalition {entity_id}",
            "color": _stored_color_to_hex(stored),
            "overridden": key in override_colors,
        })

    return alliances, coalitions


@router.get("/admin/sov-colors", response_class=HTMLResponse)
def admin_sov_colors(request: Request):
    user = require_login(request)
    if not _is_admin(user):
        return RedirectResponse(url="/home", status_code=302)

    alliances, coalitions = _load_color_rows()
    query = (request.query_params.get("q") or "").strip().casefold()
    if query:
        alliances = [
            row for row in alliances
            if query in row["name"].casefold() or query in str(row["id"])
        ]
        coalitions = [
            row for row in coalitions
            if query in row["name"].casefold() or query in str(row["id"])
        ]

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin - SOV Colors",
        active_module="admin",
        active_menu_key="admin.sov_colors",
    )
    context.update({
        "alliances": alliances,
        "coalitions": coalitions,
        "query": request.query_params.get("q") or "",
        "saved": request.query_params.get("saved") == "1",
        "reset": request.query_params.get("reset") == "1",
    })
    return templates.TemplateResponse(
        request=request,
        name="admin_sov_colors.html",
        context=context,
    )


@router.post("/admin/sov-colors")
def admin_sov_color_update(
    request: Request,
    entity_type: str = Form(...),
    entity_id: int = Form(...),
    color: str = Form(...),
):
    user = require_login(request)
    if not _is_admin(user):
        return RedirectResponse(url="/home", status_code=302)

    entity_type = str(entity_type or "").strip().lower()
    if entity_type not in {"alliance", "coalition"} or entity_id <= 0:
        return RedirectResponse(url="/admin/sov-colors", status_code=302)

    try:
        stored_color = _hex_to_hsl(color)
    except ValueError:
        return RedirectResponse(url="/admin/sov-colors", status_code=302)

    with db() as conn:
        with conn.cursor() as cur:
            _ensure_override_table(cur)

            if entity_type == "alliance":
                cur.execute(
                    "SELECT 1 FROM entities.alliances WHERE alliance_id = %s",
                    (entity_id,),
                )
            else:
                cur.execute(
                    "SELECT 1 FROM entities.coalitions WHERE coalition_id = %s",
                    (entity_id,),
                )
            if cur.fetchone() is None:
                return RedirectResponse(url="/admin/sov-colors", status_code=302)

            cur.execute("""
                INSERT INTO sovereignty.influence_color_overrides (
                    entity_type,
                    entity_id,
                    color,
                    updated_at,
                    updated_by
                )
                VALUES (%s, %s, %s, NOW(), %s)
                ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                    color = EXCLUDED.color,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
            """, (
                entity_type,
                entity_id,
                stored_color,
                user["id"],
            ))
        conn.commit()

    audit_log(
        request,
        "admin_sov_color_updated",
        user_id=user["id"],
        username=user["username"],
        target_type=entity_type,
        target_id=entity_id,
        details=f"color={stored_color}",
    )

    return RedirectResponse(url="/admin/sov-colors?saved=1", status_code=302)


@router.post("/admin/sov-colors/reset")
def admin_sov_color_reset(
    request: Request,
    entity_type: str = Form(...),
    entity_id: int = Form(...),
):
    user = require_login(request)
    if not _is_admin(user):
        return RedirectResponse(url="/home", status_code=302)

    entity_type = str(entity_type or "").strip().lower()
    if entity_type not in {"alliance", "coalition"} or entity_id <= 0:
        return RedirectResponse(url="/admin/sov-colors", status_code=302)

    with db() as conn:
        with conn.cursor() as cur:
            _ensure_override_table(cur)
            cur.execute("""
                DELETE FROM sovereignty.influence_color_overrides
                WHERE entity_type = %s
                  AND entity_id = %s
            """, (entity_type, entity_id))
        conn.commit()

    audit_log(
        request,
        "admin_sov_color_reset",
        user_id=user["id"],
        username=user["username"],
        target_type=entity_type,
        target_id=entity_id,
        details="automatic_color",
    )

    return RedirectResponse(url="/admin/sov-colors?reset=1", status_code=302)
