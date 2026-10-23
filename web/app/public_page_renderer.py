"""Published read adapters: same templates/data paths, no admin/user operations."""
from starlette.requests import Request
from .main_objects import templates
from .public_read import read_scope


from functools import lru_cache
from jinja2 import ChoiceLoader, DictLoader


@lru_cache(maxsize=1)
def content_environment():
    # Render the actual private template normally, including its root macros.
    # Only the outer base is supplied once by the shared page layout.
    return templates.env.overlay(loader=ChoiceLoader([
        DictLoader({'base.html':'{% block content %}{% endblock %}'}),templates.env.loader]))


def content(name, context):
    return content_environment().get_template(name).render(context)


def source_request(request, target):
    query = target.query
    if target.tab == 'top-kills':
        from urllib.parse import parse_qsl, urlencode
        params = dict(parse_qsl(query));params.update(tab='stats',stats_view='top-kills')
        query = urlencode(params)
    return Request(dict(request.scope, method='GET', path=target.path, raw_path=target.path.encode(), query_string=query.encode(),
                        headers=[(k,v) for k,v in request.scope.get('headers',[]) if k not in (b'cookie',b'authorization')]))


def render(request, target):
    # Caller checked current publication policy (or authenticated admin preview).
    # The read identity is internal and carries no management permissions.
    from . import routes_admin_rankings, routes_superintel, routes_entities, routes_home
    request = source_request(request, target)
    context = dict(request=request, user=None, username='', show_app_layout=False, public_mode=True)
    with read_scope():
        if target.tab=='index':
            kind=target.path.removeprefix('/entities/')
            if kind in ('system','constellation','region','weapon'):
                index=dict(entity_type=kind,**routes_entities.ENTITY_INDEX_PAGES.get(kind,{'title':target.key.title(),'description':''}))
                return content('entity_index.html',dict(context,entity_index=index))
            response=routes_entities.entities_index(request,kind)
        elif target.key == 'leaderboard':
            response = routes_admin_rankings.ranking_response(request, context)
        elif target.key == 'superintel':
            handler = {'dashboard': routes_superintel.dashboard_response,
                       'detailed': routes_superintel.detailed_response,
                       'monthly-analysis': routes_superintel.monthly_response}[target.tab]
            response = handler(request, context)
        elif target.key == 'kill':
            if target.path.startswith('/kill/mer/'):
                parts=target.path.split('/')
                handler=routes_entities.mer_hidden_killmail if target.path.endswith('/hidden') else routes_entities.mer_killmail_ambiguous
                response=handler(request,parts[3],int(parts[4]),request.query_params.get('kill_datetime'))
            else:
                response = routes_entities.kill_profile(request, target.entity_id)
        elif '/pilotable-ships/' in target.path:
            ship_id=int(target.path.rsplit('/',1)[-1])
            response=(routes_entities._coalition_pilotable_ship_detail_response(request,target.entity_id,ship_id)
                      if target.key=='coalition' else routes_entities._group_pilotable_ship_detail_response(request,target.key,target.entity_id,ship_id))
        elif target.key == 'ship':
            if target.path=='/ship-category':
                response=routes_entities.ship_category_profile(request,**{key:request.query_params.get(key) for key in ('tier','group','size','faction')})
            else:response=routes_entities.ship_profile(request,target.entity_id)
        elif target.key == 'coalition':
            response=routes_entities.coalition_profile(request,target.entity_id)
        elif target.key in ('modules','skills','commodities'):
            response=routes_entities.entity_profile(request,target.path.split('/')[1],target.entity_id)
        elif target.key == 'tools':
            if target.tab=='overview':response=routes_home.tools(request)
            elif target.tab=='ship-analysis':response=routes_entities.ship_analysis_page(request)
            elif target.tab=='super-evolution':response=routes_superintel.superintel_evolution(request)
            else:response=routes_home.comparison_tool(request)
        elif target.key == 'map':
            handler={'universe':routes_entities.map_universe,'2d':routes_entities.map_eve_2d,
                     'region':routes_entities.map_region,'constellation':routes_entities.map_constellation,
                     'system':routes_entities.map_system}[target.tab]
            response=handler(request) if target.tab in ('universe','2d') else handler(request,int(target.path.rsplit('/',1)[-1]))
        elif target.key == 'killboard':
            # Existing global KB streams bounded historical windows for filters.
            # Its own JS requests the public read gateway, never private APIs.
            response = routes_entities.global_killboard(request)
        else:
            response = routes_entities.entity_profile(request, target.key, target.entity_id)
        if response.status_code != 200 or not hasattr(response,'context') or response.context.get('error'):
            raise ValueError('Page could not be loaded. Please retry.')
        context = dict(response.context, public_mode=True)
        if target.key=='tools' and target.tab=='overview':
            from . import public_pages
            policy=public_pages.settings()
            context['tools']=[item for item in context.get('tools',[]) if public_pages.link_url(item.get('href'),policy)]
        # Public-only overview is the normal shared header without a data tab.
        if target.tab == 'overview' and target.key!='ship':
            from markupsafe import Markup
            context['public_snapshot'] = Markup('')
        if target.key == 'superintel' and target.tab == 'dashboard' and context.get('dashboard'):
            dashboard = dict(context['dashboard'])
            if not dashboard.get('full_ranking') and dashboard.get('score_ranking_month_all'):
                dashboard['score_ranking_month_all'] = dashboard['score_ranking_month_all'][:10]
            context['dashboard'] = dashboard
        return content(response.template.name, context)
