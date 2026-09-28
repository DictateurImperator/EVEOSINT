import json
import logging
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Path, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from psycopg2.errors import QueryCanceled

from .auth import has_permission, require_login, require_permission_or_redirect
from .entities import (
    EntityError,
    get_global_killmails_page,
    get_global_killmails_scan_month,
    get_alliance_killmails_page,
    get_coalition_killmails_page,
    get_character_killmails_page,
    get_character_pilotable_ships,
    get_coalition_pilotable_category_counts,
    get_coalition_pilotable_ships,
    get_coalition_pilotable_ship_pilots,
    get_corporation_killmails_page,
    get_group_pilotable_category_counts,
    get_group_pilotable_ships,
    get_group_pilotable_ship_pilots,
    get_killmail_detail,
    get_mer_killmail_match_detail,
    get_mer_hidden_killmail_detail,
    get_entity_profile,
    get_recent_entities_for_index,
    get_ship_selection_profile,
    get_ship_selection_ranking,
    get_ship_killmails_page,
    get_ship_selection_killmails_page,
    get_system_killmails_page,
    get_location_killmails_page,
    get_killmail_filter_affiliations,
    get_killmail_ship_options,
    search_killmail_modules,
    search_entities,
    search_global_entities,
    search_killmail_locations,
)
from .db import db
from .coalitions import get_coalition, list_coalitions, list_coalition_overviews, list_memberships
from .layout import app_context
from .map_data import MapDataError, get_constellation_map, get_location_preview, get_region_map, get_system_map, get_universe_map
from .main_objects import templates
from .population_intelligence import (
    get_alliance_population_history,
    get_alliance_population_intelligence,
    get_alliance_population_intelligence_group,
    get_alliance_population_intelligence_series,
    get_alliance_population_indicator_pilots,
    get_alliance_population_indicator_pilot_subset,
    get_alliance_population_flow_chunk,
    get_alliance_population_summary,
    get_corporation_population_history,
    get_corporation_population_intelligence,
    get_corporation_population_intelligence_group,
    get_corporation_population_intelligence_series,
    get_corporation_population_indicator_pilots,
    get_corporation_population_flow_chunk,
    get_corporation_population_summary,
    get_coalition_population_dependencies,
    get_coalition_population_history,
    get_coalition_population_initialization_state,
    get_coalition_population_intelligence,
    get_coalition_population_intelligence_group,
    get_coalition_population_intelligence_series,
    get_coalition_population_indicator_pilots,
    get_coalition_population_flow_chunk,
    get_coalition_population_summary,
    get_coalition_affiliation_flow_summary,
    get_coalition_affiliation_flow_pilots,
    get_coalition_affiliation_flow_series,
    invalidate_coalition_population_cache,
)
from .population_alliances_init import (
    alliance_history_initialization_needed,
    get_alliance_history_initialization_state,
    initialize_alliance_on_demand,
)
from .population_corporations_init import (
    corporation_history_initialization_needed,
    get_corporation_history_initialization_state,
    initialize_corporation_on_demand,
)
from .ship_analysis import (
    enforce_ship_analysis_super_rights,
    iter_ship_analysis_stream,
    normalize_ship_analysis_request,
    search_ship_analysis_entities,
)

router = APIRouter()

logger = logging.getLogger(__name__)


def _entity_route_error_status(exc):
    if isinstance(exc, ValueError):
        return 400

    code = str(exc)

    if code.startswith("permission_denied"):
        return 403

    if "not_found" in code:
        return 404

    if "timeout" in code:
        return 504

    if code.startswith((
        "entity_type_invalid",
        "entity_profile_id_invalid",
        "killmail_filter_invalid",
        "killmail_filter_date_range_invalid",
        "killmail_id_invalid",
        "mer_killmail_id_invalid",
        "ship_analysis_invalid",
        "ship_analysis_too_many_series",
    )):
        return 400

    return 500


def _entity_route_error_response(request, exc, status_code=None):
    resolved_status = status_code or _entity_route_error_status(exc)
    logger.exception(
        "Entity route failed method=%s path=%s status=%s error=%s",
        request.method,
        request.url.path,
        resolved_status,
        exc,
    )
    return Response(status_code=resolved_status)



ENTITY_INDEX_PAGES = {
    "character": {"title": "Character", "description": "Search and open character entity sheets."},
    "corporation": {"title": "Corporation", "description": "Search and open corporation entity sheets."},
    "alliance": {"title": "Alliance", "description": "Search and open alliance entity sheets."},
    "coalition": {"title": "Coalition", "description": "Browse and open EVEOSINT coalition entities."},
    "ship": {"title": "Ships", "description": "Ship entity sheets from SDE type IDs."},
    "skill": {"title": "Skills", "description": "Skill entity sheets from SDE type IDs."},
    "commodity": {"title": "Commodities", "description": "Commodity and item entity sheets from SDE type IDs."},
    "system": {"title": "Systems", "description": "Solar system entity sheets."},
    "constellation": {"title": "Constellations", "description": "Constellation entity sheets."},
    "region": {"title": "Regions", "description": "Region entity sheets."},
}


@router.get("/entities", response_class=HTMLResponse)
def entities_index_redirect(request: Request):
    require_login(request)
    return RedirectResponse(url="/entities/character", status_code=302)


@router.get("/profiles", response_class=HTMLResponse)
def profiles_hub(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Profiles",
        active_module="profiles",
        active_menu_key="profiles",
    )
    context.update({
        "context_title": "Profiles",
        "context_menu": [],
    })

    return templates.TemplateResponse(
        request=request,
        name="profiles.html",
        context=context,
    )


def _map_response(request: Request, user, payload):
    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - MAP - {payload['title']}",
        active_module="entities",
        active_menu_key="entities.map",
    )
    context.update({
        "map_payload": payload,
        "map_data_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<" + "\\/"),
    })
    return templates.TemplateResponse(
        request=request,
        name="system_map.html" if payload.get("scope") == "system" else "map.html",
        context=context,
    )


@router.get("/map", response_class=HTMLResponse)
def map_universe(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect
    return _map_response(request, user, get_universe_map())


@router.get("/map/region/{region_id}", response_class=HTMLResponse)
def map_region(request: Request, region_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect
    try:
        payload = get_region_map(region_id)
    except MapDataError:
        return RedirectResponse(url="/map", status_code=302)
    return _map_response(request, user, payload)


@router.get("/map/constellation/{constellation_id}", response_class=HTMLResponse)
def map_constellation(request: Request, constellation_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect
    try:
        payload = get_constellation_map(constellation_id)
    except MapDataError:
        return RedirectResponse(url="/map", status_code=302)
    return _map_response(request, user, payload)


@router.get("/map/system/{system_id}", response_class=HTMLResponse)
def map_system(request: Request, system_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect
    try:
        payload = get_system_map(system_id)
    except MapDataError:
        return RedirectResponse(url="/map", status_code=302)
    return _map_response(request, user, payload)


@router.get("/entities/coalition/manage", response_class=HTMLResponse)
def entities_coalitions_manage(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    query = (request.query_params.get("q") or "").strip()
    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - EntitiesINTEL Coalition Manage",
        active_module="entities",
        active_menu_key="entities.coalition",
    )
    context.update({
        "coalitions": list_coalitions(query=query or None),
        "query": query,
        "can_admin_coalitions": True,
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })
    return templates.TemplateResponse(
        request=request,
        name="entities_coalitions_manage.html",
        context=context,
    )


@router.get("/entities/{entity_type}", response_class=HTMLResponse)
def entities_index(request: Request, entity_type: str):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    entity_type = (entity_type or "").strip().lower()
    if entity_type in {"system", "constellation", "region", "map"}:
        return RedirectResponse(url="/map", status_code=302)
    if entity_type == "weapon":
        return RedirectResponse(url="/entities/commodity", status_code=302)
    if entity_type not in ENTITY_INDEX_PAGES:
        return RedirectResponse(url="/entities/character", status_code=302)

    if entity_type == "coalition":
        query = (request.query_params.get("q") or "").strip()
        context = app_context(
            request=request,
            user=user,
            title="EVEOSINT - EntitiesINTEL Coalition",
            active_module="entities",
            active_menu_key="entities.coalition",
        )
        coalition_overview = list_coalition_overviews(query=query or None)
        context.update({
            "coalitions": coalition_overview["active"],
            "closed_coalitions": coalition_overview["closed"],
            "active_coalition_count": coalition_overview["active_count"],
            "closed_coalition_count": coalition_overview["closed_count"],
            "query": query,
            "can_admin_coalitions": has_permission(user, "entities.coalition.admin"),
            "error": request.query_params.get("error"),
        })
        return templates.TemplateResponse(
            request=request,
            name="entities_coalitions.html",
            context=context,
        )

    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - EntitiesINTEL {ENTITY_INDEX_PAGES[entity_type]['title']}",
        active_module="entities",
        active_menu_key=f"entities.{entity_type}",
    )
    ship_tier_filter = request.query_params.get("tier") if entity_type == "ship" else None
    ship_group_filter = request.query_params.get("group") if entity_type == "ship" else None
    ship_size_filter = request.query_params.get("size") if entity_type == "ship" else None
    ship_faction_filter = request.query_params.get("faction") if entity_type == "ship" else None

    ship_filter_label = None
    if ship_tier_filter:
        ship_filter_label = ship_tier_filter.strip()
    elif ship_group_filter:
        ship_filter_label = ship_group_filter.strip()
    elif ship_size_filter:
        ship_filter_label = ship_size_filter.strip()
    elif ship_faction_filter:
        ship_filter_label = ship_faction_filter.strip()

    context.update({
        "entity_index": {
            "entity_type": entity_type,
            "ship_tier_filter": ship_tier_filter,
            "ship_group_filter": ship_group_filter,
            "ship_size_filter": ship_size_filter,
            "ship_faction_filter": ship_faction_filter,
            "ship_filter_label": ship_filter_label,
            **ENTITY_INDEX_PAGES[entity_type],
        },
        "error": request.query_params.get("error"),
    })

    return templates.TemplateResponse(
        request=request,
        name="entity_index.html",
        context=context,
    )


def _same_text(left, right):
    return (left or "").strip().lower() == (right or "").strip().lower()


def _filter_ship_entities(recent_entities, tier_filter=None, group_filter=None, size_filter=None, faction_filter=None):
    if tier_filter:
        return [
            entity
            for entity in recent_entities
            if _same_text(entity.get("ship_tier"), tier_filter)
        ]

    if group_filter:
        return [
            entity
            for entity in recent_entities
            if (
                _same_text(entity.get("ship_size"), group_filter)
                or _same_text(entity.get("group_name"), group_filter)
                or _same_text(entity.get("ship_sde_band"), group_filter)
            )
        ]

    if size_filter:
        return [
            entity
            for entity in recent_entities
            if _same_text(entity.get("ship_display_size"), size_filter)
        ]

    if faction_filter:
        return [
            entity
            for entity in recent_entities
            if _same_text(entity.get("faction_name"), faction_filter)
        ]

    return recent_entities


@router.get("/api/entities/{entity_type}/recent", response_class=HTMLResponse)
def entities_recent_fragment(request: Request, entity_type: str = Path(...), tier: str | None = Query(None), group: str | None = Query(None), size: str | None = Query(None), faction: str | None = Query(None)):
    require_login(request)

    entity_type = (entity_type or "").strip().lower()
    recent_entities = []
    error = None
    ship_tier_filter = tier if entity_type == "ship" else None
    ship_group_filter = group if entity_type == "ship" else None
    ship_size_filter = size if entity_type == "ship" else None
    ship_faction_filter = faction if entity_type == "ship" else None
    ship_filter_label = None

    if ship_tier_filter:
        ship_filter_label = ship_tier_filter.strip()
    elif ship_group_filter:
        ship_filter_label = ship_group_filter.strip()
    elif ship_size_filter:
        ship_filter_label = ship_size_filter.strip()
    elif ship_faction_filter:
        ship_filter_label = ship_faction_filter.strip()

    if entity_type == "weapon":
        entity_type = "commodity"
    if entity_type not in ENTITY_INDEX_PAGES:
        error = "entity_type_invalid"
    else:
        try:
            recent_entities = get_recent_entities_for_index(entity_type, limit=100)
            if entity_type == "ship":
                recent_entities = _filter_ship_entities(
                    recent_entities,
                    tier_filter=ship_tier_filter,
                    group_filter=ship_group_filter,
                    size_filter=ship_size_filter,
                    faction_filter=ship_faction_filter,
                )
        except QueryCanceled:
            error = "Loading failed after 30 seconds."
        except EntityError as exc:
            return _entity_route_error_response(request, exc)
        except Exception:
            error = "Loading failed."

    return templates.TemplateResponse(
        request=request,
        name="entity_recent_fragment.html",
        context={
            "entity_index": {
                "entity_type": entity_type,
                "ship_tier_filter": ship_tier_filter,
                "ship_group_filter": ship_group_filter,
                "ship_size_filter": ship_size_filter,
                "ship_faction_filter": ship_faction_filter,
                "ship_filter_label": ship_filter_label,
                **ENTITY_INDEX_PAGES.get(entity_type, {"title": entity_type, "description": ""}),
            },
            "recent_entities": recent_entities,
            "recent_entities_error": error,
        },
    )


@router.get("/api/entity-search")
def entity_search(request: Request):
    require_login(request)

    q = request.query_params.get("q", "")
    limit = request.query_params.get("limit", "8")
    scope = request.query_params.get("scope", "profiles")

    try:
        if scope == "global":
            results = search_global_entities(q, limit=limit)
        else:
            results = search_entities(q, limit=limit)
    except Exception:
        logger.exception("Entity search failed")
        return JSONResponse({"results": [], "error": "search_failed"}, status_code=500)

    return {"results": results}


@router.get("/entity-search")
def entity_search_redirect(request: Request):
    require_login(request)

    q = request.query_params.get("q", "")
    try:
        results = search_entities(q, limit=1)
    except Exception:
        results = []

    if results:
        return RedirectResponse(url=results[0]["url"], status_code=302)
    return RedirectResponse(url="/", status_code=302)




def _killmail_filters_from_request(request):
    query = request.query_params
    return {
        "participation": query.get("participation", "both"),
        "date_from": query.get("date_from"),
        "date_to": query.get("date_to"),
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
        "scan_before": query.get("scan_before"),
        "scan_month": query.get("scan_month"),
        "scan_row": query.get("scan_row"),
    }


def _killmail_filters_have_user_filters(filters):
    filters = filters or {}
    return bool(
        str(filters.get("participation") or "both") != "both"
        or filters.get("date_from")
        or filters.get("date_to")
        or filters.get("affiliation_corporation_ids")
        or filters.get("affiliation_alliance_ids")
        or filters.get("involved_corporation_ids")
        or filters.get("involved_alliance_ids")
        or filters.get("type_ids")
        or filters.get("module_type_ids")
        or filters.get("builder_ship_include")
        or filters.get("builder_ship_exclude")
        or filters.get("builder_entity_include")
        or filters.get("builder_entity_exclude")
        or filters.get("builder_zone_include")
        or filters.get("builder_zone_exclude")
    )


@router.get("/killboard", response_class=HTMLResponse)
def global_killboard(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    error = None
    killmail_page = None
    killboard_defer_scan = False
    request_filters = _killmail_filters_from_request(request)

    if _killmail_filters_have_user_filters(request_filters):
        # Filtered history must be loaded by /killboard/scan month by month.
        # Do not execute the old all-history query during SSR / browser refresh.
        killboard_defer_scan = True
    else:
        try:
            killmail_page = get_global_killmails_page(
                page=request.query_params.get("page", "1"),
                per_page=100,
                filters=request_filters,
            )
        except EntityError as exc:
            return _entity_route_error_response(request, exc)
        except QueryCanceled:
            error = "Killboard query timed out."
        except Exception:
            logger.exception("global_killboard_failed")
            error = "Killboard loading failed."

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Killboard",
        active_module="killboard",
        active_menu_key="killboard",
    )
    context.update({
        "context_title": "Killboard",
        "context_menu": [],
        "killmail_page": killmail_page,
        "killmail_mode": "api",
        "killmail_base_url": None,
        "error": error,
        "killboard_defer_scan": killboard_defer_scan,
    })
    return templates.TemplateResponse(
        request=request,
        name="killboard.html",
        context=context,
    )


@router.get("/killboard/data", response_class=HTMLResponse)
def global_killboard_fragment(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    error = None
    killmail_page = None
    request_filters = _killmail_filters_from_request(request)
    try:
        if _killmail_filters_have_user_filters(request_filters):
            # Safety net for stale pagination / old frontend code:
            # never send a filtered request through the monolithic history path.
            killmail_page = get_global_killmails_scan_month(
                per_page=100,
                filters=request_filters,
            )
        else:
            killmail_page = get_global_killmails_page(
                page=request.query_params.get("page", "1"),
                per_page=100,
                filters=request_filters,
            )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        error = "Killboard query timed out."
    except Exception:
        logger.exception("global_killboard_fragment_failed")
        error = "Killboard loading failed."

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "killmail_page": killmail_page,
            "killmail_mode": "api",
        },
    )


@router.get("/killboard/scan", response_class=HTMLResponse)
def global_killboard_scan(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    error = None
    killmail_page = None

    try:
        remaining = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
        killmail_page = get_global_killmails_scan_month(
            per_page=remaining,
            filters=_killmail_filters_from_request(request),
        )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        error = "This month exceeded the query time limit."
    except Exception:
        logger.exception("global_killboard_scan_failed")
        error = "Killboard monthly scan failed."

    response = templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "killmail_page": killmail_page,
            "killmail_mode": "api",
        },
    )

    pagination = (
        (killmail_page or {}).get("killmail_pagination") or {}
        if killmail_page
        else {}
    )
    response.headers["X-KB-Matches"] = str(
        len((killmail_page or {}).get("killmails") or [])
    )
    response.headers["X-KB-Scanned"] = str(
        int(pagination.get("scanned_estimate") or 0)
    )
    response.headers["X-KB-Month-Complete"] = (
        "1" if pagination.get("month_complete") else "0"
    )
    response.headers["X-KB-Scan-Complete"] = (
        "1" if pagination.get("scan_complete") else "0"
    )

    if pagination.get("scan_month"):
        response.headers["X-KB-Scan-Month"] = pagination["scan_month"].isoformat()
    if pagination.get("next_scan_month"):
        response.headers["X-KB-Next-Month"] = pagination["next_scan_month"].isoformat()
    if pagination.get("next_scan_before"):
        response.headers["X-KB-Next-Before"] = pagination["next_scan_before"].isoformat()
    if pagination.get("next_scan_row") is not None:
        response.headers["X-KB-Next-Row"] = str(pagination["next_scan_row"])

    return response


@router.get("/killboard/search", response_class=JSONResponse)
def global_killboard_search(
    request: Request,
    kind: str = Query(...),
    q: str = Query(""),
    limit: int = Query(12),
):
    require_login(request)
    query = str(q or "").strip()
    if len(query) < 2:
        return JSONResponse({"results": []})

    limit = max(1, min(int(limit or 12), 30))
    kind = str(kind or "").strip().lower()

    if kind == "entity":
        results = [
            item for item in search_entities(query, limit=min(limit, 10))
            if item.get("entity_type") in {"character", "corporation", "alliance"}
        ]
        return JSONResponse({"results": results[:limit]})

    if kind == "zone":
        return JSONResponse({
            "results": search_killmail_locations(
                query,
                limit=limit,
            )
        })

    if kind == "ship":
        needle = query.lower()
        items = []
        for item in get_killmail_ship_options():
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
        return JSONResponse({"results": items})

    return JSONResponse({"results": [], "error": "invalid_kind"}, status_code=400)


@router.get("/api/killmail-filter/entity-search", response_class=JSONResponse)
def killmail_filter_entity_search(request: Request, q: str = Query(""), limit: int = Query(12)):
    require_login(request)
    results = [item for item in search_entities(q, limit=max(1, min(limit * 2, 24))) if item.get("entity_type") in {"corporation", "alliance"}]
    return JSONResponse({"results": results[:limit]})


@router.get("/api/killmail-filter/module-search", response_class=JSONResponse)
def killmail_filter_module_search(request: Request, q: str = Query(""), limit: int = Query(20)):
    require_login(request)
    return JSONResponse({"results": search_killmail_modules(q, limit=limit)})


@router.get("/api/killmail-filter/ships", response_class=JSONResponse)
def killmail_filter_ships(request: Request):
    require_login(request)
    return JSONResponse({"results": get_killmail_ship_options()})


@router.get("/api/{entity_type}/{entity_id}/killmail-affiliations", response_class=JSONResponse)
def killmail_filter_affiliations(request: Request, entity_type: str, entity_id: int):
    require_login(request)
    return JSONResponse(get_killmail_filter_affiliations(entity_type, entity_id))


@router.get("/api/location/{entity_type}/{entity_id}/killmails", response_class=HTMLResponse)
def location_killmails_fragment(request: Request, entity_type: str, entity_id: int):
    require_login(request)

    if entity_type not in {"system", "constellation", "region"}:
        return HTMLResponse("Invalid location type.", status_code=400)

    mode = request.query_params.get("mode", "api")
    error = None
    killmail_page = None
    try:
        remaining_per_page = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
    except (TypeError, ValueError):
        remaining_per_page = 100

    try:
        killmail_page = get_location_killmails_page(
            entity_type,
            entity_id,
            page=1,
            per_page=remaining_per_page,
            mode=mode,
            filters=_killmail_filters_from_request(request),
        )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "killmail_mode": (killmail_page or {}).get("killmail_mode", mode),
            "killmail_page": killmail_page,
        },
    )


