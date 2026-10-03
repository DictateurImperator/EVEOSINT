from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .audit import audit_log
from .auth import require_login, require_permission_or_redirect
from .jobs import (
    JobError,
    list_jobs_with_logs,
    read_job_log,
    run_killmail_job,
    run_population_alliances_init_job,
    run_population_alliances_daily_job,
    run_recent_kill_pilots_affiliation_job,
    run_character_skill_inference_job,
    run_sovereignty_esi_job,
    run_sde_job,
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
