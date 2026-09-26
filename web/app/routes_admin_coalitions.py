from datetime import datetime
import json

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from .audit import audit_log
from .auth import has_permission, require_login, require_permission_or_redirect
from .coalitions import (
    add_membership,
    analyze_bulk_import,
    create_coalition,
    import_bulk_memberships,
    delete_membership,
    get_coalition,
    list_coalitions,
    list_memberships,
    search_member_entities,
    coalition_logo_file,
    save_coalition_logo,
    remove_coalition_logo,
    update_coalition,
    update_membership,
)
from .layout import app_context
from .main_objects import templates

router = APIRouter()


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("invalid_date") from exc


def _error_code(exc):
    code = str(exc)
    allowed = {
        "name_required",
        "coalition_not_found",
        "member_not_found",
        "membership_not_found",
        "invalid_date",
        "invalid_date_range",
        "invalid_operation",
        "invalid_member_type",
        "coalition_cycle",
        "duplicate_rule",
        "logo_required",
        "logo_too_large",
        "invalid_logo_type",
        "invalid_logo_file",
    }
    return code if code in allowed else "save_failed"


BULK_IMPORT_MAX_BYTES = 25 * 1024 * 1024
COALITION_LOGO_MAX_BYTES = 5 * 1024 * 1024


def _detect_logo_extension(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    raise ValueError("invalid_logo_file")


async def _read_coalition_logo(upload):
    if upload is None or not (upload.filename or "").strip():
        raise ValueError("logo_required")

    declared_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if declared_type and declared_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("invalid_logo_type")

    raw = await upload.read(COALITION_LOGO_MAX_BYTES + 1)
    if len(raw) > COALITION_LOGO_MAX_BYTES:
        raise ValueError("logo_too_large")
    if not raw:
        raise ValueError("logo_required")

    return raw, _detect_logo_extension(raw)


async def _read_bulk_json(upload: UploadFile):
    raw = await upload.read(BULK_IMPORT_MAX_BYTES + 1)
    if len(raw) > BULK_IMPORT_MAX_BYTES:
        raise ValueError("file_too_large")
    if not raw:
        raise ValueError("empty_file")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid_encoding") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_json:{exc.lineno}:{exc.colno}:{exc.msg}") from exc




