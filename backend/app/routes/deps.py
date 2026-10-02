"""
Shared request helpers.
"""

from fastapi import Request

from ..security import ratelimit


def client_ip(request: Request) -> str:
    # Behind a trusted proxy, replace with the proxy-provided address.
    return request.client.host if request.client else "unknown"


def limit(bucket: str, key: str):
    # RateLimited is turned into a 429 JSON response by the handler
    # in main.py.
    ratelimit.hit(bucket, key)
