from fastapi import APIRouter

from . import routes_access, routes_catalog, routes_marketplace, routes_money, routes_partners

admin_router = APIRouter(prefix="/admin/v1", include_in_schema=False)
admin_router.include_router(routes_access.router, tags=["Admin · Access"])
admin_router.include_router(routes_marketplace.router, tags=["Admin · Marketplace"])
admin_router.include_router(routes_partners.router, tags=["Admin · Partners"])
admin_router.include_router(routes_money.router, tags=["Admin · Money"])
admin_router.include_router(routes_catalog.router, tags=["Admin · Catalog"])
