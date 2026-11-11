"""Translate domain errors into one HTTP envelope.

Every refusal carries the same body shape, so a client never has to guess
whether an error has a code. The status is taken from the exception, which is
derived from `FailureCode` - the same table the recorded failed event uses, so
the HTTP response and the log cannot disagree about what happened.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ethos.api.schemas import ErrorBody
from ethos.errors import EthosError
from ethos.runtime import to_plain

__all__ = ["install_error_handlers"]


def _error_code(exc: EthosError) -> str:
    """A recorded failure code is more specific than the exception class."""
    failure_code = getattr(exc, "failure_code", None)
    if failure_code is not None:
        return failure_code.value
    return exc.code.value


def _body(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    return {
        "error": ErrorBody(
            code=code, message=message, details=details or {}
        ).model_dump(mode="json")
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(EthosError)
    async def _ethos_error(_request: Request, exc: EthosError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_body(_error_code(exc), exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = {
            "errors": [
                {
                    "location": list(error.get("loc", ())),
                    "message": error.get("msg", ""),
                    "type": error.get("type", ""),
                }
                for error in exc.errors()
            ]
        }
        return JSONResponse(
            status_code=422,
            content=_body("VALIDATION_ERROR", "request did not validate", details),
        )

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # The message is not echoed: an unexpected exception may carry internals.
        return JSONResponse(
            status_code=500,
            content=_body("INTERNAL_ERROR", "internal error"),
        )
