"""Explicit admin-only snapshot adapters. Never called by a public GET."""
from markupsafe import Markup
from starlette.requests import Request
from .main_objects import templates


def content(name, context):
    template = templates.env.get_template(name)
    return ''.join(template.blocks['content'](template.new_context(context)))


def source_request(request, target):
    scope = dict(request.scope, method='GET', path=target.path, raw_path=target.path.encode(),
                 query_string=target.query.encode())
    return Request(scope)


def render(request, target):
    """Use the requesting admin's real session; no synthetic user or auth bypass."""
    from . import routes_admin_rankings, routes_superintel, routes_entities
    from .entities import get_entity_profile
    from . import killmail_rankings
    request = source_request(request, target)
    if target.key == 'leaderboard':
        response = routes_admin_rankings.page(request)
    elif target.key == 'superintel':
        handler = {'dashboard': routes_superintel.superintel_dashboard,
                   'detailed': routes_superintel.superintel_detailed_dashboard,
                   'monthly-analysis': routes_superintel.superintel_monthly_analysis}[target.tab]
        response = handler(request)
    elif target.key == 'kill':
        response = routes_entities.kill_profile(request, target.entity_id)
    else:
        # Do not invoke entity_profile: its private population enrichment is
        # unnecessary here. Reuse its exact header and shared result templates.
        profile = get_entity_profile(target.key, target.entity_id)
        if not profile:
            raise ValueError('Entity not found.')
        fragment = ''
        if target.tab == 'stats':
            history = killmail_rankings.combat_history(target.key, target.entity_id)
            fragment = templates.env.get_template('entity_combat_stats.html').render(
                profile=profile, combat_stats=history, combat_stats_view='history',
                compact_isk=killmail_rankings.compact_isk, full_isk=killmail_rankings.full_isk)
        elif target.tab == 'killboard':
            if target.key in ('system', 'constellation', 'region'):
                response = routes_entities.location_killmails_fragment(request, target.key, target.entity_id)
            else:
                handler = {'character': routes_entities.character_killmails_fragment,
                           'corporation': routes_entities.corporation_killmails_fragment,
                           'alliance': routes_entities.alliance_killmails_fragment,
                           'ship': routes_entities.ship_killmails_fragment}[target.key]
                response = handler(request, target.entity_id)
            if response.status_code != 200 or not hasattr(response, 'context') or response.context.get('error'):
                raise ValueError('Killboard could not be prepared. Check the application log.')
            fragment = response.body.decode()
        return content('entity_profile.html', dict(request=request, profile=profile,
            profile_tab=target.tab, public_snapshot=Markup(fragment), location_previews={},
            error=None, user=None))
    if response.status_code != 200 or not hasattr(response, 'context') or response.context.get('error'):
        raise ValueError('Source page could not be prepared. Check the application log.')
    context = dict(response.context)
    if target.key == 'superintel' and target.tab == 'dashboard' and context.get('dashboard'):
        dashboard = dict(context['dashboard'])
        # The private template embeds hidden rows for client-side searching.
        # Anonymous snapshots contain only displayed rows, never that full set.
        limit = 100 if dashboard.get('full_ranking') else 10
        if dashboard.get('score_ranking_month_all'):
            dashboard['score_ranking_month_all'] = dashboard['score_ranking_month_all'][:limit]
        context['dashboard'] = dashboard
        if limit == 100:
            return '<p class="muted-line">Public snapshot: first 100 results.</p>' + content(response.template.name, context)
    return content(response.template.name, context)
