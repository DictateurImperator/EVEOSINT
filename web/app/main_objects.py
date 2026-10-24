from fastapi.templating import Jinja2Templates
from .config import TEMPLATES_DIR

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Shared notice for authenticated pages; public pages use the same dialog partial.
from .public_traffic import access_decision
templates.env.globals['site_privacy_choice'] = access_decision
