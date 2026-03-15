from fastapi import APIRouter
from services.ipfs import proxy_ipfs_gateway

router = APIRouter()

router.add_api_route("/ipfs/{path:path}", proxy_ipfs_gateway, methods=["GET", "HEAD"], summary="IPFS Proxy", description="Proxy /ipfs/ requests to the local IPFS gateway.")
router.add_api_route("/ipns/{path:path}", proxy_ipfs_gateway, methods=["GET", "HEAD"], summary="IPNS Proxy", description="Proxy /ipns/ requests to the local IPFS gateway.")
