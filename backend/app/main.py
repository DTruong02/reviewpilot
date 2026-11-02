import ipaddress

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes.indexes import router as indexes_router
from app.api.routes.read import router as read_router
from app.api.routes.webhooks import router as webhook_router

app = FastAPI(title="ReviewPilot API", version="0.1.0")


@app.middleware("http")
async def local_only_api_guard(request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/webhooks/github":
        client_host = request.client.host if request.client else None
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except (ValueError, TypeError):
            is_loopback = False
        if not is_loopback:
            return JSONResponse(
                status_code=403,
                content={
                    "code": "local_access_only",
                    "message": "This API is available only from the local machine.",
                },
            )
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if not isinstance(detail, dict) or "code" not in detail or "message" not in detail:
        detail = {"code": "request_failed", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"code": "invalid_request", "message": "Request validation failed."},
    )


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(webhook_router)
app.include_router(read_router)
app.include_router(indexes_router)
