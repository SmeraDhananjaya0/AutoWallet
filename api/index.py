"""Vercel entrypoint: the AutoWallet FastAPI app, served for /api/* (see vercel.json)."""

from backend.main import app

__all__ = ["app"]
