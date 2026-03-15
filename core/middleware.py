import time
import logging
import threading
import ipaddress
from collections import defaultdict, deque
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import settings

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
        self.requests = defaultdict(deque)
        self.lock = threading.Lock()
        self.last_cleanup = time.time()
    
    def is_allowed(self, ip: str) -> bool:
        current_time = time.time()
        with self.lock:
            if current_time - self.last_cleanup > settings.RATE_LIMIT_CLEANUP_INTERVAL:
                self._cleanup_old_entries(current_time)
                self.last_cleanup = current_time
            
            timestamps = self.requests[ip]
            while timestamps and timestamps[0] < current_time - settings.RATE_LIMIT_WINDOW:
                timestamps.popleft()
            
            if len(timestamps) < settings.RATE_LIMIT_REQUESTS:
                timestamps.append(current_time)
                return True
            return False
    
    def get_remaining_requests(self, ip: str) -> int:
        current_time = time.time()
        with self.lock:
            timestamps = self.requests[ip]
            while timestamps and timestamps[0] < current_time - settings.RATE_LIMIT_WINDOW:
                timestamps.popleft()
            return max(0, settings.RATE_LIMIT_REQUESTS - len(timestamps))
    
    def get_reset_time(self, ip: str) -> Optional[float]:
        with self.lock:
            timestamps = self.requests[ip]
            if not timestamps:
                return None
            return timestamps[0] + settings.RATE_LIMIT_WINDOW
    
    def _cleanup_old_entries(self, current_time: float):
        cutoff_time = current_time - settings.RATE_LIMIT_WINDOW
        ips_to_remove = []
        for ip, timestamps in self.requests.items():
            while timestamps and timestamps[0] < cutoff_time:
                timestamps.popleft()
            if not timestamps:
                ips_to_remove.append(ip)
        
        for ip in ips_to_remove:
            del self.requests[ip]

rate_limiter = RateLimiter()

def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"

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
        if request.url.path.startswith("/static"):
            return await call_next(request)
        
        try:
            rate_info = check_rate_limit(request)
            response = await call_next(request)
            
            response.headers["X-RateLimit-Limit"] = str(settings.RATE_LIMIT_REQUESTS)
            if rate_info.get("trusted", False):
                response.headers["X-RateLimit-Remaining"] = "unlimited"
            else:
                response.headers["X-RateLimit-Remaining"] = str(rate_info["remaining_requests"])
            
            if rate_info["reset_time"]:
                response.headers["X-RateLimit-Reset"] = str(int(rate_info["reset_time"]))
            response.headers["X-RateLimit-Client-IP"] = rate_info["client_ip"]
            
            return response
            
        except HTTPException as e:
            if e.status_code == 429:
                response = JSONResponse(status_code=429, content=e.detail)
                response.headers["X-RateLimit-Limit"] = str(settings.RATE_LIMIT_REQUESTS)
                response.headers["X-RateLimit-Remaining"] = "0"
                if e.detail.get("reset_time"):
                    response.headers["X-RateLimit-Reset"] = str(int(e.detail["reset_time"]))
                response.headers["X-RateLimit-Client-IP"] = e.detail.get("client_ip", "unknown")
                return response
            raise e
