"""One error format for the whole API: {"error": {"code": ..., "message": ...}}."""

from __future__ import annotations

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("fulfillops.api")


class ApiError(Exception):
    """Raise this from a route to answer with a specific status and error code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


_HTTP_CODES = {
    404: ("NOT_FOUND", "Resource not found"),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code, message = _HTTP_CODES.get(exc.status_code, ("HTTP_ERROR", "Request failed"))
        return error_response(exc.status_code, code, message)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, exc: RequestValidationError):
        return error_response(400, "INVALID_REQUEST", "The request is not valid")

    @app.exception_handler(psycopg.OperationalError)
    async def _db_down(_: Request, exc: psycopg.OperationalError):
        # log the type only, the text can contain connection details
        log.warning("database unavailable (%s)", type(exc).__name__)
        return error_response(503, "SERVICE_UNAVAILABLE", "Service temporarily unavailable")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.error("unexpected error (%s)", type(exc).__name__)
        return error_response(500, "INTERNAL_ERROR", "Something went wrong")
