import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from core.config import settings
from core.state import lifespan
from core.logging import setup_logging
from core.exceptions import setup_exception_handlers
from core.middleware import RateLimitMiddleware

from routers import system, nostr, media_library, media_upload, finance, cloud, analytics, ipfs, identity, crowdfunding, geo, permits, robohash

# Setup logging
setup_logging()

# Initialize FastAPI app
app = FastAPI(lifespan=lifespan)

# Setup exception handlers
setup_exception_handlers(app)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

earth_path = settings.ZEN_PATH / "workspace" / "UPlanet" / "earth"
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
app.include_router(media_library.router)
app.include_router(media_upload.router)
app.include_router(finance.router)
app.include_router(cloud.router)
app.include_router(analytics.router)
app.include_router(ipfs.router)
app.include_router(identity.router)
app.include_router(crowdfunding.router)
app.include_router(geo.router)
app.include_router(permits.router)
app.include_router(robohash.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("54321:app", host="0.0.0.0", port=54321, reload=True)
