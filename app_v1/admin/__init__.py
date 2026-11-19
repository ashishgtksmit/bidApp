"""OpenBid Admin Console API (``/admin/v1``).

Drop this package into ``bidApp/app_v1/admin/`` and register it in ``main.py``::

    from .admin import admin_router, install_admin_cors
    app.include_router(admin_router)

Admin identity is fully separate from mobile users: its own table
(``admin_users``), its own signing secret (``ADMIN_JWT_SECRET``) and audience.
Mobile JWTs are never accepted on these routes.
"""

from .router import admin_router  # noqa: F401
