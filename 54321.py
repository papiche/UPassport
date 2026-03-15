import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from core.config import settings
from core.state import lifespan
from core.logging import setup_logging
from core.exceptions import setup_exception_handlers
from core.middleware import RateLimitMiddleware

from routers import system, nostr, media, finance, cloud
from services.ipfs import proxy_ipfs_gateway

# Setup logging
setup_logging()

# Initialize FastAPI app
app = FastAPI(lifespan=lifespan)

# Setup exception handlers
setup_exception_handlers(app)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

earth_path = os.path.expanduser("~/.zen/workspace/UPlanet/earth")
if os.path.exists(earth_path):
    app.mount("/earth", StaticFiles(directory=earth_path), name="earth")

# Add Rate Limiting Middleware
app.add_middleware(RateLimitMiddleware)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(system.router)
app.include_router(nostr.router)
app.include_router(media.router)
app.include_router(finance.router)
app.include_router(cloud.router)

# IPFS proxy routes
app.add_api_route("/ipfs/{path:path}", proxy_ipfs_gateway, methods=["GET", "HEAD"])
app.add_api_route("/ipns/{path:path}", proxy_ipfs_gateway, methods=["GET", "HEAD"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("54321:app", host=settings.HOST, port=settings.PORT, reload=True)
