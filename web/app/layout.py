from .menus import build_context_menu, build_top_menu


def app_context(request, user, title, active_module, active_menu_key):
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
