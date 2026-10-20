import logging
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse
from .audit import audit_log
from .auth import require_login, has_permission
from .killmail_esi_import import ImportError, import_link

router = APIRouter()
log = logging.getLogger(__name__)


@router.post('/admin/debug/killmail/import')
def import_killmail(request: Request, esi_url: str = Form('', max_length=1024)):
    user = require_login(request)
    if not user:
        return JSONResponse({'error': 'Connexion requise.'}, status_code=401)
    if not has_permission(user, 'admin.jobs.run'):
        return JSONResponse({'error': 'Droit de lancement des tests requis.'}, status_code=403)
    try:
        result = import_link(esi_url)
    except ImportError as exc:
        return JSONResponse({'error': str(exc)}, status_code=422)
    except Exception:
        # Do not log the ESI capability URL/hash, query parameters or response body.
        log.error('Single killmail import failed for user %s', user['id'])
        return JSONResponse({'error': 'Import interrompu. Réessaie ; un killmail déjà enregistré ne sera pas réécrit.'}, status_code=503)
    try:
        audit_log(request, 'admin_debug_killmail_import', user_id=user['id'], username=user['username'],
                  target_type='killmail', target_id=str(result['killmail_id']), details=result['status'])
    except Exception:
        log.error('Killmail import audit unavailable for user %s', user['id'])
    return JSONResponse(result)