@router.get("/api/character/{character_id}/killmails", response_class=HTMLResponse)
def character_killmails_fragment(request: Request, character_id: int):
    require_login(request)

    page = request.query_params.get("page", "1")
    error = None
    killmail_page = None
    try:
        remaining_per_page = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
    except (TypeError, ValueError):
        remaining_per_page = 100

    try:
        killmail_page = get_character_killmails_page(character_id, page=page, per_page=remaining_per_page, filters=_killmail_filters_from_request(request))
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "character_id": character_id,
            "killmail_page": killmail_page,
        },
    )


def _is_super_or_titan_ship(ship):
    group_name = str(ship.get("group_name") or "").strip().lower()
    ship_size = str(ship.get("ship_size") or "").strip().lower()
    ship_sde_band = str(ship.get("ship_sde_band") or "").strip().lower()

    return (
        group_name in {"supercarrier", "titan"}
        or ship_size == "supercapital"
        or ship_sde_band == "supercapital ships"
    )




def _filter_ship_selection_for_super_rights(selection, user):
    if has_permission(user, "superintel.view"):
        return selection, None

    if not selection:
        return selection, None

    ships = selection.get("ships") or []
    restricted = [ship for ship in ships if _is_super_or_titan_ship(ship)]
    if not restricted:
        return selection, None

    allowed_ships = [ship for ship in ships if not _is_super_or_titan_ship(ship)]
    if not allowed_ships:
        return None, "Insufficient rights."

    filtered = dict(selection)
    filtered["ships"] = allowed_ships
    filtered["ship_ids"] = [int(ship["entity_id"]) for ship in allowed_ships]
    filtered["ships_count"] = len(allowed_ships)
    filtered["super_titan_hidden"] = True
    return filtered, None

def _ship_selection_error_context(request, user, title, error):
    context = app_context(
        request=request,
        user=user,
        title=title,
        active_module="entities",
        active_menu_key="entities.ship",
    )
    context.update({"selection": None, "error": error})
    return context


def _hide_super_titan_ships(pilotable_page):
    if not pilotable_page:
        return pilotable_page

    copied_page = dict(pilotable_page)
    visible_ships = []
    hidden_exact_values = {
        "ship_size": set(),
        "ship_sde_band": set(),
        "group_name": set(),
        "filter_group": set(),
    }

    for ship in copied_page.get("ships", []):
        if _is_super_or_titan_ship(ship):
            for key in ("ship_size", "ship_sde_band", "group_name"):
                value = str(ship.get(key) or "").strip()
                if value:
                    hidden_exact_values[key].add(value)
                    hidden_exact_values["filter_group"].add(value)
        else:
            visible_ships.append(ship)

    copied_page["ships"] = visible_ships

    category_counts = copied_page.get("category_pilot_counts")
    if isinstance(category_counts, dict):
        filtered_counts = {}
        for scope, values in category_counts.items():
            if not isinstance(values, dict):
                filtered_counts[scope] = values
                continue
            hidden_values = hidden_exact_values.get(scope, set())
            filtered_counts[scope] = {
                key: value
                for key, value in values.items()
                if str(key).strip() not in hidden_values
            }
        copied_page["category_pilot_counts"] = filtered_counts

    copied_page["super_titan_hidden"] = True
    return copied_page


def _pilotable_ships_direct_navigation_redirect(request, entity_type, entity_id, tier=None, group=None, size=None, faction=None):
    fetch_mode = (request.headers.get("sec-fetch-mode") or "").lower()
    fetch_dest = (request.headers.get("sec-fetch-dest") or "").lower()

    if fetch_mode != "navigate" and fetch_dest != "document":
        return None

    params = [("tab", "pilotable-ships")]
    for key, value in (
        ("tier", tier),
        ("group", group),
        ("size", size),
        ("faction", faction),
    ):
        if value:
            params.append((key, value))

    return RedirectResponse(
        url=f"/{entity_type}/{entity_id}?{urlencode(params)}",
        status_code=302,
    )


def _pilotable_ships_fragment_response(request, entity_type, entity_id, pilotable_page, error, tier, group, size, faction):
    ship_tier_filter = tier
    ship_group_filter = group
    ship_size_filter = size
    ship_faction_filter = faction
    ship_filter_label = None

    if ship_tier_filter:
        ship_filter_label = ship_tier_filter.strip()
    elif ship_group_filter:
        ship_filter_label = ship_group_filter.strip()
    elif ship_size_filter:
        ship_filter_label = ship_size_filter.strip()
    elif ship_faction_filter:
        ship_filter_label = ship_faction_filter.strip()

    ships = pilotable_page.get("ships", []) if pilotable_page else []
    ships = _filter_ship_entities(
        ships,
        tier_filter=ship_tier_filter,
        group_filter=ship_group_filter,
        size_filter=ship_size_filter,
        faction_filter=ship_faction_filter,
    )

    if entity_type in {"corporation", "alliance", "coalition"}:
        contextual_ships = []
        for ship in ships:
            item = dict(ship)
            item["url"] = f"/{entity_type}/{entity_id}/pilotable-ships/{item['entity_id']}"
            contextual_ships.append(item)
        ships = contextual_ships

    has_ship_filter = bool(ship_tier_filter or ship_group_filter or ship_size_filter or ship_faction_filter)
    category_pilot_counts = {}
    ship_filter_pilot_count = None

    if has_ship_filter and entity_type in {"corporation", "alliance", "coalition"} and ships:
        try:
            if entity_type == "coalition":
                category_pilot_counts = get_coalition_pilotable_category_counts(
                    entity_id,
                    (pilotable_page or {}).get("scope_alliance_ids", []),
                    (pilotable_page or {}).get("scope_corporation_ids", []),
                    [ship.get("entity_id") for ship in ships],
                )
            else:
                category_pilot_counts = get_group_pilotable_category_counts(
                    entity_type,
                    entity_id,
                    [ship.get("entity_id") for ship in ships],
                )
            if ship_tier_filter:
                ship_filter_pilot_count = (category_pilot_counts.get("ship_tier") or {}).get(ship_tier_filter.strip())
            elif ship_group_filter:
                ship_filter_pilot_count = (category_pilot_counts.get("filter_group") or {}).get(ship_group_filter.strip())
            elif ship_size_filter:
                ship_filter_pilot_count = (category_pilot_counts.get("ship_display_size") or {}).get(ship_size_filter.strip())
            elif ship_faction_filter:
                ship_filter_pilot_count = (category_pilot_counts.get("faction") or {}).get(ship_faction_filter.strip())
        except Exception:
            category_pilot_counts = {}
            ship_filter_pilot_count = None

    return templates.TemplateResponse(
        request=request,
        name="entity_pilotable_ships_fragment.html",
        context={
            "error": error,
            "pilotable_page": pilotable_page,
            "entity_index": {
                "entity_type": "ship",
                "ship_tier_filter": ship_tier_filter,
                "ship_group_filter": ship_group_filter,
                "ship_size_filter": ship_size_filter,
                "ship_faction_filter": ship_faction_filter,
                "ship_filter_label": ship_filter_label,
                "ship_filter_pilot_count": ship_filter_pilot_count,
                "title": "Pilotable Ships",
                "description": "Pilotable ships from inferred skills.",
            },
            "recent_entities": ships,
            "recent_entities_error": error,
            "ship_filter_base_url": f"/{entity_type}/{entity_id}?tab=pilotable-ships&",
            "category_pilot_counts": category_pilot_counts,
        },
    )


@router.get("/api/character/{character_id}/pilotable-ships", response_class=HTMLResponse)
def character_pilotable_ships_fragment(request: Request, character_id: int, tier: str | None = Query(None), group: str | None = Query(None), size: str | None = Query(None), faction: str | None = Query(None)):
    redirect = _pilotable_ships_direct_navigation_redirect(
        request, "character", character_id, tier, group, size, faction
    )
    if redirect:
        return redirect

    user = require_login(request)

    error = None
    pilotable_page = None

    try:
        pilotable_page = get_character_pilotable_ships(character_id)
        if not has_permission(user, "superintel.view"):
            pilotable_page = _hide_super_titan_ships(pilotable_page)
        error = pilotable_page.get("error")
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return _pilotable_ships_fragment_response(request, "character", character_id, pilotable_page, error, tier, group, size, faction)


@router.get("/api/corporation/{corporation_id}/pilotable-ships", response_class=HTMLResponse)
def corporation_pilotable_ships_fragment(request: Request, corporation_id: int, tier: str | None = Query(None), group: str | None = Query(None), size: str | None = Query(None), faction: str | None = Query(None)):
    redirect = _pilotable_ships_direct_navigation_redirect(
        request, "corporation", corporation_id, tier, group, size, faction
    )
    if redirect:
        return redirect

    user = require_login(request)

    error = None
    pilotable_page = None

    try:
        pilotable_page = get_group_pilotable_ships(
            "corporation",
            corporation_id,
        )
        if not has_permission(user, "superintel.view"):
            pilotable_page = _hide_super_titan_ships(pilotable_page)
        error = pilotable_page.get("error")
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return _pilotable_ships_fragment_response(request, "corporation", corporation_id, pilotable_page, error, tier, group, size, faction)


@router.get("/api/alliance/{alliance_id}/pilotable-ships", response_class=HTMLResponse)
def alliance_pilotable_ships_fragment(request: Request, alliance_id: int, tier: str | None = Query(None), group: str | None = Query(None), size: str | None = Query(None), faction: str | None = Query(None)):
    redirect = _pilotable_ships_direct_navigation_redirect(
        request, "alliance", alliance_id, tier, group, size, faction
    )
    if redirect:
        return redirect

    user = require_login(request)

    error = None
    pilotable_page = None

    try:
        pilotable_page = get_group_pilotable_ships(
            "alliance",
            alliance_id,
        )
        if not has_permission(user, "superintel.view"):
            pilotable_page = _hide_super_titan_ships(pilotable_page)
        error = pilotable_page.get("error")
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return _pilotable_ships_fragment_response(request, "alliance", alliance_id, pilotable_page, error, tier, group, size, faction)


@router.get("/api/coalition/{coalition_id}/pilotable-ships", response_class=HTMLResponse)
def coalition_pilotable_ships_fragment(request: Request, coalition_id: int, tier: str | None = Query(None), group: str | None = Query(None), size: str | None = Query(None), faction: str | None = Query(None)):
    redirect = _pilotable_ships_direct_navigation_redirect(
        request, "coalition", coalition_id, tier, group, size, faction
    )
    if redirect:
        return redirect

    user = require_login(request)

    error = None
    pilotable_page = None

    try:
        scope = _coalition_killmail_scope(coalition_id)
        pilotable_page = get_coalition_pilotable_ships(
            coalition_id,
            scope.get("alliance_ids", []),
            scope.get("corporation_ids", []),
        )
        if not has_permission(user, "superintel.view"):
            pilotable_page = _hide_super_titan_ships(pilotable_page)
        error = pilotable_page.get("error")
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return _pilotable_ships_fragment_response(request, "coalition", coalition_id, pilotable_page, error, tier, group, size, faction)


@router.get("/api/alliance/{alliance_id}/population/pilots", response_class=HTMLResponse)
@router.get("/alliance/{alliance_id}/population/pilots", response_class=HTMLResponse)
def alliance_population_indicator_pilots_page(
    request: Request,
    alliance_id: int,
    metric: str = Query(...),
    date: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
    page: int = Query(1),
    q: str = Query(""),
    sort: str = Query(""),
    direction: str = Query(""),
    view: str = Query("corporations"),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    error = None
    profile = None
    pilot_list = None
    try:
        profile = get_entity_profile("alliance", alliance_id)
        pilot_list = get_alliance_population_indicator_pilots(
            alliance_id,
            metric=metric,
            anchor_date=date,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
            page=page,
            per_page=50,
            query=q,
            sort=sort,
            direction=direction,
            view=view,
        )
    except (ValueError, EntityError) as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Alliance population pilot list timeout alliance_id=%s metric=%s date=%s",
            alliance_id,
            metric,
            date,
        )
        error = "population_pilot_list_timeout"
    except Exception:
        logger.exception(
            "Alliance population pilot list failed alliance_id=%s metric=%s date=%s",
            alliance_id,
            metric,
            date,
        )
        error = "population_pilot_list_failed"

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Population Intelligence Pilots",
        active_module="profiles",
        active_menu_key="population",
    )
    context.update({
        "profile": profile,
        "pilot_list": pilot_list,
        "alliance_id": alliance_id,
        "error": error,
        "context_title": "Population Intelligence",
        "context_menu": [],
    })
    return templates.TemplateResponse(
        request=request,
        name="population_indicator_pilots.html",
        context=context,
    )


