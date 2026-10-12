import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for errors that map cleanly to an HTTP response.

    Response body: ``{"detail": <message>, "code": <code>}`` — compatible with
    FastAPI's default ``{"detail": ...}`` shape.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "Bad request"
    headers: dict[str, str] | None = None

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.message = message or self.message
        self.code = code or self.code
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"
    message = "Bad request"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Resource already exists"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    message = "Could not validate credentials"
    headers = {"WWW-Authenticate": "Bearer"}  # noqa: RUF012


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "Not enough permissions"


class UnprocessableError(AppError):
    """The request is well-formed JSON but its content is invalid (e.g. the LLM service
    rejected a trip-plan form). Shape matches our other errors, not FastAPI's 422 list."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "Invalid request"


class UpstreamError(AppError):
    """A service we depend on (e.g. the LLM service) is down, slow, or returned an error."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "upstream_unavailable"
    message = "Upstream service unavailable"


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code},
        headers=exc.headers,
    )


async def _database_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Lost the database connection mid-request: say so, as a 503 the client can retry.

    Registered for `DBAPIError` so it runs inside the CORS middleware — the generic
    `Exception` handler runs outside it, and a browser would then see a bare network error.
    SQLAlchemy has already dropped the dead connections, so a retry gets a fresh one.
    """
    assert isinstance(exc, DBAPIError)
    if not (exc.connection_invalidated or isinstance(exc, (InterfaceError, OperationalError))):
        return await _unhandled_error_handler(request, exc)
    logger.warning(
        "Database connection lost on %s %s: %s", request.method, request.url.path, exc.orig
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "Cơ sở dữ liệu tạm thời không phản hồi, vui lòng thử lại",
            "code": "database_unavailable",
        },
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error", "code": "internal_error"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(DBAPIError, _database_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
