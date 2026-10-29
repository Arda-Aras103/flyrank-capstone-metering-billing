"""FastAPI application entrypoint with boundary error handlers and startup migration/seeding."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.db.database import SessionLocal, run_migrations
from app.seed import seed_demo_data
from app.services.meter_service import BillingError


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ensure schema migrations and baseline plans/tenants exist on startup."""
    run_migrations()
    with SessionLocal() as db:
        seed_demo_data(db)
    yield


app = FastAPI(
    title="Usage Metering & Billing Engine",
    description=(
        "Idempotent usage metering, strict quota enforcement (429/402), integer micro-cent AI token "
        "pricing, and signature-verified Stripe test-mode webhook synchronization."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(BillingError)
async def billing_error_handler(_request: Request, exc: BillingError) -> JSONResponse:
    """Return structured, machine-readable 4xx responses (400, 402, 404, 409, 429)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Ensure bad input at the HTTP boundary always returns a clean 422 response, never a 500."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Invalid request payload or parameters.",
            "details": exc.errors(),
        },
    )


app.include_router(router)
