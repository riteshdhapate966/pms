from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Literal

import httpx
from fastapi import FastAPI, Path, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.htng import HtngClient
from app.lifecycle import Lifecycle
from app.lifecycle import router as lifecycle_router
from app.pms import PmsClient, PmsError, mock_handler
from app.stay import StayHarness
from app.stay import router as stay_router


def create_app(settings: Settings | None = None, transport=None, soap_transport=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings()
        selected_transport = transport
        if selected_transport is None and config.mode == "mock":
            selected_transport = httpx.MockTransport(mock_handler)
        # Mock mode always uses a fixed URL, even if .env contains a live URL.
        base_url = "http://mock-pms/WebAPI/api/" if config.mode == "mock" else config.base_url
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Accept": "application/json",
                config.client_id_header: config.client_id.get_secret_value(),
                config.auth_code_header: config.auth_code.get_secret_value(),
            },
            timeout=config.timeout_seconds,
            transport=selected_transport,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            app.state.config = config
            app.state.pms = PmsClient(http, config.property_code, config.source_id)
            app.state.stay = StayHarness(config)
            app.state.htng = HtngClient(config, soap_transport)
            app.state.lifecycle = Lifecycle(config, app.state.stay)
            yield

    app = FastAPI(
        title="WISH PMS API Test Project",
        version="0.1.0",
        description="Rufescent stay trials: JSON and SOAP reads, reservation previews and local simulation.",
        lifespan=lifespan,
    )

    @app.exception_handler(PmsError)
    async def pms_error(request: Request, error: PmsError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.message})

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(FilePath(__file__).resolve().parent / "static" / "index.html")

    @app.get("/health", tags=["Health"])
    async def health(request: Request):
        """Application health only; does not check connectivity to WISH."""
        return {"status": "ok", "mode": request.app.state.config.mode}

    @app.get("/api/masters/{kind}", tags=["Masters"])
    async def masters(request: Request, kind: Literal["PROPERTY_INFO", "ROOM_TYPE"]):
        """Translates this local GET into the vendor's POST getMasters request."""
        return await request.app.state.pms.masters(kind)

    @app.get("/api/guests/{guest_id}/{operation}", tags=["Guests"])
    async def guest(
        request: Request,
        operation: Literal["booking-summary", "personal-details", "folio"],
        guest_id: str = Path(min_length=1, max_length=100, pattern=r"^\S+$"),
    ):
        operations = {
            "booking-summary": "getbookingsummary",
            "personal-details": "getpersonaldetails",
            "folio": "getfolio",
        }
        return await request.app.state.pms.guest(operations[operation], guest_id)

    app.include_router(stay_router)
    app.include_router(lifecycle_router)
    app.mount(
        "/static",
        StaticFiles(directory=FilePath(__file__).resolve().parent / "static"),
        name="static",
    )
    return app


app = create_app()
