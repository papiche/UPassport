import logging
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

logger = logging.getLogger(__name__)


async def http_exception_handler(request: Request, exc: HTTPException):
    # 4xx → WARNING (erreur client attendue), 5xx → ERROR
    if exc.status_code >= 500:
        logger.error("HTTP %d %s %s — %s", exc.status_code, request.method, request.url, exc.detail)
    elif exc.status_code == 429:
        pass  # déjà loggé par RateLimitMiddleware
    else:
        logger.warning("HTTP %d %s %s — %s", exc.status_code, request.method, request.url, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "status": "error"},
    )


async def validation_exception_handler(request: Request, exc: ValidationError):
    logger.warning("Validation error %s %s — %s", request.method, request.url, exc.errors())
    return JSONResponse(
        status_code=422,
        content={"error": "Validation Error", "details": exc.errors(), "status": "error"},
    )


async def general_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception %s %s — %s", request.method, request.url, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "status": "error"},
    )


def setup_exception_handlers(app) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(ValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)
