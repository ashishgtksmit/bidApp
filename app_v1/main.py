from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
# 🔐 ADDED: to customize Swagger/OpenAPI so it knows about Bearer + X-Client-Id
from fastapi.openapi.utils import get_openapi

from .endpoints.location import router as location_router
from .endpoints.car import router as car_router
from .endpoints.request import router as request_router
from .endpoints.review import router as review_router
from .endpoints.bid import router as bid_router
from .endpoints.user import router as user_router
from .endpoints.driver import router as driver_router
from .endpoints.utils import router as utils_router
from .endpoints.auth import router as auth_router
from .endpoints.reporting import router as reporting_router
from .endpoints.chat import router as chat_router
from .database import Base, engine
from .auth.deps import get_current_user_id
from .events.outbox import (
    configure_domain_event_logging,
    log_domain_event_flag_snapshot,
)
# Ensure PR5 OTP / reset-token / rate-limit tables are registered before create_all.
from .models import otp_challenge as _otp_challenge_models  # noqa: F401
# Ensure PR14 driver OTP challenge / token tables are registered before create_all.
from .models import driver_otp as _driver_otp_models  # noqa: F401
import traceback


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Safe boolean flag snapshot only — proves App Setting recycle to operators.
    try:
        configure_domain_event_logging()
        log_domain_event_flag_snapshot(reason="startup")
    except Exception:
        # Never block API boot on diagnostics.
        pass
    yield


app = FastAPI(title="OpenBid", lifespan=_lifespan)

# -------- CORS (for Angular at http://localhost:4200) --------
origins = [
    "http://localhost:4200",   # Angular dev
    # add prod origins here later
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,      # temporarily ["*"] if you want
    allow_credentials=True,
    allow_methods=["*"],        # includes OPTIONS
    allow_headers=["*"],        # includes X-Client-Id, Content-Type
)

# -------- Routers --------
app.include_router(auth_router, tags=['Auth'])
app.include_router(location_router, tags=['Location'])
app.include_router(car_router, tags=['Car'])
app.include_router(request_router, tags=['Request'])
app.include_router(review_router, tags=['Review'])
app.include_router(bid_router, tags=['Bids'])
app.include_router(user_router, tags=['User'])
app.include_router(driver_router, tags=['Driver'])
app.include_router(utils_router, tags=['Utilities'])
app.include_router(reporting_router, tags=['Reporting'])
app.include_router(chat_router, tags=['Chat'])

Base.metadata.create_all(bind=engine)

# -------- Explicit OPTIONS for /login (preflight) --------
@app.options("/login")
async def login_options():
    """
    Handle CORS preflight for POST /login.
    """
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "http://localhost:4200",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Client-Id",
            "Access-Control-Allow-Credentials": "true",
        },
    )

# -------- Exception-catching middleware --------
@app.middleware("http")
async def catch_exceptions_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        tb = traceback.format_exc()
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": tb},
        )

# -------- Custom OpenAPI (if you really need it) --------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version="1.0.0",
        description="OpenBid API",
        routes=app.routes,
    )
    # NOTE: if you want BearerAuth/ClientIdHeader, you need to define them
    # in components["securitySchemes"] here; otherwise comment out the next line.
    # openapi_schema["security"] = [{"BearerAuth": [], "ClientIdHeader": []}]
    app.openapi_schema = openapi_schema
    return openapi_schema

app.openapi = custom_openapi

# # app = FastAPI(title="OpenBid",dependencies=[Depends(get_current_user_id)])
# app = FastAPI(title="OpenBid")

# app.include_router(auth_router, tags=['Auth'])
# app.include_router(location_router, tags=['Location'])
# app.include_router(car_router, tags=['Car'])
# app.include_router(request_router, tags=['Request'])
# app.include_router(review_router, tags=['Review'])
# app.include_router(bid_router, tags=['Bids'])
# app.include_router(user_router, tags=['User'])
# app.include_router(driver_router, tags=['Driver'])
# app.include_router(utils_router, tags=['Utilities'])

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],   # only for dev!
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# Base.metadata.create_all(bind=engine)



# # 🔐 ADDED: custom OpenAPI so Swagger shows both Authorize boxes
# def custom_openapi():
#     if app.openapi_schema:
#         return app.openapi_schema
#     openapi_schema = get_openapi(
#         title=app.title,
#         version="1.0.0",
#         description="OpenBid API",
#         routes=app.routes,
#     )
#     # # security schemes
#     # openapi_schema.setdefault("components", {}).setdefault("securitySchemes", {})
#     # openapi_schema["components"]["securitySchemes"]["BearerAuth"] = {
#     #     "type": "http",
#     #     "scheme": "bearer",
#     #     "bearerFormat": "JWT",
#     # }
#     # openapi_schema["components"]["securitySchemes"]["ClientIdHeader"] = {
#     #     "type": "apiKey",
#     #     "in": "header",
#     #     "name": "X-Client-Id",
#     # }
#     # Make both required globally so the “Authorize” applies to all operations
#     openapi_schema["security"] = [
#         {"BearerAuth": [], "ClientIdHeader": []}
#     ]
#     app.openapi_schema = openapi_schema
#     return openapi_schema

# # 🔐 ADDED: tell FastAPI to use our custom schema
# app.openapi = custom_openapi

# @app.middleware("http")
# async def catch_exceptions_middleware(request, call_next):
#     try:
#         return await call_next(request)
#     except Exception as e:
#         tb = traceback.format_exc()
#         return JSONResponse(
#             status_code=500,
#             content={"error":str(e), "traceback" :tb}
#         )