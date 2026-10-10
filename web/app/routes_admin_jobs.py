from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .audit import audit_log
from .auth import has_permission, require_login, require_permission_or_redirect
from .jobs import (
    JobError,
    list_jobs_with_logs,
    read_job_log,
    read_killmail_archive_refresh_progress,
    run_killmail_job,
    run_killmail_archive_refresh_job,
    stop_killmail_archive_refresh_job,
    run_population_alliances_init_job,
    run_population_alliances_daily_job,
    run_recent_kill_pilots_affiliation_job,
    run_character_skill_inference_job,
    run_sovereignty_esi_job,
    run_sde_job,
    run_killmail_forensics_setup_job,
    run_forensics_analysis_job,
)
from .layout import app_context
from .main_objects import templates

router = APIRouter()


def _error_redirect(exc):
    message = str(exc)
    if message.startswith("job_already_running"):
        return RedirectResponse(url="/admin/jobs?error=job_already_running", status_code=302)

    return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)


@router.get("/admin/jobs", response_class=HTMLResponse)
def admin_jobs(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin Jobs",
        active_module="admin",
        active_menu_key="admin.jobs",
    )
    context.update({
        "jobs": list_jobs_with_logs(),
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })

    return templates.TemplateResponse(request=request, name="admin_jobs.html", context=context)


@router.post("/admin/jobs/sde/run")
def admin_jobs_run_sde(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect

    try:
        ok, message = run_sde_job()
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_sde",
            details=message,
        )
    except JobError as exc:
        audit_log(
            request,
            "admin_job_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_sde",
            details=str(exc),
        )
        return _error_redirect(exc)

    if not ok:
        return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)

    return RedirectResponse(url="/admin/jobs?success=job_started", status_code=302)


@router.post("/admin/jobs/population-alliances-init/run")
def admin_jobs_run_population_alliances_init(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect

    try:
        ok, message = run_population_alliances_init_job()
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="population_alliances_init",
            details=message,
        )
    except JobError as exc:
        audit_log(
            request,
            "admin_job_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="population_alliances_init",
            details=str(exc),
        )
        return _error_redirect(exc)

    if not ok:
        return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)

    return RedirectResponse(url="/admin/jobs?success=job_started", status_code=302)

@router.post("/admin/jobs/population-alliances-daily/run")
def admin_jobs_run_population_alliances_daily(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect

    try:
        ok, message = run_population_alliances_daily_job()
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="population_alliances_daily",
            details=message,
        )
    except JobError as exc:
        audit_log(
            request,
            "admin_job_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="population_alliances_daily",
            details=str(exc),
        )
        return _error_redirect(exc)

    if not ok:
        return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)

    return RedirectResponse(url="/admin/jobs?success=job_started", status_code=302)


@router.post("/admin/jobs/sovereignty-esi/run")
def admin_jobs_run_sovereignty_esi(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect

    try:
        ok, message = run_sovereignty_esi_job()
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_sovereignty_esi",
            details=message,
        )
    except JobError as exc:
        audit_log(
            request,
            "admin_job_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_sovereignty_esi",
            details=str(exc),
        )
        return _error_redirect(exc)

    if not ok:
        return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)

    return RedirectResponse(url="/admin/jobs?success=job_started", status_code=302)


@router.post("/admin/jobs/killmails/run")
def admin_jobs_run_killmails(
    request: Request,
    from_date: str = Form(...),
    to_date: str = Form(...),
    workers: int = Form(...),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect

    try:
        ok, message = run_killmail_job(from_date, to_date, workers)
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_killmails",
            details=f"{message}; from={from_date}; to={to_date}; workers={workers}",
        )
    except JobError as exc:
        audit_log(
            request,
            "admin_job_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_killmails",
            details=str(exc),
        )
        return _error_redirect(exc)

    if not ok:
        return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)

    return RedirectResponse(url="/admin/jobs?success=job_started", status_code=302)


@router.post("/admin/jobs/recent-kill-pilots-affiliation/run")
def admin_jobs_run_recent_kill_pilots_affiliation(
    request: Request,
    days: int = Form(...),
    workers: int = Form(...),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect

    try:
        ok, message = run_recent_kill_pilots_affiliation_job(days, workers)
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_recent_kill_pilots_affiliation",
            details=f"{message}; days={days}; workers={workers}",
        )
    except JobError as exc:
        audit_log(
            request,
            "admin_job_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_recent_kill_pilots_affiliation",
            details=str(exc),
        )
        return _error_redirect(exc)

    if not ok:
        return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)

    return RedirectResponse(url="/admin/jobs?success=job_started", status_code=302)


