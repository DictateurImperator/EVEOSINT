import json
import logging
from datetime import date, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg2.errors import QueryCanceled
from pydantic import BaseModel, Field, StrictInt

from . import forensics_store, forensics_batch
from .auth import has_permission, require_login, require_permission_or_redirect
from .entities import EntityError, get_hidden_killmails_page
from .forensics_forecast import EVIDENCE_FILTERS
from .killmail_filters import killmail_filters_from_request, search_killboard_filters
from .layout import app_context
from .main_objects import templates

router = APIRouter()
logger = logging.getLogger(__name__)
PERMISSION = "admin.killmail_forensics.dev"


def _data_access_error(request):
    user = require_login(request)
    if not user:
        return JSONResponse({"error": "Please log in to continue."}, status_code=401)
    if not has_permission(user, PERMISSION):
        return JSONResponse(
            {"error": "The Killmail Forensics dev permission is required."},
            status_code=403,
        )
    return None


@router.get("/admin/killmail-forensics", response_class=HTMLResponse)
def admin_killmail_forensics(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, PERMISSION)
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Killmail Forensics",
        active_module="admin",
        active_menu_key="admin.killmail_forensics",
    )
    context.update(
        {
            "killboard_search_url": "/admin/killmail-forensics/search",
            "killboard_entity_placeholder": "Alliance, corporation…",
            "can_run_forensics_analysis": has_permission(user, "admin.jobs.run"),
            "forensics_evidence_filters": EVIDENCE_FILTERS,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_killmail_forensics.html",
        context=context,
    )


@router.get("/admin/killmail-forensics/data", response_class=JSONResponse)
def admin_killmail_forensics_data(
    request: Request, limit: int = Query(100, ge=1, le=100)
):
    denied = _data_access_error(request)
    if denied is not None:
        return denied
    try:
        page = get_hidden_killmails_page(
            per_page=limit,
            filters=killmail_filters_from_request(request),
        )
    except (EntityError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except QueryCanceled:
        return JSONResponse(
            {"error": "The query time limit was reached. Narrow the filters or retry."},
            status_code=503,
        )
    except Exception:
        logger.exception("Hidden killmail search failed")
        return JSONResponse(
            {"error": "Hidden killmails could not be loaded. Please retry."},
            status_code=500,
        )
    html = templates.env.get_template("entity_killmails_fragment.html").render(
        request=request,
        killmail_page=page,
        killmail_mode="hidden",
        killmail_base_url=None,
        killmail_selectable=True,
        error=None,
    )
    return JSONResponse({"html": html, "count": len(page["killmails"])})


@router.get("/admin/killmail-forensics/search", response_class=JSONResponse)
def admin_killmail_forensics_search(
    request: Request,
    kind: str = Query(...),
    q: str = Query(""),
    limit: int = Query(15),
):
    denied = _data_access_error(request)
    if denied is not None:
        return denied
    if kind not in {"ship", "entity", "zone"}:
        return JSONResponse(
            {"error": "Invalid search kind.", "results": []}, status_code=400
        )
    return JSONResponse(
        {"results": search_killboard_filters(kind, q, limit, mer_only=True)}
    )


class MerReference(BaseModel):
    kill_datetime: datetime
    source_month: date
    source_row: int = Field(ge=1)


class CandidateChoices(BaseModel):
    ids: list[StrictInt] = Field(max_length=200)
    victims: list[StrictInt | None] = Field(max_length=200)
    attackers: list[StrictInt | None] = Field(max_length=200)


def _workspace_access(request, write=False):
    denied = _data_access_error(request)
    if denied is not None:
        return denied
    if write:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.headers.get("host")
        ):
            return JSONResponse(
                {"error": "Use the Forensics page to perform this action."},
                status_code=403,
            )
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse(
                {"error": "A JSON request is required."}, status_code=415
            )
    try:
        ready = forensics_store.ready()
    except QueryCanceled:
        return JSONResponse(
            {"error": "Forensics setup check timed out. Please retry."},
            status_code=503,
        )
    except Exception:
        logger.exception("Forensics setup check failed")
        return JSONResponse(
            {"error": "Forensics setup could not be checked. Check the server logs and retry."},
            status_code=500,
        )
    if not ready:
        return JSONResponse(
            {
                "error": "Run “Forensics · Create investigation tables (run once)” from Admin Jobs first.",
                "setup_required": True,
            },
            status_code=503,
            headers={"X-Forensics-Setup-Required": "1"},
        )
    return None


def _workspace_result(call):
    try:
        return JSONResponse(forensics_store.json_safe(call()))
    except (ValueError, EntityError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except QueryCanceled:
        return JSONResponse(
            {
                "error": "Evidence query exceeded its time limit. Retry or choose another killmail."
            },
            status_code=503,
        )
    except Exception:
        logger.exception("Forensics workspace operation failed")
        return JSONResponse(
            {
                "error": "The operation could not be completed. Check the server logs and retry."
            },
            status_code=500,
        )


@router.get("/admin/killmail-forensics/cases", response_class=JSONResponse)
def forensics_cases(
    request: Request,
    sort: str = "attempts",
    page: int = Query(1, ge=1),
    recovered: bool = False,
    assessment: str = "",
    evidence: list[str] = Query(default=[]),
    evidence_role: str = "either",
):
    denied = _workspace_access(request)
    if denied is not None:
        return denied
    return _workspace_result(
        lambda: {
            "cases": forensics_store.list_cases(
                sort, page, recovered, assessment, evidence, evidence_role
            )
        }
    )


@router.post("/admin/killmail-forensics/cases", response_class=JSONResponse)
def forensics_create_case(request: Request, reference: MerReference):
    denied = _workspace_access(request, True)
    if denied is not None:
        return denied
    ref = {
        "kill_datetime": reference.kill_datetime.isoformat(),
        "source_month": reference.source_month.isoformat(),
        "source_row": reference.source_row,
    }
    return _workspace_result(
        lambda: forensics_store.create_case(ref, require_login(request)["id"])
    )


@router.post(
    "/admin/killmail-forensics/cases/{case_id}/choices", response_class=JSONResponse
)
def forensics_choices(request: Request, case_id: int, choices: CandidateChoices):
    denied = _workspace_access(request, True)
    if denied is not None:
        return denied
    return _workspace_result(
        lambda: forensics_store.save_choices(case_id, choices.model_dump())
    )


@router.post(
    "/admin/killmail-forensics/cases/{case_id}/refresh", response_class=JSONResponse
)
def forensics_refresh(request: Request, case_id: int):
    denied = _workspace_access(request, True)
    if denied is not None:
        return denied
    return _workspace_result(lambda: forensics_store.refresh_case(case_id))


@router.post(
    "/admin/killmail-forensics/cases/{case_id}/validate", response_class=JSONResponse
)
def forensics_validate(request: Request, case_id: int):
    denied = _workspace_access(request, True)
    if denied is not None:
        return denied

    def validate_and_refresh():
        result = forensics_store.validate_next(case_id, require_login(request)["id"])
        if result.get("neighbors_queued"):
            try:
                from .jobs import run_forensics_analysis_job

                run_forensics_analysis_job(
                    require_login(request)["id"], refresh_only=True
                )
            except Exception:
                # A running worker drains this durable queue. Launch failures never
                # discard a CCP result; pending recalculations remain visible.
                logger.info(
                    "Neighbor recalculation queued; worker already running or launch unavailable.",
                    exc_info=True,
                )
        return result

    return _workspace_result(validate_and_refresh)


@router.get("/admin/killmail-forensics/recovered", response_class=HTMLResponse)
def forensics_recovered(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, PERMISSION)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="forensics_recovered.html",
        context=app_context(
            request=request,
            user=user,
            title="EVEOSINT - Recovered killmails",
            active_module="admin",
            active_menu_key="admin.killmail_forensics",
        ),
    )


@router.get(
    "/admin/killmail-forensics/recovered/{kill_id}", response_class=HTMLResponse
)
def forensics_recovered_kill(request: Request, kill_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, PERMISSION)
    if redirect:
        return redirect
    denied = _workspace_access(request)
    if denied is not None:
        return denied
    try:
        data = forensics_store.recovered_detail(kill_id)
    except ValueError:
        return HTMLResponse("Recovered killmail not found.", status_code=404)
    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - Recovered kill {kill_id}",
        active_module="admin",
        active_menu_key="admin.killmail_forensics",
    )
    context.update(
        {"kill": data, "payload_json": json.dumps(data["payload"], indent=2)}
    )
    return templates.TemplateResponse(
        request=request, name="forensics_recovered_detail.html", context=context
    )


