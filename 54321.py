import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from core.config import settings
from core.state import lifespan
from core.logging import setup_logging
from core.exceptions import setup_exception_handlers
from core.middleware import RateLimitMiddleware

# a2wsgi (PAS starlette.middleware.wsgi, déprécié depuis starlette 0.52) —
# exécute l'application WSGI WebDAV dans un pool de threads, donc sans figer la
# boucle d'événements uvicorn.
from a2wsgi import WSGIMiddleware
from services.cloud_storage import build_wsgi_dav_app

from routers import system, nostr, media_library, media_upload, finance, cloud, analytics, ipfs, identity, crowdfunding, geo, permits, robohash, feedback, qr, cookie, mailjet, skills, nostr_sign, zine, node_admin

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
    # html=True : /earth/ → /earth/index.html (et /earth/foo/ → /earth/foo/index.html)
    app.mount("/earth", StaticFiles(directory=earth_path, html=True), name="earth")

apk_path = settings.ZEN_PATH / "workspace" / "cabine-33" / "build" / "android"
if os.path.exists(apk_path):
    # /apk/atom4love.apk — APK ATOM4LOVE buildé par create_apk.sh via 20h12.process.sh
    app.mount("/apk", StaticFiles(directory=apk_path), name="apk")

zelkova_apk_path = settings.ZEN_PATH / "workspace" / "zelkova"
if os.path.exists(zelkova_apk_path):
    # /zelkova-apk/zelkova.apk — APK Ẑelkova téléchargé par 20h12.process.sh
    app.mount("/zelkova-apk", StaticFiles(directory=zelkova_apk_path), name="zelkova_apk")

# /dav — Cloud personnel chiffré du MULTIPASS (WebDAV RFC 4918).
# Auth : Basic `email:dav_token` (token délivré par POST /api/cloud/enroll) ou
# NIP-98 directe. Les fichiers sont chiffrés AES-256-GCM (format UENC) AVANT
# d'atteindre IPFS — cf. services/cloud_storage.py.
app.mount("/dav", WSGIMiddleware(build_wsgi_dav_app()), name="dav")

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

# Redirect /index.html → /earth/index.html (page d'accueil UPlanet)
# NB: GET / est géré par system.router (uStats JSON) — ne pas toucher
@app.get("/index.html", include_in_schema=False)
async def index_redirect():
    return RedirectResponse(url="/earth/index.html", status_code=302)

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
app.include_router(feedback.router)
app.include_router(qr.router)
app.include_router(cookie.router)
app.include_router(mailjet.router)
app.include_router(skills.router)
app.include_router(nostr_sign.router)
app.include_router(zine.router)
app.include_router(node_admin.router)

if __name__ == "__main__":
    import uvicorn
    # proxy_headers + forwarded_allow_ips="*" : la station est TOUJOURS servie
    # derrière Nginx Proxy Manager (SSL terminé côté NPM, HTTP en clair vers
    # uvicorn). Sans ça, uvicorn ne fait confiance à X-Forwarded-Proto que
    # depuis 127.0.0.1 (défaut) — en déploiement Docker, NPM parle à ce
    # process via le bridge dragon-net, PAS 127.0.0.1, donc scope["scheme"]
    # reste "http" ; toute redirection Starlette (ex. /dav → /dav/, trailing
    # slash) construit alors un Location http:// même pour un client HTTPS —
    # symptôme constaté : clients WebDAV (Nautilus/gvfs) qui refusent de
    # suivre un downgrade de schéma et échouent avec "Temporary Redirect".
    uvicorn.run("54321:app", host="0.0.0.0", port=54321, reload=False,
                proxy_headers=True, forwarded_allow_ips="*")