@router.post("/admin/jobs/character-skill-inference/run")
def admin_jobs_run_character_skill_inference(
    request: Request,
    progress_batch_size: int = Form(...),
    limit: str = Form(""),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect

    try:
        ok, message = run_character_skill_inference_job(progress_batch_size, limit)
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_character_skill_inference",
            details=f"{message}; progress_batch_size={progress_batch_size}; limit={limit}",
        )
    except JobError as exc:
        audit_log(
            request,
            "admin_job_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="sync_character_skill_inference",
            details=str(exc),
        )
        return _error_redirect(exc)

    if not ok:
        return RedirectResponse(url="/admin/jobs?error=job_failed", status_code=302)

    return RedirectResponse(url="/admin/jobs?success=job_started", status_code=302)


@router.get("/admin/jobs/{job_key}/log")
def admin_jobs_log(request: Request, job_key: str):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect

    try:
        content = read_job_log(job_key, lines=200)
    except JobError:
        return PlainTextResponse("Log indisponible.", status_code=404)

    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@router.post("/admin/jobs/killmail-forensics-setup/run")
def admin_jobs_run_forensics_setup(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    try:
        ok, message = run_killmail_forensics_setup_job()
        audit_log(
            request, "admin_job_run", user_id=user["id"], username=user["username"],
            target_type="job", target_id="setup_killmail_forensics", details=message,
        )
    except JobError as exc:
        audit_log(
            request, "admin_job_run_failed", user_id=user["id"], username=user["username"],
            target_type="job", target_id="setup_killmail_forensics", details=str(exc),
        )
        return _error_redirect(exc)
    return RedirectResponse(
        url="/admin/jobs?success=job_started" if ok else "/admin/jobs?error=job_failed",
        status_code=302,
    )


@router.post("/admin/jobs/forensics-analysis/run")
def admin_jobs_run_forensics_analysis(request: Request, date_from: str = Form(""), date_to: str = Form("")):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    redirect = require_permission_or_redirect(user, "admin.killmail_forensics.dev")
    if redirect:
        return redirect
    try:
        ok, message = run_forensics_analysis_job(user["id"], date_from, date_to)
        audit_log(request, "admin_job_run", user_id=user["id"], username=user["username"],
                  target_type="job", target_id="analyze_hidden_killmails", details=message)
    except JobError as exc:
        return _error_redirect(exc)
    return RedirectResponse(url="/admin/jobs?success=job_started" if ok else "/admin/jobs?error=job_failed", status_code=302)


@router.post("/admin/jobs/killmail-archives/refresh")
def admin_jobs_refresh_killmail_archives(request: Request, date_from: str = Form(""), date_to: str = Form("")):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    try:
        ok, message = run_killmail_archive_refresh_job(date_from, date_to)
        audit_log(request, "admin_job_run", user_id=user["id"], username=user["username"],
                  target_type="job", target_id="refresh_killmail_archives",
                  details=f"{message}; from={date_from}; to={date_to}")
    except JobError as exc:
        audit_log(request, "admin_job_run_failed", user_id=user["id"], username=user["username"],
                  target_type="job", target_id="refresh_killmail_archives", details=str(exc))
        return _error_redirect(exc)
    return RedirectResponse(url="/admin/jobs?success=job_started" if ok else "/admin/jobs?error=job_failed", status_code=302)


@router.post("/admin/jobs/killmail-archives/stop")
def admin_jobs_stop_killmail_archives(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    try:
        _ok, message = stop_killmail_archive_refresh_job()
        audit_log(request, "admin_job_stop", user_id=user["id"], username=user["username"],
                  target_type="job", target_id="refresh_killmail_archives", details=message)
    except JobError as exc:
        return _error_redirect(exc)
    return RedirectResponse(url="/admin/jobs?success=job_stop_requested", status_code=302)


@router.get("/admin/jobs/killmail-archives/progress")
def admin_jobs_killmail_archive_progress(request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse({"error": "Please log in to continue."}, status_code=401)
    if not has_permission(user, "admin.jobs.view"):
        return JSONResponse({"error": "The admin.jobs.view permission is required."}, status_code=403)
    return JSONResponse(read_killmail_archive_refresh_progress(), headers={"Cache-Control": "no-store"})
