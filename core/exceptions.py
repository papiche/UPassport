from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import logging

async def http_exception_handler(request: Request, exc: HTTPException):
    logging.error(f"HTTPException: {exc.detail} at {request.url}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "status": "error"}
    )

async def validation_exception_handler(request: Request, exc: ValidationError):
    logging.error(f"ValidationError: {exc.errors()} at {request.url}")
    return JSONResponse(
        status_code=422,
        content={"error": "Validation Error", "details": exc.errors(), "status": "error"}
    )

async def general_exception_handler(request: Request, exc: Exception):
    logging.error(f"Unhandled Exception: {str(exc)} at {request.url}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "status": "error"}
    )

def setup_exception_handlers(app):
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(ValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)