class AnalysisScope(BaseModel):
    date_from: date | None = None
    date_to: date | None = None


@router.get("/admin/killmail-forensics/analysis", response_class=JSONResponse)
def forensics_analysis_status(request: Request):
    denied = _workspace_access(request)
    if denied is not None:
        return denied
    return _workspace_result(forensics_batch.analysis_status)


@router.post("/admin/killmail-forensics/analysis/start", response_class=JSONResponse)
def forensics_analysis_start(request: Request, scope: AnalysisScope):
    denied = _workspace_access(request, True)
    if denied is not None:
        return denied
    user = require_login(request)
    if not has_permission(user, "admin.jobs.run"):
        return JSONResponse(
            {
                "error": "The admin.jobs.run permission is required to start the background job."
            },
            status_code=403,
        )
    from .jobs import JobError, run_forensics_analysis_job
    from .audit import audit_log

    try:
        ok, message = run_forensics_analysis_job(
            user["id"], scope.date_from, scope.date_to
        )
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="analyze_hidden_killmails",
            details=message,
        )
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(
        {"message": "Background analysis started. You may close this tab."},
        status_code=200 if ok else 503,
    )


@router.post("/admin/killmail-forensics/analysis/stop", response_class=JSONResponse)
def forensics_analysis_stop(request: Request):
    denied = _workspace_access(request, True)
    if denied is not None:
        return denied
    return _workspace_result(forensics_batch.stop_analysis)
