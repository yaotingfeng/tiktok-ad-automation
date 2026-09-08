from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings
from app.core.errors import DomainError, domain_error_handler

FRONTEND_DIR = Path(__file__).parent / "frontend"


def custom_generate_unique_id(route: APIRoute) -> str:
    tag = route.tags[0] if route.tags else "api"
    return f"{tag}-{route.name}"


# Do not initialize inherited request telemetry: OAuth query strings contain
# credentials. Operational logs use app.core.logging's explicit safe fields.
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
)

app.add_exception_handler(DomainError, domain_error_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_HOST],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
def unknown_api_path() -> None:
    raise HTTPException(status_code=404, detail="Not Found")


if (FRONTEND_DIR / "index.html").is_file():
    app.frontend("/", directory=FRONTEND_DIR)
