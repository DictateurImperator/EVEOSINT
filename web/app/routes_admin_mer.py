from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .audit import audit_log
from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from .mer import analyze_mer_archives, download_mer_archives, enrich_mer_entity_ids, get_mer_catalog_summary, import_mer_kill_dumps, list_mer_catalog, match_mer_killmails, reset_mer_downloads, scan_all_mer

router = APIRouter()


@router.get("/admin/mer", response_class=HTMLResponse)
def admin_mer(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin MER",
        active_module="admin",
        active_menu_key="admin.mer",
    )
    context.update({
        "mer_rows": list_mer_catalog(),
        "mer_summary": get_mer_catalog_summary(),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
        "scan_months": request.query_params.get("scan_months"),
        "scan_articles": request.query_params.get("scan_articles"),
        "scan_zips": request.query_params.get("scan_zips"),
        "download_started": request.query_params.get("download_started"),
        "download_month": request.query_params.get("download_month"),
        "analyze_started": request.query_params.get("analyze_started"),
        "analyze_month": request.query_params.get("analyze_month"),
        "kill_import_started": request.query_params.get("kill_import_started"),
        "entity_enrich_started": request.query_params.get("entity_enrich_started"),
        "kill_match_started": request.query_params.get("kill_match_started"),
        "reset_done": request.query_params.get("reset_done"),
        "reset_archives": request.query_params.get("reset_archives"),
        "reset_kills": request.query_params.get("reset_kills"),
    })

    return templates.TemplateResponse(
        request=request,
        name="admin_mer.html",
        context=context,
    )


@router.post("/admin/mer/scan")
def admin_mer_scan(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    try:
        stats = scan_all_mer()

        audit_log(
            request,
            "admin_mer_scan",
            user_id=user["id"],
            username=user["username"],
            target_type="mer",
            target_id="catalog",
            details=(
                f"months={stats['months']}; "
                f"articles={stats['articles']}; "
                f"zips={stats['zips']}; "
                f"ok={stats['ok']}; "
                f"errors={stats['errors']}"
            ),
        )

        return RedirectResponse(
            url=(
                "/admin/mer?success=scan_done"
                f"&scan_months={stats['months']}"
                f"&scan_articles={stats['articles']}"
                f"&scan_zips={stats['zips']}"
            ),
            status_code=302,
        )

    except Exception as exc:
        audit_log(
            request,
            "admin_mer_scan_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="mer",
            target_id="catalog",
            details=str(exc),
        )

        return RedirectResponse(
            url="/admin/mer?error=scan_failed",
            status_code=302,
        )



@router.post("/admin/mer/reset-downloads")
def admin_mer_reset_downloads(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    stats = reset_mer_downloads()

    if stats["busy"]:
        return RedirectResponse(
            url="/admin/mer?error=mer_busy",
            status_code=302,
        )

    audit_log(
        request,
        "admin_mer_reset_downloads",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id="local_downloads",
        details=(
            f"archives={stats['deleted_archives']}; "
            f"kill_dumps={stats['deleted_kill_dumps']}"
        ),
    )

    return RedirectResponse(
        url=(
            "/admin/mer?reset_done=1"
            f"&reset_archives={stats['deleted_archives']}"
            f"&reset_kills={stats['deleted_kill_dumps']}"
        ),
        status_code=302,
    )



@router.post("/admin/mer/download")
def admin_mer_download_all(
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    audit_log(
        request,
        "admin_mer_download_all",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id="archives",
        details="Téléchargement des archives MER manquantes lancé.",
    )

    background_tasks.add_task(download_mer_archives)

    return RedirectResponse(
        url="/admin/mer?download_started=all",
        status_code=302,
    )


@router.post("/admin/mer/{month}/download")
def admin_mer_download_one(
    month: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    try:
        parsed_month = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError:
        return RedirectResponse(
            url="/admin/mer?error=invalid_month",
            status_code=302,
        )

    audit_log(
        request,
        "admin_mer_download_one",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id=month,
        details=f"Téléchargement MER {month} lancé.",
    )

    background_tasks.add_task(
        download_mer_archives,
        parsed_month,
    )

    return RedirectResponse(
        url=f"/admin/mer?download_started=one&download_month={month}",
        status_code=302,
    )



@router.post("/admin/mer/analyze")
def admin_mer_analyze_all(
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    audit_log(
        request,
        "admin_mer_analyze_all",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id="archives",
        details="Analyse des dumps MER locaux lancée.",
    )

    background_tasks.add_task(analyze_mer_archives)

    return RedirectResponse(
        url="/admin/mer?analyze_started=all",
        status_code=302,
    )


@router.post("/admin/mer/{month}/analyze")
def admin_mer_analyze_one(
    month: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    try:
        parsed_month = datetime.strptime(
            month,
            "%Y-%m",
        ).date().replace(day=1)
    except ValueError:
        return RedirectResponse(
            url="/admin/mer?error=invalid_month",
            status_code=302,
        )

    audit_log(
        request,
        "admin_mer_analyze_one",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id=month,
        details=f"Analyse MER {month} lancée.",
    )

    background_tasks.add_task(
        analyze_mer_archives,
        parsed_month,
    )

    return RedirectResponse(
        url=(
            f"/admin/mer?analyze_started=one"
            f"&analyze_month={month}"
        ),
        status_code=302,
    )


@router.post("/admin/mer/import-kills")
def admin_mer_import_kills(
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    audit_log(
        request,
        "admin_mer_import_kills",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id="kill_dumps",
        details="Import des kill dumps MER analysés lancé.",
    )

    background_tasks.add_task(import_mer_kill_dumps)

    return RedirectResponse(
        url="/admin/mer?kill_import_started=1",
        status_code=302,
    )


@router.post("/admin/mer/resolve-entity-ids")
def admin_mer_resolve_entity_ids(
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    audit_log(
        request,
        "admin_mer_resolve_entity_ids",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id="entity_ids",
        details="Résolution des IDs corporation/alliance MER lancée.",
    )

    background_tasks.add_task(enrich_mer_entity_ids)

    return RedirectResponse(
        url="/admin/mer?entity_enrich_started=1",
        status_code=302,
    )



@router.post("/admin/mer/match-killmails")
def admin_mer_match_killmails(
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.mer.view")
    if redirect:
        return redirect

    audit_log(
        request,
        "admin_mer_match_killmails",
        user_id=user["id"],
        username=user["username"],
        target_type="mer",
        target_id="killmails",
        details="MER -> existing raw killmail matching started.",
    )

    background_tasks.add_task(match_mer_killmails)

    return RedirectResponse(
        url="/admin/mer?kill_match_started=1",
        status_code=302,
    )
