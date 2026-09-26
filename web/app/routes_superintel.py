import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from .superintel import (
    SuperIntelError,
    get_dashboard,
    get_detailed_dashboard,
    get_monthly_analysis,
    get_monthly_analysis_excel,
    list_daily_status,
    latest_complete_daily_day,
)
from .superintel_evolution import (
    build_evolution_csv,
    build_evolution_data,
    get_evolution_page_context,
)


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/superintel", response_class=HTMLResponse)
def superintel_dashboard(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "superintel.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - SuperINTEL",
        active_module="superintel",
        active_menu_key="superintel.dashboard",
    )

    error = None
    status = None
    dashboard = None

    try:
        entity_type = request.query_params.get("view", "alliance")
        full_ranking = request.query_params.get("more") == "1"
        ranking_sort = request.query_params.get("sort", "score")
        status = list_daily_status(entity_type=entity_type)
        display_day = latest_complete_daily_day(entity_type=entity_type)
        if display_day is None:
            raise SuperIntelError("superintel_no_complete_daily_snapshot")
        dashboard = get_dashboard(
            today=display_day,
            entity_type=entity_type,
            full_ranking=full_ranking,
            ranking_sort=ranking_sort,
        )
    except SuperIntelError:
        logger.exception("SuperINTEL dashboard failed")
        return Response(status_code=500)

    context.update({
        "error": error,
        "status": status,
        "dashboard": dashboard,
    })

    return templates.TemplateResponse(
        request=request,
        name="superintel.html",
        context=context,
    )


@router.get("/superintel/detailed", response_class=HTMLResponse)
def superintel_detailed_dashboard(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "superintel.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - SuperINTEL Detailed Dashboard",
        active_module="superintel",
        active_menu_key="superintel.detailed",
    )

    error = None
    status = None
    detailed = None

    try:
        entity_type = request.query_params.get("view", "alliance")
        movement_period = request.query_params.get("period", "7d")
        q = request.query_params.get("q", "")
        ranking_sort = request.query_params.get("sort", "score")
        page = request.query_params.get("page", "1")
        page_size = request.query_params.get("page_size", "50")
        analysis_view = request.query_params.get("analysis_view")
        analysis_movement = request.query_params.get("analysis_movement", "all")
        analysis_asset = request.query_params.get("analysis_asset", "all")
        analysis_q = request.query_params.get("analysis_q", "")
        analysis_page = request.query_params.get("analysis_page", "1")
        analysis_page_size = request.query_params.get("analysis_page_size", "50")

        status = list_daily_status(entity_type=entity_type)
        display_day = latest_complete_daily_day(entity_type=entity_type)
        if display_day is None:
            raise SuperIntelError("superintel_no_complete_daily_snapshot")
        detailed = get_detailed_dashboard(
            today=display_day,
            entity_type=entity_type,
            movement_period=movement_period,
            q=q,
            ranking_sort=ranking_sort,
            page=page,
            page_size=page_size,
            analysis_view=analysis_view,
            analysis_movement=analysis_movement,
            analysis_asset=analysis_asset,
            analysis_q=analysis_q,
            analysis_page=analysis_page,
            analysis_page_size=analysis_page_size,
        )
    except SuperIntelError:
        logger.exception("SuperINTEL detailed dashboard failed")
        return Response(status_code=500)

    context.update({
        "error": error,
        "status": status,
        "detailed": detailed,
    })

    return templates.TemplateResponse(
        request=request,
        name="superintel_detailed.html",
        context=context,
    )


@router.get("/superintel/evolution", response_class=HTMLResponse)
def superintel_evolution(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "superintel.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - SuperINTEL Evolution",
        active_module="superintel",
        # Evolution is a Detailed-dashboard companion page.
        active_menu_key="superintel.detailed",
    )

    error = None
    evolution = None

    try:
        evolution = get_evolution_page_context()
    except SuperIntelError:
        logger.exception("SuperINTEL evolution page failed")
        return Response(status_code=500)

    context.update({
        "error": error,
        "evolution": evolution,
    })

    return templates.TemplateResponse(
        request=request,
        name="superintel_evolution.html",
        context=context,
    )


@router.post("/api/superintel/evolution/data", response_class=JSONResponse)
async def superintel_evolution_data(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "superintel.view")
    if redirect:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    try:
        payload = await request.json()
        data = build_evolution_data(payload)
        return JSONResponse(data)
    except SuperIntelError:
        logger.exception("SuperINTEL evolution data validation failed")
        return Response(status_code=400)
    except Exception:
        logger.exception("SuperINTEL evolution data failed")
        return JSONResponse({"error": "evolution_failed"}, status_code=500)


@router.post("/superintel/evolution/download")
async def superintel_evolution_download(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "superintel.view")
    if redirect:
        return redirect

    try:
        payload = await request.json()
        filename, csv_payload = build_evolution_csv(payload)
    except SuperIntelError:
        logger.exception("SuperINTEL evolution download failed")
        return Response(status_code=400)
    except Exception:
        logger.exception("SuperINTEL evolution download failed")
        return Response(
            "evolution_failed",
            status_code=500,
            media_type="text/plain",
        )

    return Response(
        csv_payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/superintel/monthly-analysis", response_class=HTMLResponse)
def superintel_monthly_analysis(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "superintel.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - SuperINTEL Monthly Analysis",
        active_module="superintel",
        active_menu_key="superintel.monthly_analysis",
    )

    error = None
    analysis = None

    try:
        period = request.query_params.get("period")
        reason = request.query_params.get("reason", "all")
        view = request.query_params.get("view", "pilot")
        q = request.query_params.get("q", "")
        sort = request.query_params.get("sort", "sort_delta")
        direction = request.query_params.get("dir", "desc")
        page = request.query_params.get("page", "1")
        page_size = request.query_params.get("page_size", "50")
        filters = {
            key[2:]: value
            for key, value in request.query_params.items()
            if key.startswith("f_")
        }
        analysis = get_monthly_analysis(
            period=period,
            reason=reason,
            view=view,
            q=q,
            filters=filters,
            sort=sort,
            direction=direction,
            page=page,
            page_size=page_size,
        )
    except SuperIntelError:
        logger.exception("SuperINTEL monthly analysis page failed")
        return Response(status_code=500)

    context.update({
        "error": error,
        "analysis": analysis,
    })

    return templates.TemplateResponse(
        request=request,
        name="superintel_monthly.html",
        context=context,
    )


@router.get("/superintel/monthly-analysis/download")
def superintel_monthly_analysis_download(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "superintel.view")
    if redirect:
        return redirect

    try:
        period = request.query_params.get("period")
        filename, payload = get_monthly_analysis_excel(period=period)
    except SuperIntelError:
        logger.exception("SuperINTEL monthly analysis download failed")
        return Response(status_code=500)

    return Response(
        payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/super")
def old_reports_super_redirect(request: Request):
    return RedirectResponse(url="/superintel", status_code=302)