@router.get("/api/alliance/{alliance_id}/population/pilots/subset", response_class=HTMLResponse)
def alliance_population_indicator_pilots_subset(
    request: Request,
    alliance_id: int,
    metric: str = Query(...),
    date: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
    corporation_id: int | None = Query(None),
    outside: int = Query(0),
    page: int = Query(1),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    error = None
    subset = None
    try:
        subset = get_alliance_population_indicator_pilot_subset(
            alliance_id,
            metric=metric,
            anchor_date=date,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
            corporation_id=corporation_id,
            outside_alliance=bool(outside),
            page=page,
            per_page=50,
        )
    except (ValueError, EntityError) as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Alliance population pilot subset timeout alliance_id=%s metric=%s date=%s corporation_id=%s outside=%s",
            alliance_id,
            metric,
            date,
            corporation_id,
            bool(outside),
        )
        error = "population_pilot_subset_timeout"
    except Exception:
        logger.exception(
            "Alliance population pilot subset failed alliance_id=%s metric=%s date=%s corporation_id=%s outside=%s",
            alliance_id,
            metric,
            date,
            corporation_id,
            bool(outside),
        )
        error = "population_pilot_subset_failed"

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Population Intelligence Pilots",
        active_module="profiles",
        active_menu_key="population",
    )
    context.update({
        "subset": subset,
        "alliance_id": alliance_id,
        "error": error,
    })
    return templates.TemplateResponse(
        request=request,
        name="population_indicator_pilot_subset.html",
        context=context,
    )


@router.post("/api/alliance/{alliance_id}/population-history-check", response_class=JSONResponse)
def alliance_population_history_check_start(
    request: Request,
    background_tasks: BackgroundTasks,
    alliance_id: int,
):
    require_login(request)

    try:
        state = get_alliance_history_initialization_state(alliance_id)
        if state["initialization_done"]:
            return JSONResponse(content={"status": "complete", "state": state})

        profile = get_entity_profile("alliance", alliance_id)
        if not profile or profile.get("entity_type") != "alliance":
            return JSONResponse(status_code=404, content={"error": "alliance_not_found"})

        background_tasks.add_task(
            initialize_alliance_on_demand,
            profile["entity_id"],
            profile.get("name"),
        )
        return JSONResponse(
            status_code=202,
            content={"status": "started", "alliance_id": int(alliance_id)},
        )
    except Exception:
        logger.exception("Alliance DOTLAN history check start failed alliance_id=%s", alliance_id)
        return JSONResponse(status_code=500, content={"error": "dotlan_history_check_start_failed"})


@router.get("/api/alliance/{alliance_id}/population-history-check/status", response_class=JSONResponse)
def alliance_population_history_check_status(request: Request, alliance_id: int):
    require_login(request)
    try:
        return JSONResponse(content=get_alliance_history_initialization_state(alliance_id))
    except Exception:
        logger.exception("Alliance DOTLAN history check status failed alliance_id=%s", alliance_id)
        return JSONResponse(status_code=500, content={"error": "dotlan_history_check_status_failed"})


@router.get("/api/alliance/{alliance_id}/population-flows/chunk", response_class=JSONResponse)
def alliance_population_flow_chunk_data(
    request: Request,
    alliance_id: int,
    direction: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    analysis_start: str = Query(...),
    analysis_end: str = Query(...),
    mode: str = Query("any"),
):
    require_login(request)
    try:
        return JSONResponse(content=get_alliance_population_flow_chunk(
            alliance_id=alliance_id,
            direction=direction,
            start_date=start,
            end_date=end,
            analysis_start_date=analysis_start,
            analysis_end_date=analysis_end,
            activity_mode=mode,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Alliance population flow chunk timeout alliance_id=%s direction=%s start=%s end=%s analysis_end=%s mode=%s",
            alliance_id, direction, start, end, analysis_end, mode,
        )
        return JSONResponse(status_code=504, content={"error": "population_flow_chunk_timeout"})
    except Exception:
        logger.exception(
            "Alliance population flow chunk failed alliance_id=%s direction=%s start=%s end=%s analysis_end=%s mode=%s",
            alliance_id, direction, start, end, analysis_end, mode,
        )
        return JSONResponse(status_code=500, content={"error": "population_flow_chunk_failed"})


@router.get("/api/alliance/{alliance_id}/population", response_class=JSONResponse)
def alliance_population_data(request: Request, alliance_id: int):
    require_login(request)

    try:
        return JSONResponse(content=get_alliance_population_history(alliance_id))
    except Exception:
        logger.exception("Alliance population load failed for alliance_id=%s", alliance_id)
        return JSONResponse(
            status_code=500,
            content={"error": "population_load_failed"},
        )


@router.get("/api/alliance/{alliance_id}/population-intelligence", response_class=JSONResponse)
def alliance_population_intelligence_data(
    request: Request,
    alliance_id: int,
    dates: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
):
    require_login(request)

    requested_dates = [value.strip() for value in str(dates or "").split(",") if value.strip()]
    try:
        return JSONResponse(
            content=get_alliance_population_intelligence(
                alliance_id,
                requested_dates,
                activity_window_days=window,
                activity_mode=mode,
                core_months=core_months,
            )
        )
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning("Alliance population intelligence timeout alliance_id=%s", alliance_id)
        return JSONResponse(status_code=504, content={"error": "population_intelligence_timeout"})
    except Exception:
        logger.exception("Alliance population intelligence failed for alliance_id=%s", alliance_id)
        return JSONResponse(status_code=500, content={"error": "population_intelligence_failed"})


@router.get("/api/alliance/{alliance_id}/population-intelligence/group", response_class=JSONResponse)
def alliance_population_intelligence_group_data(
    request: Request,
    alliance_id: int,
    group: str = Query(...),
    dates: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
):
    require_login(request)

    requested_dates = [value.strip() for value in str(dates or "").split(",") if value.strip()]
    try:
        return JSONResponse(
            content=get_alliance_population_intelligence_group(
                alliance_id,
                group=group,
                dates=requested_dates,
                activity_window_days=window,
                activity_mode=mode,
                core_months=core_months,
            )
        )
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Alliance population intelligence group timeout alliance_id=%s group=%s",
            alliance_id,
            group,
        )
        return JSONResponse(status_code=504, content={"error": "population_intelligence_group_timeout"})
    except Exception:
        logger.exception(
            "Alliance population intelligence group failed alliance_id=%s group=%s",
            alliance_id,
            group,
        )
        return JSONResponse(status_code=500, content={"error": "population_intelligence_group_failed"})


@router.get("/api/alliance/{alliance_id}/population-intelligence/series", response_class=JSONResponse)
def alliance_population_intelligence_series_data(
    request: Request,
    alliance_id: int,
    metric: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
    max_points: int = Query(90),
):
    require_login(request)

    try:
        return JSONResponse(
            content=get_alliance_population_intelligence_series(
                alliance_id,
                metric=metric,
                date_from=date_from,
                date_to=date_to,
                activity_window_days=window,
                activity_mode=mode,
                core_months=core_months,
                max_points=max_points,
            )
        )
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Alliance population intelligence series timeout alliance_id=%s metric=%s",
            alliance_id,
            metric,
        )
        return JSONResponse(status_code=504, content={"error": "population_intelligence_series_timeout"})
    except Exception:
        logger.exception(
            "Alliance population intelligence series failed alliance_id=%s metric=%s",
            alliance_id,
            metric,
        )
        return JSONResponse(status_code=500, content={"error": "population_intelligence_series_failed"})



@router.get("/api/corporation/{corporation_id}/population/pilots", response_class=HTMLResponse)
@router.get("/corporation/{corporation_id}/population/pilots", response_class=HTMLResponse)
def corporation_population_indicator_pilots_page(
    request: Request,
    corporation_id: int,
    metric: str = Query(...),
    date: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
    page: int = Query(1),
    q: str = Query(""),
    sort: str = Query(""),
    direction: str = Query(""),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    error = None
    profile = None
    pilot_list = None
    try:
        profile = get_entity_profile("corporation", corporation_id)
        pilot_list = get_corporation_population_indicator_pilots(
            corporation_id,
            metric=metric,
            anchor_date=date,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
            page=page,
            per_page=50,
            query=q,
            sort=sort,
            direction=direction,
        )
    except (ValueError, EntityError) as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Corporation population pilot list timeout corporation_id=%s metric=%s date=%s",
            corporation_id, metric, date,
        )
        error = "population_pilot_list_timeout"
    except Exception:
        logger.exception(
            "Corporation population pilot list failed corporation_id=%s metric=%s date=%s",
            corporation_id, metric, date,
        )
        error = "population_pilot_list_failed"

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Population Intelligence Pilots",
        active_module="profiles",
        active_menu_key="population",
    )
    context.update({
        "profile": profile,
        "pilot_list": pilot_list,
        "entity_type": "corporation",
        "entity_id": corporation_id,
        "corporation_id": corporation_id,
        "error": error,
        "context_title": "Population Intelligence",
        "context_menu": [],
    })
    return templates.TemplateResponse(
        request=request,
        name="population_indicator_pilots.html",
        context=context,
    )


@router.post("/api/corporation/{corporation_id}/population-history-check", response_class=JSONResponse)
def corporation_population_history_check_start(
    request: Request,
    background_tasks: BackgroundTasks,
    corporation_id: int,
):
    require_login(request)
    try:
        state = get_corporation_history_initialization_state(corporation_id)
        if state["initialization_done"]:
            return JSONResponse(content={"status": "complete", "state": state})

        profile = get_entity_profile("corporation", corporation_id)
        if not profile or profile.get("entity_type") != "corporation":
            return JSONResponse(status_code=404, content={"error": "corporation_not_found"})

        background_tasks.add_task(
            initialize_corporation_on_demand,
            profile["entity_id"],
            profile.get("name"),
        )
        return JSONResponse(
            status_code=202,
            content={"status": "started", "corporation_id": int(corporation_id)},
        )
    except Exception:
        logger.exception("Corporation DOTLAN history check start failed corporation_id=%s", corporation_id)
        return JSONResponse(status_code=500, content={"error": "dotlan_history_check_start_failed"})


@router.get("/api/corporation/{corporation_id}/population-history-check/status", response_class=JSONResponse)
def corporation_population_history_check_status(request: Request, corporation_id: int):
    require_login(request)
    try:
        return JSONResponse(content=get_corporation_history_initialization_state(corporation_id))
    except Exception:
        logger.exception("Corporation DOTLAN history check status failed corporation_id=%s", corporation_id)
        return JSONResponse(status_code=500, content={"error": "dotlan_history_check_status_failed"})


@router.get("/api/corporation/{corporation_id}/population-flows/chunk", response_class=JSONResponse)
def corporation_population_flow_chunk_data(
    request: Request,
    corporation_id: int,
    direction: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    analysis_start: str = Query(...),
    analysis_end: str = Query(...),
    mode: str = Query("any"),
):
    require_login(request)
    try:
        return JSONResponse(content=get_corporation_population_flow_chunk(
            corporation_id=corporation_id,
            direction=direction,
            start_date=start,
            end_date=end,
            analysis_start_date=analysis_start,
            analysis_end_date=analysis_end,
            activity_mode=mode,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Corporation population flow chunk timeout corporation_id=%s direction=%s start=%s end=%s mode=%s",
            corporation_id, direction, start, end, mode,
        )
        return JSONResponse(status_code=504, content={"error": "population_flow_chunk_timeout"})
    except Exception:
        logger.exception(
            "Corporation population flow chunk failed corporation_id=%s direction=%s start=%s end=%s mode=%s",
            corporation_id, direction, start, end, mode,
        )
        return JSONResponse(status_code=500, content={"error": "population_flow_chunk_failed"})


@router.get("/api/character/{character_id}/history", response_class=JSONResponse)
def character_affiliation_history_data(request: Request, character_id: int):
    require_login(request)
    try:
        return JSONResponse(content=_get_character_affiliation_history(character_id))
    except QueryCanceled:
        logger.warning(
            "Character affiliation history timeout character_id=%s",
            character_id,
        )
        return JSONResponse(status_code=504, content={"error": "character_history_timeout"})
    except Exception:
        logger.exception(
            "Character affiliation history load failed character_id=%s",
            character_id,
        )
        return JSONResponse(status_code=500, content={"error": "character_history_load_failed"})


@router.get("/api/character/{character_id}/history/kills", response_class=JSONResponse)
def character_affiliation_history_kills_data(
    request: Request,
    character_id: int,
    start_at: str = Query(...),
    end_at: str | None = Query(None),
):
    require_login(request)
    try:
        return JSONResponse(content=_get_history_period_kills(
            "character",
            character_id,
            _parse_history_datetime_parameter(start_at),
            _parse_history_datetime_parameter(end_at) if end_at else None,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Character history period kill count timeout character_id=%s start_at=%s end_at=%s",
            character_id, start_at, end_at,
        )
        return JSONResponse(status_code=504, content={"error": "history_kill_count_timeout"})
    except Exception:
        logger.exception(
            "Character history period kill count failed character_id=%s start_at=%s end_at=%s",
            character_id, start_at, end_at,
        )
        return JSONResponse(status_code=500, content={"error": "history_kill_count_failed"})


@router.get("/api/corporation/{corporation_id}/history", response_class=JSONResponse)
def corporation_affiliation_history_data(request: Request, corporation_id: int):
    require_login(request)
    try:
        profile = get_entity_profile("corporation", corporation_id)
        refresh_status = initialize_corporation_on_demand(
            corporation_id,
            profile.get("name") if profile else None,
        )
        history = _get_corporation_affiliation_history(corporation_id)
        history["population_refresh_status"] = refresh_status
        return JSONResponse(content=history)
    except QueryCanceled:
        logger.warning(
            "Corporation affiliation history timeout corporation_id=%s",
            corporation_id,
        )
        return JSONResponse(status_code=504, content={"error": "corporation_history_timeout"})
    except Exception:
        logger.exception(
            "Corporation affiliation history load failed corporation_id=%s",
            corporation_id,
        )
        return JSONResponse(status_code=500, content={"error": "corporation_history_load_failed"})


@router.get("/api/corporation/{corporation_id}/history/kills", response_class=JSONResponse)
def corporation_affiliation_history_kills_data(
    request: Request,
    corporation_id: int,
    start_at: str = Query(...),
    end_at: str | None = Query(None),
):
    require_login(request)
    try:
        return JSONResponse(content=_get_history_period_kills(
            "corporation",
            corporation_id,
            _parse_history_datetime_parameter(start_at),
            _parse_history_datetime_parameter(end_at) if end_at else None,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Corporation history period kill count timeout corporation_id=%s start_at=%s end_at=%s",
            corporation_id, start_at, end_at,
        )
        return JSONResponse(status_code=504, content={"error": "history_kill_count_timeout"})
    except Exception:
        logger.exception(
            "Corporation history period kill count failed corporation_id=%s start_at=%s end_at=%s",
            corporation_id, start_at, end_at,
        )
        return JSONResponse(status_code=500, content={"error": "history_kill_count_failed"})


@router.get("/api/corporation/{corporation_id}/population", response_class=JSONResponse)
def corporation_population_data(request: Request, corporation_id: int):
    require_login(request)
    try:
        return JSONResponse(content=get_corporation_population_history(corporation_id))
    except Exception:
        logger.exception("Corporation population load failed for corporation_id=%s", corporation_id)
        return JSONResponse(status_code=500, content={"error": "population_load_failed"})


@router.get("/api/corporation/{corporation_id}/population-intelligence", response_class=JSONResponse)
def corporation_population_intelligence_data(
    request: Request,
    corporation_id: int,
    dates: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
):
    require_login(request)
    requested_dates = [value.strip() for value in str(dates or "").split(",") if value.strip()]
    try:
        return JSONResponse(content=get_corporation_population_intelligence(
            corporation_id,
            requested_dates,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning("Corporation population intelligence timeout corporation_id=%s", corporation_id)
        return JSONResponse(status_code=504, content={"error": "population_intelligence_timeout"})
    except Exception:
        logger.exception("Corporation population intelligence failed corporation_id=%s", corporation_id)
        return JSONResponse(status_code=500, content={"error": "population_intelligence_failed"})


@router.get("/api/corporation/{corporation_id}/population-intelligence/group", response_class=JSONResponse)
def corporation_population_intelligence_group_data(
    request: Request,
    corporation_id: int,
    group: str = Query(...),
    dates: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
):
    require_login(request)
    requested_dates = [value.strip() for value in str(dates or "").split(",") if value.strip()]
    try:
        return JSONResponse(content=get_corporation_population_intelligence_group(
            corporation_id,
            group=group,
            dates=requested_dates,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Corporation population intelligence group timeout corporation_id=%s group=%s",
            corporation_id, group,
        )
        return JSONResponse(status_code=504, content={"error": "population_intelligence_group_timeout"})
    except Exception:
        logger.exception(
            "Corporation population intelligence group failed corporation_id=%s group=%s",
            corporation_id, group,
        )
        return JSONResponse(status_code=500, content={"error": "population_intelligence_group_failed"})


@router.get("/api/corporation/{corporation_id}/population-intelligence/series", response_class=JSONResponse)
def corporation_population_intelligence_series_data(
    request: Request,
    corporation_id: int,
    metric: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
    max_points: int = Query(90),
):
    require_login(request)
    try:
        return JSONResponse(content=get_corporation_population_intelligence_series(
            corporation_id,
            metric=metric,
            date_from=date_from,
            date_to=date_to,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
            max_points=max_points,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Corporation population intelligence series timeout corporation_id=%s metric=%s",
            corporation_id, metric,
        )
        return JSONResponse(status_code=504, content={"error": "population_intelligence_series_timeout"})
    except Exception:
        logger.exception(
            "Corporation population intelligence series failed corporation_id=%s metric=%s",
            corporation_id, metric,
        )
        return JSONResponse(status_code=500, content={"error": "population_intelligence_series_failed"})


@router.get("/api/corporation/{corporation_id}/killmails", response_class=HTMLResponse)
def corporation_killmails_fragment(request: Request, corporation_id: int):
    require_login(request)

    page = request.query_params.get("page", "1")
    mode = request.query_params.get("mode", "api")
    error = None
    killmail_page = None
    try:
        remaining_per_page = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
    except (TypeError, ValueError):
        remaining_per_page = 100

    try:
        killmail_page = get_corporation_killmails_page(corporation_id, page=page, per_page=remaining_per_page, filters=_killmail_filters_from_request(request), mode=mode)
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "corporation_id": corporation_id,
            "killmail_mode": (killmail_page or {}).get("killmail_mode", mode),
            "killmail_page": killmail_page,
        },
    )


@router.get("/api/alliance/{alliance_id}/killmails", response_class=HTMLResponse)
def alliance_killmails_fragment(request: Request, alliance_id: int):
    require_login(request)

    page = request.query_params.get("page", "1")
    mode = request.query_params.get("mode", "api")
    error = None
    killmail_page = None
    try:
        remaining_per_page = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
    except (TypeError, ValueError):
        remaining_per_page = 100

    try:
        killmail_page = get_alliance_killmails_page(alliance_id, page=page, per_page=remaining_per_page, filters=_killmail_filters_from_request(request), mode=mode)
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "alliance_id": alliance_id,
            "killmail_mode": (killmail_page or {}).get("killmail_mode", mode),
            "killmail_page": killmail_page,
        },
    )


@router.get("/api/ship/{type_id}/killmails", response_class=HTMLResponse)
def ship_killmails_fragment(request: Request, type_id: int):
    user = require_login(request)

    page = request.query_params.get("page", "1")
    mode = request.query_params.get("mode", "api")
    error = None
    killmail_page = None
    try:
        remaining_per_page = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
    except (TypeError, ValueError):
        remaining_per_page = 100

    try:
        selection = get_ship_selection_profile(type_id=type_id)
        _, rights_error = _filter_ship_selection_for_super_rights(selection, user)
        if rights_error:
            error = rights_error
        else:
            killmail_page = get_ship_killmails_page(
                type_id,
                page=page,
                per_page=remaining_per_page,
                mode=mode,
                filters=_killmail_filters_from_request(request),
            )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "ship_type_id": type_id,
            "killmail_mode": (killmail_page or {}).get("killmail_mode", mode),
            "killmail_page": killmail_page,
        },
    )


@router.get("/api/ship-selection/killmails", response_class=HTMLResponse)
def ship_selection_killmails_fragment(
    request: Request,
    type_id: int | None = Query(None),
    tier: str | None = Query(None),
    group: str | None = Query(None),
    size: str | None = Query(None),
    faction: str | None = Query(None),
):
    user = require_login(request)

    page = request.query_params.get("page", "1")
    mode = request.query_params.get("mode", "api")
    error = None
    killmail_page = None
    try:
        remaining_per_page = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
    except (TypeError, ValueError):
        remaining_per_page = 100

    try:
        selection = get_ship_selection_profile(
            type_id=type_id,
            tier=tier,
            group=group,
            size=size,
            faction=faction,
        )
        selection, rights_error = _filter_ship_selection_for_super_rights(selection, user)
        if rights_error:
            error = rights_error
        else:
            killmail_page = get_ship_selection_killmails_page(
                selection.get("ship_ids", []),
                page=page,
                per_page=remaining_per_page,
                mode=mode,
                filters=_killmail_filters_from_request(request),
            )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "killmail_mode": (killmail_page or {}).get("killmail_mode", mode),
            "killmail_page": killmail_page,
        },
    )


