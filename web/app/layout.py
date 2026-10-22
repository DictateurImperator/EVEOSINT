from .menus import build_context_menu, build_top_menu


def app_context(request, user, title, active_module, active_menu_key):
    if user and user.get('is_public'):
        return dict(request=request,user=user,username='',title=title,show_app_layout=False,public_mode=True,
                    active_module=active_module,top_menu=[],context_title='',context_menu=[])
    context_menu = build_context_menu(user, active_module, active_menu_key)

    return {
        "request": request,
        "title": title,
        "user": user,
        "username": user["username"],
        "show_app_layout": True,
        "active_module": active_module,
        "top_menu": build_top_menu(user, active_module),
        "context_title": context_menu["title"],
        "context_menu": context_menu["items"],
    }
