import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from .api import router
from .config import settings
from .database import Base, SessionLocal, engine, mongo_client
from .migrations import apply_startup_migrations
from .realtime import hub
from .retention import apply_retention, retention_days


@asynccontextmanager
async def lifespan(_: FastAPI):
    required_secrets = {
        "JWT_SECRET_KEY": settings.jwt_secret_key,
        "COLLECTOR_API_KEY": settings.collector_api_key,
        "BOOTSTRAP_TOKEN": settings.bootstrap_token,
    }
    missing_secrets = [name for name, value in required_secrets.items() if len(value) < 32]
    if missing_secrets:
        raise RuntimeError(f"Configure secrets of at least 32 characters: {', '.join(missing_secrets)}")

    Base.metadata.create_all(bind=engine)
    apply_startup_migrations(engine)
    hub.bind_loop(asyncio.get_running_loop())
    try:
        mongo_client.admin.command("ping")
        database = mongo_client[settings.mongodb_database]
        for collection_name in ("metrics", "security_events", "scan_results"):
            database[collection_name].create_index("timestamp")
            database[collection_name].create_index([("host", 1), ("timestamp", -1)])
        database["scan_results"].create_index([("scan_type", 1), ("timestamp", -1)])
        with SessionLocal() as session:
            configured_days = retention_days(session)
        apply_retention(database, configured_days)
    except Exception as exc:  # the API still serves HTTP 503s for Mongo routes
        print(f"MongoDB startup work skipped: {type(exc).__name__}: {exc}", flush=True)
    yield
    mongo_client.close()


app = FastAPI(
    title="Server Health & Security Monitor API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, __: PyMongoError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Time-series store is unavailable"},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Collector-Key", "X-Bootstrap-Token"],
    # The login form reads Retry-After to say how long a throttle or lock lasts.
    expose_headers=["Retry-After"],
)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        mongo_client.admin.command("ping")
        mongo_status = "ok"
    except Exception:
        mongo_status = "unavailable"
    return {"status": "ok", "mongodb": mongo_status}