def _profile_context_menu(profile, active_menu_key="pilotable-ships"):
    if not profile or profile.get("entity_type") not in {"character", "corporation", "alliance"}:
        return []
    base_url = f"/{profile['entity_type']}/{profile['entity_id']}"
    return [
        {
            "id": None,
            "menu_key": "killboard",
            "label": "Killboard",
            "href": base_url,
            "icon": "☠",
            "permission_key": "entities.view",
            "active": active_menu_key == "killboard",
        },
        {
            "id": None,
            "menu_key": "pilotable-ships",
            "label": "Pilotable Ships",
            "href": f"{base_url}?tab=pilotable-ships",
            "icon": "🚀",
            "permission_key": "entities.view",
            "active": active_menu_key == "pilotable-ships",
        },
    ]


def _group_pilotable_ship_detail_response(request, entity_type, entity_id, ship_type_id):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    page = request.query_params.get("page", "1")
    q = request.query_params.get("q", "")
    error = None
    detail = None

    try:
        detail = get_group_pilotable_ship_pilots(
            entity_type,
            entity_id,
            ship_type_id,
            page=page,
            per_page=100,
            q=q,
        )
        error = detail.get("error")
        if detail.get("ship") and not has_permission(user, "superintel.view") and _is_super_or_titan_ship(detail["ship"]):
            return RedirectResponse(url=f"/{entity_type}/{entity_id}?tab=pilotable-ships", status_code=302)
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    profile = detail.get("profile") if detail else None
    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Pilotable Ship Pilots",
        active_module="profiles",
        active_menu_key="pilotable-ships",
    )
    context.update({
        "error": error,
        "detail": detail,
        "profile": profile,
        "context_title": "Profile",
        "context_menu": _profile_context_menu(profile, "pilotable-ships"),
    })

    return templates.TemplateResponse(
        request=request,
        name="entity_pilotable_ship_pilots.html",
        context=context,
    )


@router.get("/corporation/{corporation_id}/pilotable-ships/{ship_type_id}", response_class=HTMLResponse)
def corporation_pilotable_ship_detail(request: Request, corporation_id: int, ship_type_id: int):
    return _group_pilotable_ship_detail_response(request, "corporation", corporation_id, ship_type_id)


@router.get("/alliance/{alliance_id}/pilotable-ships/{ship_type_id}", response_class=HTMLResponse)
def alliance_pilotable_ship_detail(request: Request, alliance_id: int, ship_type_id: int):
    return _group_pilotable_ship_detail_response(request, "alliance", alliance_id, ship_type_id)


def _coalition_profile_context_menu(coalition_id, active_menu_key="pilotable-ships"):
    base_url = f"/coalition/{int(coalition_id)}"
    return [
        {
            "id": None,
            "menu_key": "killboard",
            "label": "Killboard",
            "href": base_url,
            "icon": "☠",
            "permission_key": "entities.view",
            "active": active_menu_key == "killboard",
        },
        {
            "id": None,
            "menu_key": "population",
            "label": "Population",
            "href": f"{base_url}?tab=population",
            "icon": "📈",
            "permission_key": "entities.view",
            "active": active_menu_key == "population",
        },
        {
            "id": None,
            "menu_key": "history",
            "label": "History",
            "href": f"{base_url}?tab=history",
            "icon": "🕘",
            "permission_key": "entities.view",
            "active": active_menu_key == "history",
        },
        {
            "id": None,
            "menu_key": "pilotable-ships",
            "label": "Pilotable Ships",
            "href": f"{base_url}?tab=pilotable-ships",
            "icon": "🚀",
            "permission_key": "entities.view",
            "active": active_menu_key == "pilotable-ships",
        },
    ]


def _coalition_pilotable_ship_detail_response(request: Request, coalition_id: int, ship_type_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    page = request.query_params.get("page", "1")
    q = request.query_params.get("q", "")
    error = None
    detail = None

    try:
        scope = _coalition_killmail_scope(coalition_id)
        detail = get_coalition_pilotable_ship_pilots(
            coalition_id,
            scope.get("alliance_ids", []),
            scope.get("corporation_ids", []),
            ship_type_id,
            page=page,
            per_page=100,
            q=q,
        )
        error = detail.get("error")
        if detail.get("ship") and not has_permission(user, "superintel.view") and _is_super_or_titan_ship(detail["ship"]):
            return RedirectResponse(url=f"/coalition/{coalition_id}?tab=pilotable-ships", status_code=302)
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    profile = detail.get("profile") if detail else None
    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Coalition Pilotable Ship Pilots",
        active_module="profiles",
        active_menu_key="pilotable-ships",
    )
    context.update({
        "error": error,
        "detail": detail,
        "profile": profile,
        "context_title": "Profile",
        "context_menu": _coalition_profile_context_menu(coalition_id, "pilotable-ships"),
    })

    return templates.TemplateResponse(
        request=request,
        name="entity_pilotable_ship_pilots.html",
        context=context,
    )


@router.get("/coalition/{coalition_id}/pilotable-ships/{ship_type_id}", response_class=HTMLResponse)
def coalition_pilotable_ship_detail(request: Request, coalition_id: int, ship_type_id: int):
    return _coalition_pilotable_ship_detail_response(request, coalition_id, ship_type_id)


@router.get("/kill/mer/{source_month}/{source_row}", response_class=HTMLResponse)
def mer_killmail_ambiguous(request: Request, source_month: str, source_row: int, kill_datetime: str | None = Query(default=None)):
    user = require_login(request)

    error = None
    mer_match = None
    try:
        mer_match = get_mer_killmail_match_detail(source_month, source_row, kill_datetime)
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - MER killmail candidates",
        active_module=None,
        active_menu_key=None,
    )
    context.update({
        "error": error,
        "mer_match": mer_match,
    })
    return templates.TemplateResponse(
        request=request,
        name="mer_killmail_ambiguous.html",
        context=context,
    )


@router.get("/kill/mer/{source_month}/{source_row}/hidden", response_class=HTMLResponse)
def mer_hidden_killmail(request: Request, source_month: str, source_row: int, kill_datetime: str | None = Query(default=None)):
    user = require_login(request)

    error = None
    mer_killmail = None
    try:
        mer_killmail = get_mer_hidden_killmail_detail(source_month, source_row, kill_datetime)
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    title = "EVEOSINT - Hidden MER killmail"
    if mer_killmail and mer_killmail.get("victim_ship"):
        title = f"EVEOSINT - Hidden MER killmail - {mer_killmail['victim_ship']['name']}"

    context = app_context(
        request=request,
        user=user,
        title=title,
        active_module=None,
        active_menu_key=None,
    )
    context.update({
        "error": error,
        "mer_killmail": mer_killmail,
    })
    return templates.TemplateResponse(
        request=request,
        name="mer_killmail_hidden.html",
        context=context,
    )


@router.get("/kill/{killmail_id}", response_class=HTMLResponse)
def kill_profile(request: Request, killmail_id: int):
    user = require_login(request)

    error = None
    killmail = None
    try:
        killmail = get_killmail_detail(killmail_id)
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    title = "EVEOSINT - Killmail"
    if killmail and killmail.get("victim_ship"):
        title = f"EVEOSINT - Killmail {killmail_id} - {killmail['victim_ship']['name']}"

    context = app_context(
        request=request,
        user=user,
        title=title,
        active_module=None,
        active_menu_key=None,
    )
    context.update({
        "error": error,
        "killmail_id": killmail_id,
        "killmail": killmail,
    })
    return templates.TemplateResponse(
        request=request,
        name="killmail_detail.html",
        context=context,
    )


def _resolve_current_coalition_killmail_scope(coalition_id, stack=frozenset()):
    coalition_id = int(coalition_id)
    if coalition_id in stack:
        return set()

    includes = set()
    excludes = set()
    next_stack = stack | {coalition_id}

    for membership in list_memberships(coalition_id):
        if membership.get("status") != "current":
            continue

        member_type = str(membership.get("member_type") or "").strip().lower()
        member_id = int(membership["member_id"])

        if member_type == "coalition":
            target = _resolve_current_coalition_killmail_scope(member_id, next_stack)
        elif member_type in {"alliance", "corporation"}:
            target = {(member_type, member_id)}
        else:
            target = set()

        if membership.get("operation") == "exclude":
            excludes.update(target)
        else:
            includes.update(target)

    return includes - excludes


def _coalition_killmail_scope(coalition_id):
    resolved = _resolve_current_coalition_killmail_scope(coalition_id)
    return {
        "alliance_ids": sorted(entity_id for entity_type, entity_id in resolved if entity_type == "alliance"),
        "corporation_ids": sorted(entity_id for entity_type, entity_id in resolved if entity_type == "corporation"),
    }



def _subtract_date_intervals(include_intervals, exclude_intervals):
    result = []
    one_day = timedelta(days=1)
    for include_start, include_end in include_intervals:
        segments = [(include_start, include_end)]
        for exclude_start, exclude_end in exclude_intervals:
            next_segments = []
            for segment_start, segment_end in segments:
                if exclude_end < segment_start or exclude_start > segment_end:
                    next_segments.append((segment_start, segment_end))
                    continue
                if exclude_start > segment_start:
                    next_segments.append((segment_start, exclude_start - one_day))
                if exclude_end < segment_end:
                    next_segments.append((exclude_end + one_day, segment_end))
            segments = next_segments
            if not segments:
                break
        result.extend(segments)
    return result


def _merge_date_intervals(intervals):
    if not intervals:
        return []
    one_day = timedelta(days=1)
    ordered = sorted(intervals)
    merged = [[ordered[0][0], ordered[0][1]]]
    for start_day, end_day in ordered[1:]:
        previous = merged[-1]
        touches = start_day <= previous[1]
        if previous[1] != date.max:
            touches = touches or start_day == previous[1] + one_day
        if touches:
            if end_day > previous[1]:
                previous[1] = end_day
        else:
            merged.append([start_day, end_day])
    return [(row[0], row[1]) for row in merged]