def _parse_bulk_resolutions(raw):
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_resolutions") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid_resolutions")

    result = {}
    for row_number, entity_id in data.items():
        try:
            row_number = int(row_number)
            entity_id = int(entity_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_resolutions") from exc
        if row_number < 1 or entity_id < 1:
            raise ValueError("invalid_resolutions")
        result[row_number] = entity_id
    return result

def _bulk_upload_error(exc):
    code = str(exc)
    if code == "file_too_large":
        return {"ready": False, "error_count": 1, "errors": [{"row": None, "code": code, "message": "JSON file exceeds 25 MB."}]}
    if code == "empty_file":
        return {"ready": False, "error_count": 1, "errors": [{"row": None, "code": code, "message": "JSON file is empty."}]}
    if code == "invalid_encoding":
        return {"ready": False, "error_count": 1, "errors": [{"row": None, "code": code, "message": "JSON file must be UTF-8."}]}
    if code == "invalid_resolutions":
        return {"ready": False, "error_count": 1, "errors": [{"row": None, "code": code, "message": "Invalid alliance resolution selection."}]}
    if code.startswith("invalid_json:"):
        _prefix, line, column, message = code.split(":", 3)
        return {"ready": False, "error_count": 1, "errors": [{"row": None, "code": "invalid_json", "message": f"Invalid JSON at line {line}, column {column}: {message}"}]}
    return {"ready": False, "error_count": 1, "errors": [{"row": None, "code": "import_failed", "message": "Bulk import validation failed."}]}


@router.get("/entities/coalition/manage/import")
def coalition_bulk_import_page(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect
    return RedirectResponse(url="/entities/coalition/manage#bulk-import", status_code=302)


@router.post("/admin/coalitions/bulk-import/preview")
async def coalition_bulk_import_preview(
    request: Request,
    source: str = Form(...),
    resolutions: str = Form(""),
    json_file: UploadFile = File(...),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return JSONResponse({"ready": False, "error": "forbidden"}, status_code=403)

    try:
        payload = await _read_bulk_json(json_file)
        resolution_map = _parse_bulk_resolutions(resolutions)
        report = analyze_bulk_import(payload, source, resolutions=resolution_map)
        report["filename"] = json_file.filename or ""
        return JSONResponse(report)
    except ValueError as exc:
        report = _bulk_upload_error(exc)
        report["filename"] = json_file.filename or ""
        return JSONResponse(report, status_code=400)


@router.post("/admin/coalitions/bulk-import")
async def coalition_bulk_import_execute(
    request: Request,
    source: str = Form(...),
    resolutions: str = Form(""),
    json_file: UploadFile = File(...),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return JSONResponse({"ready": False, "error": "forbidden"}, status_code=403)

    try:
        payload = await _read_bulk_json(json_file)
        resolution_map = _parse_bulk_resolutions(resolutions)
        report = import_bulk_memberships(
            payload,
            source,
            actor_user_id=user["id"],
            resolutions=resolution_map,
        )
        report["filename"] = json_file.filename or ""
        if not report.get("ready"):
            return JSONResponse(report, status_code=400)

        audit_log(
            request,
            "admin_coalition_bulk_import",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition_bulk_import",
            target_id=None,
            details=(
                f"source={report.get('source')}; file={json_file.filename or '-'}; "
                f"rows={report.get('total_rows')}; new_coalitions={report.get('new_coalition_count')}"
            ),
        )
        return JSONResponse(report)
    except ValueError as exc:
        report = _bulk_upload_error(exc)
        report["filename"] = json_file.filename or ""
        return JSONResponse(report, status_code=400)
    except Exception:
        return JSONResponse(
            {
                "ready": False,
                "error_count": 1,
                "errors": [{"row": None, "code": "import_failed", "message": "Import failed; transaction was rolled back."}],
                "filename": json_file.filename or "",
            },
            status_code=500,
        )


@router.get("/admin/coalitions", response_class=HTMLResponse)
def admin_coalitions(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect
    query = (request.query_params.get("q") or "").strip()
    suffix = f"?q={query}" if query else ""
    return RedirectResponse(url=f"/entities/coalition/manage{suffix}", status_code=302)


@router.post("/admin/coalitions/create")
def admin_coalition_create(
    request: Request,
    name: str = Form(...),
    short_name: str = Form(""),
    description: str = Form(""),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    try:
        coalition_id = create_coalition(
            name=name,
            short_name=short_name,
            description=description,
            actor_user_id=user["id"],
        )
        audit_log(
            request,
            "admin_coalition_created",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition",
            target_id=coalition_id,
            details=name.strip(),
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/entities/coalition/manage?error={_error_code(exc)}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/coalition/{coalition_id}/manage?success=coalition_created",
        status_code=302,
    )


@router.get("/admin/coalitions/member-search")
def admin_coalition_member_search(request: Request, q: str = "", coalition_id: int | None = None):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return JSONResponse({"results": []}, status_code=403)

    return {
        "results": search_member_entities(
            q,
            limit=25,
            excluded_coalition_id=coalition_id,
        )
    }


@router.get("/admin/coalitions/{coalition_id}", response_class=HTMLResponse)
def admin_coalition_detail(request: Request, coalition_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect
    return RedirectResponse(url=f"/coalition/{coalition_id}/manage", status_code=302)


@router.post("/admin/coalitions/{coalition_id}/update")
def admin_coalition_update(
    request: Request,
    coalition_id: int,
    name: str = Form(...),
    short_name: str = Form(""),
    description: str = Form(""),
    is_active: str = Form("off"),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    try:
        update_coalition(
            coalition_id=coalition_id,
            name=name,
            short_name=short_name,
            description=description,
            is_active=(is_active == "on"),
            actor_user_id=user["id"],
        )
        audit_log(
            request,
            "admin_coalition_updated",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition",
            target_id=coalition_id,
            details=name.strip(),
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error={_error_code(exc)}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/coalition/{coalition_id}/manage?success=coalition_updated",
        status_code=302,
    )


@router.get("/coalition/{coalition_id}/logo")
def coalition_logo_image(coalition_id: int):
    path = coalition_logo_file(coalition_id)
    if path is None:
        return Response(status_code=404)

    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/admin/coalitions/{coalition_id}/logo")
async def admin_coalition_logo_upload(
    request: Request,
    coalition_id: int,
    logo_file: UploadFile = File(...),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    coalition = get_coalition(coalition_id)
    if not coalition:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error=coalition_not_found",
            status_code=302,
        )

    try:
        raw, extension = await _read_coalition_logo(logo_file)
        save_coalition_logo(coalition_id, raw, extension)
        audit_log(
            request,
            "admin_coalition_logo_updated",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition",
            target_id=coalition_id,
            details=f"format={extension}; bytes={len(raw)}",
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error={_error_code(exc)}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/coalition/{coalition_id}/manage?success=coalition_logo_updated",
        status_code=302,
    )


@router.post("/admin/coalitions/{coalition_id}/logo/remove")
def admin_coalition_logo_remove(request: Request, coalition_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    coalition = get_coalition(coalition_id)
    if not coalition:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error=coalition_not_found",
            status_code=302,
        )

    try:
        remove_coalition_logo(coalition_id)
        audit_log(
            request,
            "admin_coalition_logo_removed",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition",
            target_id=coalition_id,
            details="logo removed",
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error={_error_code(exc)}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/coalition/{coalition_id}/manage?success=coalition_logo_removed",
        status_code=302,
    )


@router.post("/admin/coalitions/{coalition_id}/memberships/add")
def admin_coalition_membership_add(
    request: Request,
    coalition_id: int,
    operation: str = Form(...),
    member_type: str = Form(...),
    member_id: int = Form(...),
    valid_from: str = Form(""),
    valid_to: str = Form(""),
    source: str = Form(""),
    notes: str = Form(""),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    try:
        membership_id = add_membership(
            coalition_id=coalition_id,
            operation=operation,
            member_type=member_type,
            member_id=member_id,
            valid_from=_parse_date(valid_from),
            valid_to=_parse_date(valid_to),
            source=source,
            notes=notes,
            actor_user_id=user["id"],
        )
        audit_log(
            request,
            "admin_coalition_membership_added",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition_membership",
            target_id=membership_id,
            details=f"coalition={coalition_id}; {operation} {member_type}:{member_id}; {valid_from or '-'} -> {valid_to or '-'}",
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error={_error_code(exc)}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/coalition/{coalition_id}/manage?success=membership_added",
        status_code=302,
    )


@router.post("/admin/coalitions/{coalition_id}/memberships/{membership_id}/update")
def admin_coalition_membership_update(
    request: Request,
    coalition_id: int,
    membership_id: int,
    operation: str = Form(...),
    valid_from: str = Form(""),
    valid_to: str = Form(""),
    source: str = Form(""),
    notes: str = Form(""),
    replacement_member_type: str = Form(""),
    replacement_member_id: str = Form(""),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    try:
        replacement_member_type = (replacement_member_type or "").strip()
        replacement_member_id = (replacement_member_id or "").strip()
        update_membership(
            coalition_id=coalition_id,
            membership_id=membership_id,
            operation=operation,
            valid_from=_parse_date(valid_from),
            valid_to=_parse_date(valid_to),
            source=source,
            notes=notes,
            actor_user_id=user["id"],
            replacement_member_type=replacement_member_type or None,
            replacement_member_id=replacement_member_id or None,
        )
        audit_log(
            request,
            "admin_coalition_membership_updated",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition_membership",
            target_id=membership_id,
            details=(
                f"coalition={coalition_id}; operation={operation}; {valid_from or '-'} -> {valid_to or '-'}"
                + (f"; replacement={replacement_member_type}:{replacement_member_id}" if replacement_member_type and replacement_member_id else "")
            ),
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error={_error_code(exc)}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/coalition/{coalition_id}/manage?success=membership_updated",
        status_code=302,
    )


@router.post("/admin/coalitions/{coalition_id}/memberships/{membership_id}/delete")
def admin_coalition_membership_delete(request: Request, coalition_id: int, membership_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.coalition.admin")
    if redirect:
        return redirect

    try:
        delete_membership(coalition_id, membership_id)
        audit_log(
            request,
            "admin_coalition_membership_deleted",
            user_id=user["id"],
            username=user["username"],
            target_type="coalition_membership",
            target_id=membership_id,
            details=f"coalition={coalition_id}",
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/coalition/{coalition_id}/manage?error={_error_code(exc)}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/coalition/{coalition_id}/manage?success=membership_deleted",
        status_code=302,
    )
