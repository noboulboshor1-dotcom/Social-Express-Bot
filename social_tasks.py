"""Compatibility shim: social-task routes are proof-only; no credentials are collected."""
from proof_tasks import proof_router as social_router

__all__ = ["social_router"]