def _coalition_history_resolver(conn):
    """Return effective top-level coalition state for historical entity keys.

    Coalition membership dates are inclusive.  Nested coalitions and
    INCLUDE/EXCLUDE rules use the same resolution semantics as the killboard.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.coalition_id,
                c.name,
                c.short_name,
                m.id,
                m.operation,
                m.member_type,
                m.member_id,
                m.valid_from,
                m.valid_to
            FROM entities.coalitions c
            LEFT JOIN entities.coalition_memberships m
              ON m.coalition_id = c.coalition_id
            ORDER BY c.coalition_id, m.id
            """
        )
        raw_rows = cur.fetchall()

    coalition_meta = {}
    rules_by_coalition = {}
    change_days = set()

    for coalition_id, name, short_name, membership_id, operation, member_type, member_id, valid_from, valid_to in raw_rows:
        coalition_id = int(coalition_id)
        coalition_meta[coalition_id] = {
            "coalition_id": coalition_id,
            "name": name or f"Coalition {coalition_id}",
            "short_name": short_name,
            "url": f"/coalition/{coalition_id}",
        }
        if membership_id is None or operation is None or member_type is None or member_id is None:
            continue

        rule = {
            "id": int(membership_id),
            "operation": str(operation).strip().lower(),
            "member_type": str(member_type).strip().lower(),
            "member_id": int(member_id),
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
        rules_by_coalition.setdefault(coalition_id, []).append(rule)

        if valid_from is not None:
            change_days.add(valid_from)
        if valid_to is not None and valid_to < date.max:
            change_days.add(valid_to + timedelta(days=1))

    state_cache = {}

    def day_state(day):
        cached = state_cache.get(day)
        if cached is not None:
            return cached

        def active(rule):
            return (
                (rule["valid_from"] is None or rule["valid_from"] <= day)
                and (rule["valid_to"] is None or rule["valid_to"] >= day)
            )

        resolved_cache = {}

        def resolve(coalition_id, stack=frozenset()):
            coalition_id = int(coalition_id)
            if coalition_id in resolved_cache:
                return resolved_cache[coalition_id]
            if coalition_id in stack:
                return frozenset()

            includes = set()
            excludes = set()
            next_stack = stack | {coalition_id}
            for rule in rules_by_coalition.get(coalition_id, []):
                if not active(rule):
                    continue
                if rule["member_type"] == "coalition":
                    target = set(resolve(rule["member_id"], next_stack))
                elif rule["member_type"] in {"alliance", "corporation"}:
                    target = {(rule["member_type"], rule["member_id"])}
                else:
                    continue

                if rule["operation"] == "exclude":
                    excludes.update(target)
                else:
                    includes.update(target)

            result = frozenset(includes - excludes)
            resolved_cache[coalition_id] = result
            return result

        resolved_by_coalition = {
            coalition_id: resolve(coalition_id)
            for coalition_id in coalition_meta
        }

        direct_child_state = {}
        for parent_id, rules in rules_by_coalition.items():
            for rule in rules:
                if not active(rule) or rule["member_type"] != "coalition":
                    continue
                key = (int(parent_id), int(rule["member_id"]))
                if rule["operation"] == "exclude":
                    direct_child_state[key] = False
                elif key not in direct_child_state:
                    direct_child_state[key] = True

        active_parent_links = {
            key for key, included in direct_child_state.items() if included
        }
        cached = (resolved_by_coalition, active_parent_links)
        state_cache[day] = cached
        return cached

    def resolve_entity(day, entity_keys):
        normalized_keys = {
            (str(entity_type), int(entity_id))
            for entity_type, entity_id in entity_keys
            if entity_id is not None
        }
        if not normalized_keys:
            return tuple()

        resolved_by_coalition, active_parent_links = day_state(day)
        matching = {
            coalition_id
            for coalition_id, resolved in resolved_by_coalition.items()
            if normalized_keys.intersection(resolved)
        }
        if not matching:
            return tuple()

        roots = {
            coalition_id
            for coalition_id in matching
            if not any(
                parent_id in matching and (parent_id, coalition_id) in active_parent_links
                for parent_id in matching
                if parent_id != coalition_id
            )
        }
        selected_ids = sorted(
            roots or matching,
            key=lambda coalition_id: (
                (coalition_meta[coalition_id]["name"] or "").casefold(),
                coalition_id,
            ),
        )
        return tuple(dict(coalition_meta[coalition_id]) for coalition_id in selected_ids)

    return resolve_entity, sorted(change_days)


def _coalition_segment_fields(coalitions):
    coalitions = tuple(coalitions or ())
    return {
        "coalitions": [dict(item) for item in coalitions],
        "coalition_key": tuple(int(item["coalition_id"]) for item in coalitions),
    }


def _split_affiliation_segments_by_coalition(segments, resolver, change_days):
    """Split affiliation segments whenever effective coalition membership changes."""
    today = date.today()
    historical_changes = [day for day in change_days if day <= today]
    result = []

    for source in segments:
        start_at = _history_datetime(source.get("start_at"))
        end_at = _history_datetime(source.get("end_at"))
        if start_at is None or end_at is None or end_at <= start_at:
            continue

        boundaries = [start_at, end_at]
        for change_day in historical_changes:
            boundary = datetime.combine(change_day, datetime.min.time(), tzinfo=timezone.utc)
            if start_at < boundary < end_at:
                boundaries.append(boundary)
        boundaries = sorted(set(boundaries))

        entity_keys = set()
        if source.get("alliance_id") is not None:
            entity_keys.add(("alliance", int(source["alliance_id"])))
        if source.get("corporation_id") is not None:
            entity_keys.add(("corporation", int(source["corporation_id"])))

        for index in range(len(boundaries) - 1):
            piece_start = boundaries[index]
            piece_end = boundaries[index + 1]
            if piece_end <= piece_start:
                continue
            coalitions = resolver(piece_start.date(), entity_keys)
            item = dict(source)
            item["start_at"] = piece_start
            item["end_at"] = piece_end
            item.update(_coalition_segment_fields(coalitions))
            result.append(item)

    return result


def _merge_history_segments(segments, identity_fields):
    merged = []
    for segment in sorted(segments, key=lambda row: (row["start_at"], row.get("record_id", 0))):
        identity = tuple(segment.get(field) for field in identity_fields)
        coalition_key = tuple(segment.get("coalition_key") or ())
        if merged:
            previous = merged[-1]
            previous_identity = tuple(previous.get(field) for field in identity_fields)
            previous_coalition_key = tuple(previous.get("coalition_key") or ())
            if (
                previous_identity == identity
                and previous_coalition_key == coalition_key
                and previous["end_at"] >= segment["start_at"]
            ):
                if segment["end_at"] > previous["end_at"]:
                    previous["end_at"] = segment["end_at"]
                previous["corporation_deleted"] = bool(
                    previous.get("corporation_deleted") or segment.get("corporation_deleted")
                )
                previous["alliance_deleted"] = bool(
                    previous.get("alliance_deleted") or segment.get("alliance_deleted")
                )
                continue
        merged.append(dict(segment))
    return merged


def _alliance_coalition_history(alliance_id):
    alliance_id = int(alliance_id)
    today = date.today()

    with db() as conn:
        resolver, change_days = _coalition_history_resolver(conn)

        alliance_start = None
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_jsonb(a)
                FROM entities.alliances a
                WHERE a.alliance_id = %s
                """,
                (alliance_id,),
            )
            raw = cur.fetchone()
        payload = raw[0] if raw and isinstance(raw[0], dict) else {}
        for key in ("date_founded", "date_created", "created_date"):
            value = payload.get(key)
            if not value:
                continue
            try:
                if isinstance(value, datetime):
                    alliance_start = value.date()
                elif isinstance(value, date):
                    alliance_start = value
                else:
                    alliance_start = date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                alliance_start = None
            if alliance_start is not None:
                break

        relevant_changes = sorted(day for day in change_days if day <= today)
        if alliance_start is not None:
            scan_start = alliance_start
        elif relevant_changes:
            scan_start = relevant_changes[0] - timedelta(days=1)
        else:
            scan_start = today

        boundaries = [scan_start]
        boundaries.extend(day for day in relevant_changes if day > scan_start)
        tomorrow = today + timedelta(days=1)
        boundaries.append(tomorrow)
        boundaries = sorted(set(boundaries))

        raw_periods = []
        for index in range(len(boundaries) - 1):
            start_day = boundaries[index]
            end_exclusive = boundaries[index + 1]
            if start_day > today or end_exclusive <= start_day:
                continue
            coalitions = resolver(start_day, {("alliance", alliance_id)})
            raw_periods.append({
                "start_day": start_day,
                "end_exclusive": min(end_exclusive, tomorrow),
                **_coalition_segment_fields(coalitions),
            })

        merged = []
        for period in raw_periods:
            if merged and tuple(merged[-1]["coalition_key"]) == tuple(period["coalition_key"]) and merged[-1]["end_exclusive"] == period["start_day"]:
                merged[-1]["end_exclusive"] = period["end_exclusive"]
            else:
                merged.append(dict(period))

        if alliance_start is None:
            first_membership = next(
                (index for index, period in enumerate(merged) if period.get("coalition_key")),
                None,
            )
            if first_membership is None:
                merged = []
            else:
                merged = merged[first_membership:]

        rows = []
        for index, period in enumerate(reversed(merged)):
            current = period["start_day"] <= today < period["end_exclusive"]
            public_end = None if current else period["end_exclusive"] - timedelta(days=1)
            public_start = period["start_day"]
            if (
                alliance_start is None
                and index == len(merged) - 1
                and period.get("coalition_key")
                and relevant_changes
                and public_start == relevant_changes[0] - timedelta(days=1)
            ):
                public_start = None

            rows.append({
                "coalitions": period.get("coalitions") or [],
                "coalition_key": list(period.get("coalition_key") or ()),
                "start_date": public_start.isoformat() if public_start else None,
                "end_date": public_end.isoformat() if public_end else None,
                "current": current,
            })

    return {
        "alliance_id": alliance_id,
        "created_date": alliance_start.isoformat() if alliance_start else None,
        "rows": rows,
    }


def _list_parent_coalition_memberships(coalition_id):
    coalition_id = int(coalition_id)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    m.coalition_id,
                    parent.name,
                    parent.short_name,
                    m.operation,
                    m.valid_from,
                    m.valid_to
                FROM entities.coalition_memberships m
                JOIN entities.coalitions parent
                  ON parent.coalition_id = m.coalition_id
                WHERE m.member_type = 'coalition'
                  AND m.member_id = %s
                ORDER BY lower(parent.name), m.valid_from NULLS FIRST, m.valid_to NULLS LAST, m.id
                """,
                (coalition_id,),
            )
            rows = cur.fetchall()

    by_parent = {}
    for parent_id, parent_name, parent_short_name, operation, valid_from, valid_to in rows:
        bucket = by_parent.setdefault(
            int(parent_id),
            {
                "coalition_id": int(parent_id),
                "name": parent_name or f"Coalition {int(parent_id)}",
                "short_name": parent_short_name,
                "includes": [],
                "excludes": [],
            },
        )
        bounds = (valid_from or date.min, valid_to or date.max)
        if str(operation or "").strip().lower() == "exclude":
            bucket["excludes"].append(bounds)
        else:
            bucket["includes"].append(bounds)

    today = date.today()
    result = []
    for bucket in by_parent.values():
        effective = _subtract_date_intervals(bucket["includes"], bucket["excludes"])
        effective = _merge_date_intervals(effective)
        for start_day, end_day in effective:
            if start_day > today:
                continue
            current = start_day <= today <= end_day
            public_start = None if start_day == date.min else start_day
            public_end = None if end_day == date.max else end_day
            duration_end = today if current else public_end
            duration_days = None
            if public_start is not None and duration_end is not None:
                duration_days = max(0, (duration_end - public_start).days + 1)
            result.append({
                "coalition_id": bucket["coalition_id"],
                "name": bucket["name"],
                "short_name": bucket["short_name"],
                "valid_from": public_start,
                "valid_to": public_end,
                "current": current,
                "duration_days": duration_days,
                "url": f"/coalition/{bucket['coalition_id']}",
            })

    result.sort(
        key=lambda row: (
            0 if row["current"] else 1,
            -(row["valid_to"].toordinal() if row["valid_to"] else date.max.toordinal()),
            -(row["valid_from"].toordinal() if row["valid_from"] else 0),
            row["name"].casefold(),
        )
    )
    return result


# ---------------------------------------------------------------------------
# Coalition Population Intelligence
# ---------------------------------------------------------------------------


def _coalition_population_history_state(coalition_id):
    return get_coalition_population_initialization_state(coalition_id)

def _initialize_coalition_population_histories(coalition_id):
    dependencies = get_coalition_population_dependencies(coalition_id)
    for alliance_id in dependencies.get("alliance_ids", []):
        try:
            state = get_alliance_history_initialization_state(alliance_id)
            last_error = str(state.get("last_error") or "")
            if last_error.startswith("dotlan_alliance_id_mismatch:"):
                continue
            if not state.get("initialization_done"):
                profile = get_entity_profile("alliance", alliance_id)
                initialize_alliance_on_demand(
                    alliance_id, (profile or {}).get("name")
                )
        except Exception:
            logger.exception(
                "Coalition population alliance history init failed coalition_id=%s alliance_id=%s",
                coalition_id,
                alliance_id,
            )

    for corporation_id in dependencies.get("corporation_ids", []):
        try:
            state = get_corporation_history_initialization_state(corporation_id)
            if not state.get("initialization_done"):
                profile = get_entity_profile("corporation", corporation_id)
                initialize_corporation_on_demand(
                    corporation_id, (profile or {}).get("name")
                )
        except Exception:
            logger.exception(
                "Coalition population corporation history init failed coalition_id=%s corporation_id=%s",
                coalition_id,
                corporation_id,
            )

    invalidate_coalition_population_cache(coalition_id)


@router.get("/api/coalition/{coalition_id}/population/pilots", response_class=HTMLResponse)
def coalition_population_indicator_pilots_page(
    request: Request,
    coalition_id: int,
    metric: str = Query(...),
    date: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
    page: int = Query(1),
    q: str = Query(""),
    sort: str = Query(""),
    direction: str = Query(""),
    view: str = Query("alliances"),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    error = None
    pilot_list = None
    coalition = get_coalition(coalition_id)
    profile = None
    if coalition:
        profile = {
            "entity_type": "coalition",
            "entity_id": int(coalition_id),
            "name": coalition["name"],
            "subtitle": "Coalition",
            "ticker": coalition.get("short_name"),
            "image_url": coalition.get("logo_url"),
        }
    try:
        pilot_list = get_coalition_population_indicator_pilots(
            coalition_id,
            metric=metric,
            anchor_date=date,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
            page=page,
            per_page=50,
            query=q,
            sort=sort,
            direction=direction,
            view=view,
        )
    except (ValueError, EntityError) as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        error = "population_pilot_list_timeout"
    except Exception:
        logger.exception(
            "Coalition population pilot list failed coalition_id=%s metric=%s date=%s",
            coalition_id,
            metric,
            date,
        )
        error = "population_pilot_list_failed"

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Population Intelligence Pilots",
        active_module="profiles",
        active_menu_key="population",
    )
    context.update({
        "profile": profile,
        "pilot_list": pilot_list,
        "coalition_id": coalition_id,
        "error": error,
        "context_title": "Population Intelligence",
        "context_menu": [],
    })
    return templates.TemplateResponse(
        request=request,
        name="population_indicator_pilots.html",
        context=context,
    )


@router.post("/api/coalition/{coalition_id}/population-history-check", response_class=JSONResponse)
def coalition_population_history_check_start(
    request: Request,
    background_tasks: BackgroundTasks,
    coalition_id: int,
):
    require_login(request)
    if not get_coalition(coalition_id):
        return JSONResponse(status_code=404, content={"error": "coalition_not_found"})
    try:
        state = _coalition_population_history_state(coalition_id)
        if state["initialization_done"]:
            return JSONResponse(content={"status": "complete", "state": state})
        background_tasks.add_task(_initialize_coalition_population_histories, coalition_id)
        return JSONResponse(
            status_code=202,
            content={"status": "started", "coalition_id": int(coalition_id)},
        )
    except Exception:
        logger.exception(
            "Coalition population history check start failed coalition_id=%s",
            coalition_id,
        )
        return JSONResponse(status_code=500, content={"error": "coalition_history_check_start_failed"})


@router.get("/api/coalition/{coalition_id}/population-history-check/status", response_class=JSONResponse)
def coalition_population_history_check_status(request: Request, coalition_id: int):
    require_login(request)
    if not get_coalition(coalition_id):
        return JSONResponse(status_code=404, content={"error": "coalition_not_found"})
    try:
        return JSONResponse(content=_coalition_population_history_state(coalition_id))
    except Exception:
        logger.exception(
            "Coalition population history status failed coalition_id=%s",
            coalition_id,
        )
        return JSONResponse(status_code=500, content={"error": "coalition_history_status_failed"})


