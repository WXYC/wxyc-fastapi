"""HTTP utilities: async-singleton helper for race-free lazy clients."""

from wxyc_fastapi.http.singleton import async_singleton

__all__ = ["async_singleton"]
