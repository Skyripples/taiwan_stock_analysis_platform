"""FastAPI application for read-only PostgreSQL stock queries."""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response

from api.db import DatabaseUnavailable, pool
from api.routes.health import router as health_router
from api.routes.stocks import router as stocks_router
from api.routes.auth import router as auth_router


LOGGER = logging.getLogger("taiwan_stock_api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="Taiwan Stock Analysis API",
    version="3.15.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=700)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://skyripples.github.io"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Accept", "Authorization", "Content-Type", "If-None-Match"],
    expose_headers=["ETag", "Last-Modified"],
    max_age=600,
)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/v1/auth"):
        response.headers["Cache-Control"] = "no-store"
        return response
    if request.method != "GET" or response.status_code >= 400:
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=30"})
    headers = dict(response.headers)
    headers.update({"ETag": etag, "Cache-Control": "public, max-age=30"})
    return Response(content=body, status_code=response.status_code, headers=headers,
                    media_type=response.media_type)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    return error(exc.status_code, detail.get("code", "HTTP_ERROR"), detail.get("message", "Request failed"))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, __: RequestValidationError):
    return error(422, "VALIDATION_ERROR", "Request parameters are invalid")


@app.exception_handler(DatabaseUnavailable)
async def database_error(_: Request, __: DatabaseUnavailable):
    return error(503, "DATABASE_UNAVAILABLE", "Database is temporarily unavailable")


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    LOGGER.exception("Unhandled API error", exc_info=exc)
    return error(500, "INTERNAL_ERROR", "Internal server error")


app.include_router(health_router, prefix="/api/v1")
app.include_router(stocks_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