@router.get("/api/coalition/{coalition_id}/population-flows/chunk", response_class=JSONResponse)
def coalition_population_flow_chunk_data(
    request: Request,
    coalition_id: int,
    direction: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    analysis_start: str = Query(...),
    analysis_end: str = Query(...),
    mode: str = Query("any"),
):
    require_login(request)
    try:
        return JSONResponse(content=get_coalition_population_flow_chunk(
            coalition_id=coalition_id,
            direction=direction,
            start_date=start,
            end_date=end,
            analysis_start_date=analysis_start,
            analysis_end_date=analysis_end,
            activity_mode=mode,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Coalition population flow chunk timeout coalition_id=%s direction=%s start=%s end=%s mode=%s",
            coalition_id,
            direction,
            start,
            end,
            mode,
        )
        return JSONResponse(status_code=504, content={"error": "population_flow_chunk_timeout"})
    except Exception:
        logger.exception(
            "Coalition population flow chunk failed coalition_id=%s direction=%s start=%s end=%s mode=%s",
            coalition_id,
            direction,
            start,
            end,
            mode,
        )
        return JSONResponse(status_code=500, content={"error": "population_flow_chunk_failed"})


def _parse_affiliation_entity_keys(raw_value):
    result = []
    seen = set()
    for token in str(raw_value or "").split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        entity_type, entity_id = token.split(":", 1)
        entity_type = entity_type.strip().lower()
        if entity_type not in {"corporation", "alliance", "coalition", "unknown"}:
            continue
        try:
            key = (entity_type, int(entity_id))
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


@router.get("/api/coalition/{coalition_id}/population-affiliations", response_class=JSONResponse)
def coalition_population_affiliations_data(
    request: Request,
    coalition_id: int,
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    require_login(request)
    if not get_coalition(coalition_id):
        return JSONResponse(status_code=404, content={"error": "coalition_not_found"})
    try:
        return JSONResponse(content=get_coalition_affiliation_flow_summary(
            coalition_id=coalition_id,
            start_date=start,
            end_date=end,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Coalition affiliation summary timeout coalition_id=%s start=%s end=%s",
            coalition_id,
            start,
            end,
        )
        return JSONResponse(status_code=504, content={"error": "population_affiliations_timeout"})
    except Exception:
        logger.exception(
            "Coalition affiliation summary failed coalition_id=%s start=%s end=%s",
            coalition_id,
            start,
            end,
        )
        return JSONResponse(status_code=500, content={"error": "population_affiliations_failed"})


@router.get("/api/coalition/{coalition_id}/population-affiliations/pilots", response_class=JSONResponse)
def coalition_population_affiliation_pilots_data(
    request: Request,
    coalition_id: int,
    kind: str = Query(...),
    entity_type: str = Query(...),
    entity_id: int = Query(...),
    start: str | None = Query(None),
    end: str | None = Query(None),
    page: int = Query(1),
):
    require_login(request)
    try:
        return JSONResponse(content=get_coalition_affiliation_flow_pilots(
            coalition_id=coalition_id,
            kind=kind,
            entity_type=entity_type,
            entity_id=entity_id,
            start_date=start,
            end_date=end,
            page=page,
            per_page=100,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        return JSONResponse(status_code=504, content={"error": "population_affiliation_pilots_timeout"})
    except Exception:
        logger.exception(
            "Coalition affiliation pilots failed coalition_id=%s kind=%s entity=%s:%s",
            coalition_id,
            kind,
            entity_type,
            entity_id,
        )
        return JSONResponse(status_code=500, content={"error": "population_affiliation_pilots_failed"})


@router.get("/api/coalition/{coalition_id}/population-affiliations/series", response_class=JSONResponse)
def coalition_population_affiliation_series_data(
    request: Request,
    coalition_id: int,
    origins: str = Query(""),
    destinations: str = Query(""),
    start: str | None = Query(None),
    end: str | None = Query(None),
    bucket: str = Query("month"),
):
    require_login(request)
    try:
        return JSONResponse(content=get_coalition_affiliation_flow_series(
            coalition_id=coalition_id,
            origins=_parse_affiliation_entity_keys(origins),
            destinations=_parse_affiliation_entity_keys(destinations),
            start_date=start,
            end_date=end,
            bucket=bucket,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        return JSONResponse(status_code=504, content={"error": "population_affiliation_series_timeout"})
    except Exception:
        logger.exception(
            "Coalition affiliation series failed coalition_id=%s start=%s end=%s",
            coalition_id,
            start,
            end,
        )
        return JSONResponse(status_code=500, content={"error": "population_affiliation_series_failed"})


@router.get("/api/coalition/{coalition_id}/population", response_class=JSONResponse)
def coalition_population_data(request: Request, coalition_id: int):
    require_login(request)
    try:
        return JSONResponse(content=get_coalition_population_history(coalition_id))
    except Exception:
        logger.exception(
            "Coalition population load failed coalition_id=%s", coalition_id
        )
        return JSONResponse(status_code=500, content={"error": "population_load_failed"})


@router.get("/api/coalition/{coalition_id}/population-intelligence", response_class=JSONResponse)
def coalition_population_intelligence_data(
    request: Request,
    coalition_id: int,
    dates: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
):
    require_login(request)
    requested_dates = [value.strip() for value in str(dates or "").split(",") if value.strip()]
    try:
        return JSONResponse(content=get_coalition_population_intelligence(
            coalition_id,
            requested_dates,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning("Coalition population intelligence timeout coalition_id=%s", coalition_id)
        return JSONResponse(status_code=504, content={"error": "population_intelligence_timeout"})
    except Exception:
        logger.exception("Coalition population intelligence failed coalition_id=%s", coalition_id)
        return JSONResponse(status_code=500, content={"error": "population_intelligence_failed"})


@router.get("/api/coalition/{coalition_id}/population-intelligence/group", response_class=JSONResponse)
def coalition_population_intelligence_group_data(
    request: Request,
    coalition_id: int,
    group: str = Query(...),
    dates: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
):
    require_login(request)
    requested_dates = [value.strip() for value in str(dates or "").split(",") if value.strip()]
    try:
        return JSONResponse(content=get_coalition_population_intelligence_group(
            coalition_id,
            group=group,
            dates=requested_dates,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Coalition population intelligence group timeout coalition_id=%s group=%s",
            coalition_id,
            group,
        )
        return JSONResponse(status_code=504, content={"error": "population_intelligence_group_timeout"})
    except Exception:
        logger.exception(
            "Coalition population intelligence group failed coalition_id=%s group=%s",
            coalition_id,
            group,
        )
        return JSONResponse(status_code=500, content={"error": "population_intelligence_group_failed"})


@router.get("/api/coalition/{coalition_id}/population-intelligence/series", response_class=JSONResponse)
def coalition_population_intelligence_series_data(
    request: Request,
    coalition_id: int,
    metric: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
    window: int = Query(90),
    mode: str = Query("any"),
    core_months: int = Query(6),
    max_points: int = Query(90),
):
    require_login(request)
    try:
        return JSONResponse(content=get_coalition_population_intelligence_series(
            coalition_id,
            metric=metric,
            date_from=date_from,
            date_to=date_to,
            activity_window_days=window,
            activity_mode=mode,
            core_months=core_months,
            max_points=max_points,
        ))
    except ValueError as exc:
        return _entity_route_error_response(request, exc)
    except QueryCanceled:
        logger.warning(
            "Coalition population intelligence series timeout coalition_id=%s metric=%s",
            coalition_id,
            metric,
        )
        return JSONResponse(status_code=504, content={"error": "population_intelligence_series_timeout"})
    except Exception:
        logger.exception(
            "Coalition population intelligence series failed coalition_id=%s metric=%s",
            coalition_id,
            metric,
        )
        return JSONResponse(status_code=500, content={"error": "population_intelligence_series_failed"})


@router.get("/api/coalition/{coalition_id}/killmails", response_class=HTMLResponse)
def coalition_killmails_fragment(request: Request, coalition_id: int):
    require_login(request)

    page = request.query_params.get("page", "1")
    mode = request.query_params.get("mode", "api")
    error = None
    killmail_page = None
    try:
        remaining_per_page = max(
            1,
            min(100, int(request.query_params.get("remaining", "100") or "100")),
        )
    except (TypeError, ValueError):
        remaining_per_page = 100

    try:
        scope = _coalition_killmail_scope(coalition_id)
        killmail_page = get_coalition_killmails_page(
            scope["alliance_ids"],
            scope["corporation_ids"],
            page=page,
            per_page=remaining_per_page,
            filters=_killmail_filters_from_request(request),
            mode=mode,
        )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="entity_killmails_fragment.html",
        context={
            "error": error,
            "coalition_id": coalition_id,
            "killmail_mode": (killmail_page or {}).get("killmail_mode", mode),
            "killmail_page": killmail_page,
        },
    )


@router.get("/coalition/{coalition_id}", response_class=HTMLResponse)
def coalition_profile(request: Request, coalition_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    coalition = get_coalition(coalition_id)
    if not coalition:
        return RedirectResponse(url="/entities/coalition?error=coalition_not_found", status_code=302)

    requested_tab = request.query_params.get("tab", "killboard")
    allowed_tabs = {"killboard", "population", "history", "pilotable-ships"}
    if requested_tab not in allowed_tabs:
        requested_tab = "killboard"

    memberships = list_memberships(coalition_id) if requested_tab == "history" else []
    parent_memberships = _list_parent_coalition_memberships(coalition_id) if requested_tab == "history" else []
    history_view = str(request.query_params.get("history_view", "members") or "members").strip().lower()
    if history_view not in {"members", "coalitions"}:
        history_view = "members"
    if history_view == "coalitions" and not parent_memberships:
        history_view = "members"

    coalition_population_summary = None
    try:
        coalition_population_summary = get_coalition_population_summary(coalition_id)
    except Exception:
        logger.exception(
            "Coalition population summary load failed for coalition_id=%s",
            coalition_id,
        )

    profile = {
        "entity_type": "coalition",
        "entity_id": int(coalition_id),
        "name": coalition["name"],
        "subtitle": "Coalition",
        "ticker": coalition.get("short_name"),
        "image_url": coalition.get("logo_url"),
    }

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

    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - Coalition {coalition['name']}",
        active_module="profiles",
        active_menu_key=requested_tab,
    )
    context.update({
        "error": request.query_params.get("error"),
        "coalition": coalition,
        "profile": profile,
        "profile_tab": requested_tab,
        "coalition_tab": requested_tab,
        "memberships": memberships,
        "parent_memberships": parent_memberships,
        "history_view": history_view,
        "can_admin_coalitions": has_permission(user, "entities.coalition.admin"),
        "killmail_page": None,
        "killmail_mode": request.query_params.get("mode", "api"),
        "alliance_population_summary": None,
        "corporation_population_summary": None,
        "coalition_population_summary": coalition_population_summary,
        "location_previews": {},
        "context_title": "Profile",
        "context_menu": profile_menu,
    })

    return templates.TemplateResponse(
        request=request,
        name="coalition_profile.html",
        context=context,
    )


@router.get("/coalition/{coalition_id}/manage", response_class=HTMLResponse)
def coalition_manage(request: Request, coalition_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    coalition = get_coalition(coalition_id)
    if not coalition:
        return RedirectResponse(url="/entities/coalition/manage?error=coalition_not_found", status_code=302)

    memberships = list_memberships(coalition_id)
    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - Manage Coalition {coalition['name']}",
        active_module="entities",
        active_menu_key="entities.coalition",
    )
    context.update({
        "coalition": coalition,
        "memberships": memberships,
        "can_admin_coalitions": True,
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })

    return templates.TemplateResponse(
        request=request,
        name="coalition_manage.html",
        context=context,
    )


@router.get("/character/{character_id}", response_class=HTMLResponse)
def character_profile(request: Request, character_id: int):
    return entity_profile(request, "character", character_id)


@router.get("/corporation/{corporation_id}", response_class=HTMLResponse)
def corporation_profile(request: Request, corporation_id: int):
    return entity_profile(request, "corporation", corporation_id)


@router.get("/alliance/{alliance_id}", response_class=HTMLResponse)
def alliance_profile(request: Request, alliance_id: int):
    return entity_profile(request, "alliance", alliance_id)


@router.get("/system/{system_id}", response_class=HTMLResponse)
def system_profile(request: Request, system_id: int):
    return entity_profile(request, "system", system_id)


@router.get("/constellation/{constellation_id}", response_class=HTMLResponse)
def constellation_profile(request: Request, constellation_id: int):
    return entity_profile(request, "constellation", constellation_id)


@router.get("/region/{region_id}", response_class=HTMLResponse)
def region_profile(request: Request, region_id: int):
    return entity_profile(request, "region", region_id)


def _ship_selection_context(request: Request, user, selection, title_suffix=""):
    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - {selection['title']}{title_suffix}",
        active_module="entities",
        active_menu_key="entities.ship",
    )
    context.update({
        "selection": selection,
        "error": None,
    })
    return context


@router.get("/ship-analysis", response_class=HTMLResponse)
def ship_analysis_page(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Ship Analysis",
        active_module="entities",
        active_menu_key="entities.ship",
    )
    context.update({
        "selection": {
            "title": "Ship Analysis",
            "ships": [],
        },
        "ship_analysis_standalone": True,
    })

    return templates.TemplateResponse(
        request=request,
        name="ship_analysis_page.html",
        context=context,
    )


@router.get("/ship/{type_id}", response_class=HTMLResponse)
def ship_profile(request: Request, type_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    try:
        selection = get_ship_selection_profile(type_id=type_id)
        selection, rights_error = _filter_ship_selection_for_super_rights(selection, user)
        if rights_error:
            return templates.TemplateResponse(
                request=request,
                name="ship_selection_profile.html",
                context=_ship_selection_error_context(request, user, "EVEOSINT - Ship", rights_error),
            )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="ship_selection_profile.html",
        context=_ship_selection_context(request, user, selection),
    )


@router.get("/ship-category", response_class=HTMLResponse)
def ship_category_profile(request: Request, tier: str | None = Query(None), group: str | None = Query(None), size: str | None = Query(None), faction: str | None = Query(None)):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect

    try:
        selection = get_ship_selection_profile(tier=tier, group=group, size=size, faction=faction)
        selection, rights_error = _filter_ship_selection_for_super_rights(selection, user)
        if rights_error:
            return templates.TemplateResponse(
                request=request,
                name="ship_selection_profile.html",
                context=_ship_selection_error_context(request, user, "EVEOSINT - Ship Category", rights_error),
            )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="ship_selection_profile.html",
        context=_ship_selection_context(request, user, selection),
    )


@router.get("/api/ship-selection-ranking", response_class=HTMLResponse)
def ship_selection_ranking_fragment(
    request: Request,
    ranking_type: str = Query("alliance"),
    type_id: int | None = Query(None),
    tier: str | None = Query(None),
    group: str | None = Query(None),
    size: str | None = Query(None),
    faction: str | None = Query(None),
    page: int = Query(1),
    per_page: int = Query(10),
    offset: int | None = Query(None),
    q: str = Query(""),
    hide_npc_corps: bool = Query(True),
):
    user = require_login(request)

    error = None
    ranking = None
    selection = None
    try:
        selection = get_ship_selection_profile(type_id=type_id, tier=tier, group=group, size=size, faction=faction)
        selection, rights_error = _filter_ship_selection_for_super_rights(selection, user)
        if rights_error:
            error = rights_error
        else:
            ranking = get_ship_selection_ranking(
                selection.get("ship_ids", []),
                ranking_type=ranking_type,
                page=page,
                per_page=per_page,
                offset=offset,
                q=q,
                hide_npc_corps=hide_npc_corps,
            )
            error = ranking.get("error")
    except EntityError as exc:
        return _entity_route_error_response(request, exc)
    except Exception as exc:
        return _entity_route_error_response(request, exc, status_code=500)

    return templates.TemplateResponse(
        request=request,
        name="ship_selection_ranking_fragment.html",
        context={
            "selection": selection,
            "ranking": ranking,
            "ranking_error": error,
        },
    )


@router.get("/api/ship-analysis/search", response_class=JSONResponse)
def ship_analysis_search(
    request: Request,
    kind: str = Query(...),
    q: str = Query(""),
    limit: int = Query(15),
):
    user = require_login(request)
    if not user:
        return JSONResponse({"results": [], "error": "login_required"}, status_code=401)
    if not has_permission(user, "entities.view"):
        return JSONResponse({"results": [], "error": "permission_denied"}, status_code=403)

    try:
        results = search_ship_analysis_entities(
            kind,
            q,
            limit=limit,
            allow_super_titan=has_permission(user, "superintel.view"),
        )
        return JSONResponse({"results": results})
    except EntityError as exc:
        return _entity_route_error_response(request, exc)


@router.post("/api/ship-analysis")
async def ship_analysis_stream(request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse({"error": "login_required"}, status_code=401)
    if not has_permission(user, "entities.view"):
        return JSONResponse({"error": "permission_denied"}, status_code=403)

    try:
        payload = await request.json()
        normalized = normalize_ship_analysis_request(payload)
        normalized = enforce_ship_analysis_super_rights(
            normalized,
            allow_super_titan=has_permission(user, "superintel.view"),
        )
    except EntityError as exc:
        return _entity_route_error_response(request, exc)
    except Exception:
        return JSONResponse({"error": "ship_analysis_invalid:payload"}, status_code=400)

    def stream():
        for message in iter_ship_analysis_stream(normalized):
            yield json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/weapon/{type_id}", response_class=HTMLResponse)
def weapon_profile(request: Request, type_id: int):
    return entity_profile(request, "weapon", type_id)


@router.get("/skill/{type_id}", response_class=HTMLResponse)
def skill_profile(request: Request, type_id: int):
    return entity_profile(request, "skill", type_id)


@router.get("/commodity/{type_id}", response_class=HTMLResponse)
def commodity_profile(request: Request, type_id: int):
    return entity_profile(request, "commodity", type_id)


@router.get("/superintel/{entity_type}/{entity_id}")
def old_superintel_entity_profile_redirect(request: Request, entity_type: str, entity_id: int):
    if entity_type == "player":
        entity_type = "character"
    if entity_type not in {
        "character",
        "corporation",
        "alliance",
        "system",
        "constellation",
        "region",
        "ship",
        "weapon",
        "skill",
        "commodity",
    }:
        return RedirectResponse(url="/superintel", status_code=302)
    return RedirectResponse(url=f"/{entity_type}/{entity_id}", status_code=302)



def _dotlan_history_loading_response(request: Request, alliance_id: int, alliance_name: str):
    # Keep this page self-contained so the normal profile template does not need to
    # know anything about the one-time DOTLAN initialization workflow.
    return_url = str(request.url)
    return_url_js = json.dumps(return_url)
    alliance_name_js = json.dumps(str(alliance_name or "Alliance"))
    start_url_js = json.dumps(f"/api/alliance/{int(alliance_id)}/population-history-check")
    status_url_js = json.dumps(f"/api/alliance/{int(alliance_id)}/population-history-check/status")

    html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>EVEOSINT - DOTLAN history check</title>
  <style>
    html,body{{margin:0;min-height:100%;background:#08111f;color:#e6eef8;font-family:system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}}
    body{{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;box-sizing:border-box}}
    .card{{width:min(680px,100%);background:#101b2d;border:1px solid #263a55;border-radius:16px;padding:26px 28px;box-shadow:0 18px 60px rgba(0,0,0,.35)}}
    .row{{display:flex;align-items:center;gap:14px}}
    .spinner{{width:22px;height:22px;border-radius:50%;border:3px solid #29435f;border-top-color:#54c8ff;animation:spin .85s linear infinite;flex:0 0 auto}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    h1{{font-size:20px;margin:0 0 6px;color:#fff}}
    p{{margin:8px 0;color:#a9c3df;line-height:1.5}}
    #status{{margin-top:16px;padding:10px 12px;border-radius:9px;background:#0a1525;border:1px solid #20344e;color:#8ed8ff}}
    .muted{{font-size:13px;color:#7893af}}
  </style>
</head>
<body>
  <div class=\"card\">
    <div class=\"row\">
      <div class=\"spinner\" id=\"spinner\"></div>
      <div>
        <h1>Checking DOTLAN population history</h1>
        <p id=\"alliance-name\"></p>
      </div>
    </div>
    <p>A DOTLAN request has been started to verify and retrieve this alliance's historical population data.</p>
    <p class=\"muted\">Old alliances can contain gaps, so this check may take a little longer. The profile will open automatically when the verification finishes.</p>
    <div id=\"status\">Starting history check…</div>
  </div>
<script>
(function(){{
  const allianceName = {alliance_name_js};
  const startUrl = {start_url_js};
  const statusUrl = {status_url_js};
  const returnUrl = {return_url_js};
  const statusNode = document.getElementById('status');
  const spinner = document.getElementById('spinner');
  const startedAt = Date.now();
  document.getElementById('alliance-name').textContent = allianceName;

  function openProfile(){{
    const u = new URL(returnUrl, window.location.origin);
    u.searchParams.set('dotlan_history_checked', '1');
    window.location.replace(u.toString());
  }}

  async function poll(){{
    try {{
      const response = await fetch(statusUrl, {{credentials:'same-origin', cache:'no-store'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
      if (data.initialization_done) {{
        statusNode.textContent = 'DOTLAN history verified. Opening profile…';
        spinner.style.display = 'none';
        setTimeout(openProfile, 250);
        return;
      }}
      if (data.last_error && (Date.now() - startedAt) > 5000) {{
        statusNode.textContent = 'DOTLAN history check failed: ' + data.last_error + '. Opening profile…';
        spinner.style.display = 'none';
        setTimeout(openProfile, 1800);
        return;
      }}
      statusNode.textContent = 'DOTLAN history check in progress…';
      setTimeout(poll, 2500);
    }} catch (error) {{
      statusNode.textContent = 'History check status unavailable; retrying…';
      setTimeout(poll, 3500);
    }}
  }}

  fetch(startUrl, {{method:'POST', credentials:'same-origin', cache:'no-store'}})
    .then(async response => {{
      let data = {{}};
      try {{ data = await response.json(); }} catch (_) {{}}
      if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
      if (data.status === 'complete') {{ openProfile(); return; }}
      statusNode.textContent = 'DOTLAN history request started…';
      setTimeout(poll, 600);
    }})
    .catch(error => {{
      statusNode.textContent = 'Unable to start DOTLAN history check: ' + error.message + '. Opening profile…';
      spinner.style.display = 'none';
      setTimeout(openProfile, 1800);
    }});
}})();
</script>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200, headers={"Cache-Control": "no-store"})


def _parse_history_datetime_parameter(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_history_period") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get_history_period_kills(entity_type, entity_id, start_at, end_at=None):
    entity_id = int(entity_id)
    if entity_type not in {"character", "corporation"}:
        raise ValueError("invalid_history_entity_type")
    if end_at is not None and end_at <= start_at:
        raise ValueError("invalid_history_period")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    to_regclass('rawkm.killmails'),
                    to_regclass('rawkm.killmail_attackers'),
                    to_regclass('rawkm.killmail_import_days')
                """
            )
            rawkm_tables = cur.fetchone()
        if not (rawkm_tables and rawkm_tables[0] and rawkm_tables[1] and rawkm_tables[2]):
            return {
                "available": False,
                "reason": "unavailable",
                "kills_inflicted": None,
                "kills_suffered": None,
                "kills_inflicted_display": "—",
                "kills_suffered_display": "—",
            }

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(day), MAX(day)
                FROM rawkm.killmail_import_days
                WHERE status = 'success'
                """
            )
            coverage_row = cur.fetchone()

        coverage_min = coverage_row[0] if coverage_row else None
        coverage_max = coverage_row[1] if coverage_row else None
        if coverage_min is None or coverage_max is None:
            return {
                "available": False,
                "reason": "unavailable",
                "kills_inflicted": None,
                "kills_suffered": None,
                "kills_inflicted_display": "—",
                "kills_suffered_display": "—",
            }

        coverage_end = datetime.combine(
            coverage_max + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        if start_at.date() < coverage_min:
            return {
                "available": False,
                "reason": "outside_coverage",
                "coverage_from": coverage_min.isoformat(),
                "coverage_to": coverage_max.isoformat(),
                "kills_inflicted": None,
                "kills_suffered": None,
                "kills_inflicted_display": "—",
                "kills_suffered_display": "—",
            }
        if end_at is not None and end_at > coverage_end:
            return {
                "available": False,
                "reason": "outside_coverage",
                "coverage_from": coverage_min.isoformat(),
                "coverage_to": coverage_max.isoformat(),
                "kills_inflicted": None,
                "kills_suffered": None,
                "kills_inflicted_display": "—",
                "kills_suffered_display": "—",
            }

        end_bound = min(end_at, coverage_end) if end_at is not None else coverage_end
        victim_column = "victim_character_id" if entity_type == "character" else "victim_corporation_id"
        attacker_column = "character_id" if entity_type == "character" else "corporation_id"

        with conn.cursor() as cur:
            # One request handles one period only. Keep it below the usual 60s proxy timeout.
            cur.execute("SET LOCAL statement_timeout = '50000ms'")
            cur.execute(
                f"""
                SELECT
                    (
                        SELECT COUNT(*)::bigint
                        FROM rawkm.killmails km
                        WHERE km.{victim_column} = %s
                          AND km.killmail_time >= %s
                          AND km.killmail_time < %s
                    ) AS losses,
                    (
                        SELECT COUNT(DISTINCT ka.killmail_id)::bigint
                        FROM rawkm.killmail_attackers ka
                        WHERE ka.{attacker_column} = %s
                          AND ka.killmail_time >= %s
                          AND ka.killmail_time < %s
                    ) AS kills
                """,
                (
                    entity_id, start_at, end_bound,
                    entity_id, start_at, end_bound,
                ),
            )
            count_row = cur.fetchone()

    kills_suffered = int(count_row[0] or 0) if count_row else 0
    kills_inflicted = int(count_row[1] or 0) if count_row else 0
    return {
        "available": True,
        "reason": None,
        "coverage_from": coverage_min.isoformat(),
        "coverage_to": coverage_max.isoformat(),
        "kills_inflicted": kills_inflicted,
        "kills_suffered": kills_suffered,
        "kills_inflicted_display": _format_history_count(kills_inflicted),
        "kills_suffered_display": _format_history_count(kills_suffered),
    }


def _format_history_count(value):
    if value is None:
        return "—"
    return f"{int(value):,}"


def _merge_corporation_affiliation_periods(history_rows):
    """Merge touching/overlapping periods with the same alliance_id.

    `is_deleted` marks a historical/deleted alliance record; it must never be
    interpreted as "No alliance".  "No alliance" is only alliance_id IS NULL.
    """
    ordered = sorted(history_rows, key=lambda row: (row[2], row[0]))
    merged = []

    for record_id, alliance_id, start_at, end_at, alliance_name, alliance_ticker, alliance_deleted in ordered:
        if merged:
            previous = merged[-1]
            previous_end = previous["end_at"]
            same_alliance = previous["alliance_id"] == alliance_id
            touches_or_overlaps = (
                previous_end is not None
                and start_at <= previous_end
            )

            if same_alliance and touches_or_overlaps:
                if end_at is None:
                    previous["end_at"] = None
                elif previous_end is not None and end_at > previous_end:
                    previous["end_at"] = end_at

                # Prefer a resolved alliance label if one of the duplicate rows has it.
                if previous["alliance_name"] == "Unknown alliance" and alliance_name:
                    previous["alliance_name"] = alliance_name
                if not previous["alliance_ticker"] and alliance_ticker:
                    previous["alliance_ticker"] = alliance_ticker
                previous["alliance_deleted"] = bool(previous["alliance_deleted"] or alliance_deleted)
                continue

        merged.append({
            "record_id": record_id,
            "alliance_id": alliance_id,
            "start_at": start_at,
            "end_at": end_at,
            "alliance_name": alliance_name,
            "alliance_ticker": alliance_ticker,
            "alliance_deleted": bool(alliance_deleted),
        })

    return [
        (
            row["record_id"],
            row["alliance_id"],
            row["start_at"],
            row["end_at"],
            row["alliance_name"],
            row["alliance_ticker"],
            row["alliance_deleted"],
        )
        for row in sorted(merged, key=lambda row: (row["start_at"], row["record_id"]), reverse=True)
    ]


def _get_corporation_affiliation_history(corporation_id):
    corporation_id = int(corporation_id)
    rows = []
    open_end = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            cur.execute(
                """
                SELECT
                    h.record_id,
                    h.alliance_id,
                    h.start_date,
                    h.end_date,
                    COALESCE(NULLIF(a.name, ''), 'Unknown alliance') AS alliance_name,
                    a.ticker,
                    COALESCE(h.is_deleted, FALSE) AS alliance_deleted
                FROM entities.corporation_alliance_history h
                LEFT JOIN entities.alliances a
                  ON a.alliance_id = h.alliance_id
                WHERE h.corporation_id = %s
                ORDER BY h.start_date DESC, h.record_id DESC
                """,
                (corporation_id,),
            )
            history_rows = cur.fetchall()

        history_rows = _merge_corporation_affiliation_periods(history_rows)
        coalition_resolver, coalition_change_days = _coalition_history_resolver(conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date_founded, COALESCE(is_deleted, FALSE)
                FROM entities.corporations
                WHERE corporation_id = %s
                """,
                (corporation_id,),
            )
            corporation_row = cur.fetchone()

        corporation_date_founded = corporation_row[0] if corporation_row else None
        corporation_closed = bool(corporation_row[1]) if corporation_row else False

        population_rows = []
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('population.corporation_daily')")
            population_table = cur.fetchone()[0]
            if population_table:
                cur.execute(
                    """
                    SELECT snapshot_date, member_count
                    FROM population.corporation_daily
                    WHERE corporation_id = %s
                    ORDER BY snapshot_date ASC
                    """,
                    (corporation_id,),
                )
                population_rows = [
                    (snapshot_date, int(member_count) if member_count is not None else None)
                    for snapshot_date, member_count in cur.fetchall()
                ]

        population_first_day = population_rows[0][0] if population_rows else None
        population_last_day = population_rows[-1][0] if population_rows else None

        base_segments = []
        for record_id, alliance_id, start_at, end_at, alliance_name, alliance_ticker, alliance_deleted in history_rows:
            normalized_start = _history_datetime(start_at)
            normalized_end = _history_datetime(end_at) or open_end
            base_segments.append({
                "record_id": int(record_id),
                "corporation_id": corporation_id,
                "alliance_id": int(alliance_id) if alliance_id is not None else None,
                "alliance_name": alliance_name or "Unknown alliance",
                "alliance_ticker": alliance_ticker,
                "alliance_deleted": bool(alliance_deleted),
                "alliance_membership_start": normalized_start,
                "start_at": normalized_start,
                "end_at": normalized_end,
            })

        segmented = _split_affiliation_segments_by_coalition(
            base_segments,
            coalition_resolver,
            coalition_change_days,
        )
        segmented = _merge_history_segments(
            segmented,
            ("corporation_id", "alliance_id"),
        )

        for segment in sorted(segmented, key=lambda row: (row["start_at"], row["record_id"]), reverse=True):
            start_at = segment["start_at"]
            end_at_effective = segment["end_at"]
            current = end_at_effective >= open_end
            end_at = None if current else end_at_effective
            start_day = start_at.date()
            end_day = end_at.date() if end_at else None

            period_population_rows = [
                (snapshot_date, member_count)
                for snapshot_date, member_count in population_rows
                if snapshot_date >= start_day
                and (end_day is None or snapshot_date <= end_day)
                and member_count is not None
            ]
            period_population = [member_count for _, member_count in period_population_rows]

            population_start_covered = bool(
                population_first_day is not None
                and population_first_day <= start_day
                and population_last_day is not None
                and population_last_day >= start_day
            )
            population_end_covered = bool(
                end_day is None
                or (
                    population_last_day is not None
                    and population_last_day >= end_day
                )
            )
            population_period_covered = population_start_covered and population_end_covered

            members_entry = (
                period_population_rows[0][1]
                if population_start_covered and period_population_rows
                else None
            )
            members_exit = period_population_rows[-1][1] if period_population_rows else None
            members_min = min(period_population) if population_period_covered and period_population else None
            members_max = max(period_population) if population_period_covered and period_population else None

            rows.append({
                "record_id": int(segment["record_id"]),
                "alliance_id": int(segment["alliance_id"]) if segment.get("alliance_id") is not None else None,
                "alliance_name": segment.get("alliance_name") or "Unknown alliance",
                "alliance_ticker": segment.get("alliance_ticker"),
                "alliance_deleted": bool(segment.get("alliance_deleted")),
                "coalitions": segment.get("coalitions") or [],
                "coalition_key": list(segment.get("coalition_key") or ()),
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat() if end_day else None,
                "current": current,
                "alliance_membership_start": (
                    segment["alliance_membership_start"].date().isoformat()
                    if segment.get("alliance_membership_start")
                    else None
                ),
                "members_entry": members_entry,
                "members_exit": members_exit,
                "members_min": members_min,
                "members_max": members_max,
                "kills_inflicted": None,
                "kills_suffered": None,
                "kill_start_at": start_at.isoformat(),
                "kill_end_at": end_at.isoformat() if end_at else None,
                "members_entry_display": _format_history_count(members_entry),
                "members_exit_display": _format_history_count(members_exit),
                "members_min_display": _format_history_count(members_min),
                "members_max_display": _format_history_count(members_max),
                "kills_inflicted_display": "—",
                "kills_suffered_display": "—",
            })

    current_period = next((row for row in rows if row.get("current")), None)

    if corporation_closed:
        current_status = {
            "kind": "closed",
            "since": None,
            "alliance_id": None,
            "alliance_name": None,
            "alliance_ticker": None,
            "coalitions": [],
        }
    elif current_period and current_period.get("alliance_id") is not None:
        current_status = {
            "kind": "alliance",
            "since": current_period.get("alliance_membership_start") or current_period.get("start_date"),
            "alliance_id": current_period.get("alliance_id"),
            "alliance_name": current_period.get("alliance_name"),
            "alliance_ticker": current_period.get("alliance_ticker"),
            "coalitions": current_period.get("coalitions") or [],
        }
    elif current_period:
        current_status = {
            "kind": "no_alliance",
            "since": current_period.get("alliance_membership_start") or current_period.get("start_date"),
            "alliance_id": None,
            "alliance_name": None,
            "alliance_ticker": None,
            "coalitions": current_period.get("coalitions") or [],
        }
    else:
        current_status = {
            "kind": "unknown",
            "since": None,
            "alliance_id": None,
            "alliance_name": None,
            "alliance_ticker": None,
            "coalitions": [],
        }

    return {
        "corporation_id": corporation_id,
        "created_date": corporation_date_founded.date().isoformat() if corporation_date_founded else None,
        "current_status": current_status,
        "rows": rows,
        "kill_stats_progressive": True,
    }



def _history_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _merge_character_corporation_periods(history_rows):
    """Merge duplicate/touching corporation-history rows for the same corporation.

    `is_deleted` is preserved as a closed/deleted corporation flag; it is not a
    reason to remove a historical membership row.
    """
    ordered = sorted(history_rows, key=lambda row: (row[2], row[0]))
    merged = []

    for record_id, corporation_id, start_at, end_at, corporation_name, corporation_ticker, corporation_deleted in ordered:
        start_at = _history_datetime(start_at)
        end_at = _history_datetime(end_at)
        if merged:
            previous = merged[-1]
            previous_end = previous["end_at"]
            same_corporation = previous["corporation_id"] == corporation_id
            touches_or_overlaps = previous_end is None or start_at <= previous_end

            if same_corporation and touches_or_overlaps:
                if previous_end is None or end_at is None:
                    previous["end_at"] = None
                elif end_at > previous_end:
                    previous["end_at"] = end_at
                if previous["corporation_name"] == "Unknown corporation" and corporation_name:
                    previous["corporation_name"] = corporation_name
                if not previous["corporation_ticker"] and corporation_ticker:
                    previous["corporation_ticker"] = corporation_ticker
                previous["corporation_deleted"] = bool(previous["corporation_deleted"] or corporation_deleted)
                continue

        merged.append({
            "record_id": int(record_id),
            "corporation_id": int(corporation_id) if corporation_id is not None else None,
            "start_at": start_at,
            "end_at": end_at,
            "corporation_name": corporation_name or "Unknown corporation",
            "corporation_ticker": corporation_ticker,
            "corporation_deleted": bool(corporation_deleted),
        })

    return merged


def _get_character_affiliation_history(character_id):
    character_id = int(character_id)
    rows = []
    open_end = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            cur.execute(
                """
                SELECT birthday, COALESCE(is_deleted, FALSE)
                FROM entities.characters
                WHERE character_id = %s
                LIMIT 1
                """,
                (character_id,),
            )
            character_row = cur.fetchone()

        birthday = character_row[0] if character_row else None
        character_closed = bool(character_row[1]) if character_row else False

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            cur.execute(
                """
                SELECT
                    cch.record_id,
                    cch.corporation_id,
                    cch.start_date,
                    cch.end_date,
                    COALESCE(NULLIF(c.name, ''), 'Unknown corporation') AS corporation_name,
                    c.ticker,
                    (COALESCE(cch.is_deleted, FALSE) OR COALESCE(c.is_deleted, FALSE)) AS corporation_deleted
                FROM entities.character_corporation_history cch
                LEFT JOIN entities.corporations c
                  ON c.corporation_id = cch.corporation_id
                WHERE cch.character_id = %s
                ORDER BY cch.start_date ASC, cch.record_id ASC
                """,
                (character_id,),
            )
            corporation_history = _merge_character_corporation_periods(cur.fetchall())

        corporation_ids = sorted({
            int(row["corporation_id"])
            for row in corporation_history
            if row.get("corporation_id") is not None
        })

        alliance_history_by_corporation = {}
        if corporation_ids:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '15000ms'")
                cur.execute(
                    """
                    SELECT
                        h.corporation_id,
                        h.record_id,
                        h.alliance_id,
                        h.start_date,
                        h.end_date,
                        COALESCE(NULLIF(a.name, ''), 'Unknown alliance') AS alliance_name,
                        a.ticker,
                        COALESCE(h.is_deleted, FALSE) AS alliance_deleted
                    FROM entities.corporation_alliance_history h
                    LEFT JOIN entities.alliances a
                      ON a.alliance_id = h.alliance_id
                    WHERE h.corporation_id = ANY(%s)
                    ORDER BY h.corporation_id, h.start_date ASC, h.record_id ASC
                    """,
                    (corporation_ids,),
                )
                alliance_rows = cur.fetchall()

            raw_by_corporation = {}
            for corporation_id_value, record_id, alliance_id, start_at, end_at, alliance_name, alliance_ticker, alliance_deleted in alliance_rows:
                raw_by_corporation.setdefault(int(corporation_id_value), []).append((
                    record_id,
                    alliance_id,
                    _history_datetime(start_at),
                    _history_datetime(end_at),
                    alliance_name,
                    alliance_ticker,
                    alliance_deleted,
                ))

            for corporation_id_value, raw_rows in raw_by_corporation.items():
                merged = _merge_corporation_affiliation_periods(raw_rows)
                alliance_history_by_corporation[corporation_id_value] = [
                    {
                        "record_id": int(record_id),
                        "alliance_id": int(alliance_id) if alliance_id is not None else None,
                        "start_at": _history_datetime(start_at),
                        "end_at": _history_datetime(end_at),
                        "alliance_name": alliance_name or "Unknown alliance",
                        "alliance_ticker": alliance_ticker,
                        "alliance_deleted": bool(alliance_deleted),
                    }
                    for record_id, alliance_id, start_at, end_at, alliance_name, alliance_ticker, alliance_deleted in sorted(
                        merged,
                        key=lambda row: (row[2], row[0]),
                    )
                ]

        coalition_resolver, coalition_change_days = _coalition_history_resolver(conn)

        # Split each character corporation spell by the corporation's alliance spells.
        segments = []
        synthetic_record_id = -1
        for corporation_period in corporation_history:
            corporation_id_value = corporation_period.get("corporation_id")
            corporation_start = corporation_period["start_at"]
            corporation_end = corporation_period["end_at"]
            corporation_end_effective = corporation_end or open_end
            cursor = corporation_start

            alliance_periods = alliance_history_by_corporation.get(corporation_id_value, [])
            for alliance_period in alliance_periods:
                alliance_start = alliance_period["start_at"]
                alliance_end_effective = alliance_period["end_at"] or open_end
                if alliance_start >= corporation_end_effective or alliance_end_effective <= corporation_start:
                    continue

                overlap_start = max(corporation_start, alliance_start)
                overlap_end = min(corporation_end_effective, alliance_end_effective)
                if overlap_end <= overlap_start:
                    continue

                if overlap_start > cursor:
                    segments.append({
                        "record_id": synthetic_record_id,
                        "corporation_id": corporation_id_value,
                        "corporation_name": corporation_period["corporation_name"],
                        "corporation_ticker": corporation_period["corporation_ticker"],
                        "corporation_deleted": corporation_period["corporation_deleted"],
                        "corporation_membership_start": corporation_start,
                        "alliance_id": None,
                        "alliance_name": None,
                        "alliance_ticker": None,
                        "alliance_deleted": False,
                        "start_at": cursor,
                        "end_at": overlap_start,
                    })
                    synthetic_record_id -= 1

                segment_start = max(cursor, overlap_start)
                if overlap_end > segment_start:
                    segments.append({
                        "record_id": int(alliance_period["record_id"]),
                        "corporation_id": corporation_id_value,
                        "corporation_name": corporation_period["corporation_name"],
                        "corporation_ticker": corporation_period["corporation_ticker"],
                        "corporation_deleted": corporation_period["corporation_deleted"],
                        "corporation_membership_start": corporation_start,
                        "alliance_id": alliance_period["alliance_id"],
                        "alliance_name": alliance_period["alliance_name"],
                        "alliance_ticker": alliance_period["alliance_ticker"],
                        "alliance_deleted": alliance_period["alliance_deleted"],
                        "start_at": segment_start,
                        "end_at": overlap_end,
                    })
                    cursor = max(cursor, overlap_end)

                if cursor >= corporation_end_effective:
                    break

            if cursor < corporation_end_effective:
                segments.append({
                    "record_id": synthetic_record_id,
                    "corporation_id": corporation_id_value,
                    "corporation_name": corporation_period["corporation_name"],
                    "corporation_ticker": corporation_period["corporation_ticker"],
                    "corporation_deleted": corporation_period["corporation_deleted"],
                    "corporation_membership_start": corporation_start,
                    "alliance_id": None,
                    "alliance_name": None,
                    "alliance_ticker": None,
                    "alliance_deleted": False,
                    "start_at": cursor,
                    "end_at": corporation_end_effective,
                })
                synthetic_record_id -= 1

        # Coalition is part of the historical key too: a coalition change
        # creates a new row even when corporation and alliance stay unchanged.
        segments = _split_affiliation_segments_by_coalition(
            segments,
            coalition_resolver,
            coalition_change_days,
        )

        # Merge only identical touching (corporation, alliance, coalition) keys.
        merged_segments = []
        for segment in sorted(segments, key=lambda row: (row["start_at"], row["record_id"])):
            if merged_segments:
                previous = merged_segments[-1]
                same_pair = (
                    previous["corporation_id"] == segment["corporation_id"]
                    and previous["alliance_id"] == segment["alliance_id"]
                    and tuple(previous.get("coalition_key") or ())
                        == tuple(segment.get("coalition_key") or ())
                )
                if same_pair and previous["end_at"] >= segment["start_at"]:
                    previous["end_at"] = max(previous["end_at"], segment["end_at"])
                    previous["corporation_deleted"] = bool(previous["corporation_deleted"] or segment["corporation_deleted"])
                    previous["alliance_deleted"] = bool(previous["alliance_deleted"] or segment["alliance_deleted"])
                    continue
            merged_segments.append(segment.copy())


        chronological_segments = sorted(merged_segments, key=lambda row: (row["start_at"], row["record_id"]))
        current_segment = next((segment for segment in reversed(chronological_segments) if segment["end_at"] >= open_end), None)
        current_alliance_since_at = current_segment["start_at"] if current_segment else None
        if current_segment is not None:
            current_index = chronological_segments.index(current_segment)
            wanted_alliance_id = current_segment.get("alliance_id")
            sequence_start = current_segment["start_at"]
            for idx in range(current_index - 1, -1, -1):
                previous = chronological_segments[idx]
                if previous.get("alliance_id") != wanted_alliance_id:
                    break
                if previous["end_at"] < sequence_start:
                    break
                sequence_start = previous["start_at"]
            current_alliance_since_at = sequence_start

        for segment in sorted(merged_segments, key=lambda row: (row["start_at"], row["record_id"]), reverse=True):
            start_at = segment["start_at"]
            end_at_effective = segment["end_at"]
            current = end_at_effective >= open_end
            end_at = None if current else end_at_effective
            start_day = start_at.date()
            end_day = end_at.date() if end_at else None

            rows.append({
                "record_id": int(segment["record_id"]),
                "corporation_id": int(segment["corporation_id"]) if segment.get("corporation_id") is not None else None,
                "corporation_name": segment.get("corporation_name") or "Unknown corporation",
                "corporation_ticker": segment.get("corporation_ticker"),
                "corporation_deleted": bool(segment.get("corporation_deleted")),
                "alliance_id": int(segment["alliance_id"]) if segment.get("alliance_id") is not None else None,
                "alliance_name": segment.get("alliance_name") or "Unknown alliance",
                "alliance_ticker": segment.get("alliance_ticker"),
                "alliance_deleted": bool(segment.get("alliance_deleted")),
                "coalitions": segment.get("coalitions") or [],
                "coalition_key": list(segment.get("coalition_key") or ()),
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat() if end_day else None,
                "current": current,
                "corporation_membership_start": segment["corporation_membership_start"].date().isoformat(),
                "kills_inflicted": None,
                "kills_suffered": None,
                "kill_start_at": start_at.isoformat(),
                "kill_end_at": end_at.isoformat() if end_at else None,
                "kills_inflicted_display": "—",
                "kills_suffered_display": "—",
            })

    current_period = next((row for row in rows if row.get("current")), None)
    current_corporation = None
    current_alliance = None
    no_alliance_since = None

    if current_period and current_period.get("corporation_id") is not None:
        current_corporation = {
            "corporation_id": current_period["corporation_id"],
            "corporation_name": current_period.get("corporation_name"),
            "corporation_ticker": current_period.get("corporation_ticker"),
            "since": current_period.get("corporation_membership_start"),
        }

        alliance_since = current_alliance_since_at.date().isoformat() if current_alliance_since_at else current_period.get("start_date")
        if current_period.get("alliance_id") is not None:
            current_alliance = {
                "alliance_id": current_period["alliance_id"],
                "alliance_name": current_period.get("alliance_name"),
                "alliance_ticker": current_period.get("alliance_ticker"),
                "since": alliance_since,
            }
        else:
            no_alliance_since = alliance_since

    return {
        "character_id": character_id,
        "birth_date": birthday.date().isoformat() if birthday else None,
        "closed": character_closed,
        "current_corporation": current_corporation,
        "current_alliance": current_alliance,
        "current_coalitions": current_period.get("coalitions") if current_period else [],
        "no_alliance_since": no_alliance_since,
        "rows": rows,
        "kill_stats_progressive": True,
    }


def entity_profile(request: Request, entity_type: str, entity_id: int):
    timing_started = perf_counter()
    user = require_login(request)

    requested_tab = request.query_params.get("tab", "killboard")
    allowed_tabs = {"killboard", "pilotable-ships"}
    if entity_type in {"alliance", "corporation"}:
        allowed_tabs.add("population")
    if entity_type in {"character", "corporation"}:
        allowed_tabs.add("history")
    if requested_tab not in allowed_tabs:
        requested_tab = "killboard"

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Entity Profile",
        active_module="profiles",
        active_menu_key=requested_tab,
    )

    error = None
    profile = None
    killmail_ship_options = []
    killmail_ship_options_error = None
    killmail_page = None
    alliance_population_summary = None
    corporation_population_summary = None
    corporation_history = None

    try:
        profile_started = perf_counter()
        profile = get_entity_profile(entity_type, entity_id)
        profile_ms = round((perf_counter() - profile_started) * 1000, 1)
    except EntityError as exc:
        profile_ms = round((perf_counter() - profile_started) * 1000, 1) if "profile_started" in locals() else None
        return _entity_route_error_response(request, exc)

    if profile and profile.get("entity_type") == "alliance":
        try:
            alliance_population_summary = get_alliance_population_summary(profile["entity_id"])
        except Exception:
            logger.exception(
                "Alliance population summary load failed for alliance_id=%s",
                profile.get("entity_id"),
            )

    if profile and profile.get("entity_type") == "corporation":
        try:
            corporation_population_summary = get_corporation_population_summary(profile["entity_id"])
        except Exception:
            logger.exception(
                "Corporation population summary load failed for corporation_id=%s",
                profile.get("entity_id"),
            )

    if (
        profile
        and requested_tab == "killboard"
        and profile.get("entity_type") in {"system", "constellation", "region"}
    ):
        try:
            killmail_page = get_location_killmails_page(
                profile["entity_type"],
                profile["entity_id"],
                page=request.query_params.get("page", "1"),
                per_page=100,
                mode=request.query_params.get("mode", "api"),
                filters=_killmail_filters_from_request(request),
            )
        except EntityError as exc:
            return _entity_route_error_response(request, exc)

    profile_menu = []
    if profile and profile.get("entity_type") in {"character", "corporation", "alliance"}:
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
        if profile.get("entity_type") in {"character", "corporation"}:
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

    location_previews = {}
    if profile and profile.get("entity_type") in {"system", "constellation", "region"}:
        preview_targets = [(profile["entity_type"], profile["entity_id"])]
        if profile.get("current_constellation"):
            preview_targets.append(("constellation", profile["current_constellation"]["entity_id"]))
        if profile.get("current_region"):
            preview_targets.append(("region", profile["current_region"]["entity_id"]))
        for preview_type, preview_id in preview_targets:
            key = f"{preview_type}:{int(preview_id)}"
            if key in location_previews:
                continue
            try:
                location_previews[key] = get_location_preview(preview_type, preview_id)
            except Exception:
                logger.exception("Location preview failed for %s %s", preview_type, preview_id)

    context.update({
        "error": error,
        "profile": profile,
        "profile_tab": requested_tab,
        "alliance_population_summary": alliance_population_summary,
        "corporation_population_summary": corporation_population_summary,
        "corporation_history": corporation_history,
        "killmail_ship_options": killmail_ship_options,
        "killmail_ship_options_error": killmail_ship_options_error,
        "killmail_page": killmail_page,
        "killmail_mode": (killmail_page or {}).get("killmail_mode", request.query_params.get("mode", "api")),
        "system_id": profile.get("entity_id") if profile and profile.get("entity_type") == "system" else None,
        "killmail_base_url": f"/{profile['entity_type']}/{profile['entity_id']}" if profile and profile.get("entity_type") in {"system", "constellation", "region"} else None,
        "location_previews": location_previews,
        "context_title": "Profile",
        "context_menu": profile_menu,
    })

    render_started = perf_counter()
    response = templates.TemplateResponse(
        request=request,
        name="entity_profile.html",
        context=context,
    )
    render_ms = round((perf_counter() - render_started) * 1000, 1)
    total_ms = round((perf_counter() - timing_started) * 1000, 1)
    print(
        f"ENTITY_PROFILE_TIMING route entity_type={entity_type} entity_id={entity_id} "
        f"profile_ms={profile_ms} render_ms={render_ms} total_ms={total_ms} error={error}",
        flush=True,
    )
    return response
