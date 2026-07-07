import time
import uuid
import logging
import threading
import ipaddress
from collections import deque
from typing import Optional, Dict, Any
from cachetools import TTLCache
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import settings
from core.logging import request_id_var
from utils.observability import log_node_event

logger = logging.getLogger(__name__)

def is_trusted_ip(ip: str) -> bool:
    if ip in settings.TRUSTED_IPS:
        return True
    try:
        ip_obj = ipaddress.ip_address(ip)
        for cidr in settings.TRUSTED_IP_RANGES:
            if ip_obj in ipaddress.ip_network(cidr):
                return True
    except Exception:
        pass
    return False

class RateLimiter:
    def __init__(self):
        # TTLCache borne la mémoire (maxsize IPs max) et expire automatiquement
        # les IPs inactives après RATE_LIMIT_WINDOW secondes — sans cleanup manuel.
        self.requests: TTLCache = TTLCache(maxsize=10_000, ttl=settings.RATE_LIMIT_WINDOW)
        self.lock = threading.Lock()

    def is_allowed(self, ip: str) -> bool:
        current_time = time.time()
        with self.lock:
            timestamps = self.requests.get(ip, deque())
            while timestamps and timestamps[0] < current_time - settings.RATE_LIMIT_WINDOW:
                timestamps.popleft()

            if len(timestamps) < settings.RATE_LIMIT_REQUESTS:
                timestamps.append(current_time)
                self.requests[ip] = timestamps  # réinitialise le TTL à chaque requête
                return True
            return False

    def get_remaining_requests(self, ip: str) -> int:
        current_time = time.time()
        with self.lock:
            timestamps = self.requests.get(ip, deque())
            while timestamps and timestamps[0] < current_time - settings.RATE_LIMIT_WINDOW:
                timestamps.popleft()
            return max(0, settings.RATE_LIMIT_REQUESTS - len(timestamps))

    def get_reset_time(self, ip: str) -> Optional[float]:
        with self.lock:
            timestamps = self.requests.get(ip, deque())
            if not timestamps:
                return None
            return timestamps[0] + settings.RATE_LIMIT_WINDOW

rate_limiter = RateLimiter()

def get_client_ip(request: Request) -> str:
    client_host = request.client.host if request.client else None
    if client_host and is_trusted_ip(client_host):
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
    return client_host or "unknown"

def check_rate_limit(request: Request) -> Dict[str, Any]:
    client_ip = get_client_ip(request)
    
    if is_trusted_ip(client_ip):
        return {
            "remaining_requests": float('inf'),
            "reset_time": None,
            "client_ip": client_ip,
            "trusted": True
        }
    
    if not rate_limiter.is_allowed(client_ip):
        reset_time = rate_limiter.get_reset_time(client_ip)
        remaining_time = int(reset_time - time.time()) if reset_time else 0
        
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                "message": f"Too many requests. Limit: {settings.RATE_LIMIT_REQUESTS} requests per minute.",
                "remaining_time": remaining_time,
                "reset_time": reset_time,
                "client_ip": client_ip,
                "trusted": False
            }
        )
    
    return {
        "remaining_requests": rate_limiter.get_remaining_requests(client_ip),
        "reset_time": rate_limiter.get_reset_time(client_ip),
        "client_ip": client_ip,
        "trusted": False
    }

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Injecter un request ID unique pour corrélation dans tous les logs
        req_id = str(uuid.uuid4())[:8]
        token = request_id_var.set(req_id)

        is_static = request.url.path.startswith("/static")
        start = time.perf_counter()

        try:
            if not is_static:
                client_ip = get_client_ip(request)
                logger.debug(
                    "→ %s %s client=%s",
                    request.method, request.url.path, client_ip,
                )

            if is_static:
                response = await call_next(request)
                return response

            try:
                rate_info = check_rate_limit(request)
            except HTTPException as e:
                if e.status_code == 429:
                    elapsed = (time.perf_counter() - start) * 1000
                    logger.warning(
                        "← 429 RATE_LIMIT %s %s ip=%s (%.0fms)",
                        request.method, request.url.path,
                        e.detail.get("client_ip", "?"), elapsed,
                    )
                    # Observabilité NODE : visibilité station-wide sur les 4xx/5xx,
                    # additive, sans effet sur la réponse HTTP (échoue toujours
                    # silencieusement). Ring buffer partagé avec bro_log_event()/
                    # nip101_log_event() — on ne journalise QUE les erreurs ici pour
                    # ne pas noyer le digest sous le trafic 2xx normal.
                    log_node_event(
                        f"{request.method} {request.url.path}", False,
                        category="http_error", latency_ms=elapsed,
                        extra={"status": 429, "ip": e.detail.get("client_ip", "?")},
                    )
                    response = JSONResponse(status_code=429, content=e.detail)
                    response.headers["X-RateLimit-Limit"] = str(settings.RATE_LIMIT_REQUESTS)
                    response.headers["X-RateLimit-Remaining"] = "0"
                    if e.detail.get("reset_time"):
                        response.headers["X-RateLimit-Reset"] = str(int(e.detail["reset_time"]))
                    response.headers["X-RateLimit-Client-IP"] = e.detail.get("client_ip", "unknown")
                    return response
                raise e

            response = await call_next(request)
            elapsed = (time.perf_counter() - start) * 1000

            response.headers["X-RateLimit-Limit"] = str(settings.RATE_LIMIT_REQUESTS)
            if rate_info.get("trusted", False):
                response.headers["X-RateLimit-Remaining"] = "unlimited"
            else:
                response.headers["X-RateLimit-Remaining"] = str(rate_info["remaining_requests"])
            if rate_info["reset_time"]:
                response.headers["X-RateLimit-Reset"] = str(int(rate_info["reset_time"]))
            response.headers["X-RateLimit-Client-IP"] = rate_info["client_ip"]

            status = response.status_code
            log_fn = logger.warning if status >= 400 else logger.info
            log_fn(
                "← %d %s %s ip=%s (%.0fms)",
                status, request.method, request.url.path,
                rate_info["client_ip"], elapsed,
            )
            if status >= 400:
                # Idem : uniquement les erreurs, pour préserver le signal du
                # digest NODE partagé (ring buffer 200 lignes).
                log_node_event(
                    f"{request.method} {request.url.path}", False,
                    category="http_error", latency_ms=elapsed,
                    extra={"status": status, "ip": rate_info["client_ip"]},
                )
            return response

        finally:
            request_id_var.reset(token)
