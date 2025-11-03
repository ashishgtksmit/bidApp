from fastapi import FastAPI, Depends
from .endpoints.location import router as location_router
from .endpoints.car import router as car_router
from .endpoints.request import router as request_router
from .endpoints.review import router as review_router
from .endpoints.bid import router as bid_router
from .endpoints.user import router as user_router
from .endpoints.driver import router as driver_router
from .endpoints.utils import router as utils_router
from .endpoints.auth import router as auth_router
from .database import Base, engine
import traceback
from .auth.deps import get_current_user_id
from fastapi.responses import JSONResponse

# 🔐 ADDED: to customize Swagger/OpenAPI so it knows about Bearer + X-Client-Id
from fastapi.openapi.utils import get_openapi

app = FastAPI(title="OpenBid",dependencies=[Depends(get_current_user_id)])

app.include_router(auth_router, tags=['Auth'])
app.include_router(location_router, tags=['Location'])
app.include_router(car_router, tags=['Car'])
app.include_router(request_router, tags=['Request'])
app.include_router(review_router, tags=['Review'])
app.include_router(bid_router, tags=['Bids'])
app.include_router(user_router, tags=['User'])
app.include_router(driver_router, tags=['Driver'])
app.include_router(utils_router, tags=['Utilities'])

Base.metadata.create_all(bind=engine)



# 🔐 ADDED: custom OpenAPI so Swagger shows both Authorize boxes
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version="1.0.0",
        description="OpenBid API",
        routes=app.routes,
    )
    # # security schemes
    # openapi_schema.setdefault("components", {}).setdefault("securitySchemes", {})
    # openapi_schema["components"]["securitySchemes"]["BearerAuth"] = {
    #     "type": "http",
    #     "scheme": "bearer",
    #     "bearerFormat": "JWT",
    # }
    # openapi_schema["components"]["securitySchemes"]["ClientIdHeader"] = {
    #     "type": "apiKey",
    #     "in": "header",
    #     "name": "X-Client-Id",
    # }
    # Make both required globally so the “Authorize” applies to all operations
    openapi_schema["security"] = [
        {"BearerAuth": [], "ClientIdHeader": []}
    ]
    app.openapi_schema = openapi_schema
    return openapi_schema

# 🔐 ADDED: tell FastAPI to use our custom schema
app.openapi = custom_openapi

@app.middleware("http")
async def catch_exceptions_middleware(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        tb = traceback.format_exc()
        return JSONResponse(
            status_code=500,
            content={"error":str(e), "traceback" :tb}
        )