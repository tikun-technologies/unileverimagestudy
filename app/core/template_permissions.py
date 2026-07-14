"""Authorized emails for layer template management."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.core.config import settings
from app.core.dependencies import get_current_active_user
from app.models.user_model import User


def get_template_manager_emails() -> set[str]:
    """
    Return the set of emails allowed to manage templates.
    Configured via TEMPLATE_MANAGER_EMAILS (comma-separated) in settings/.env.
    """
    raw = (settings.TEMPLATE_MANAGER_EMAILS or "").strip()
    if not raw:
        return set()
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def can_manage_templates(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in get_template_manager_emails()


def require_template_manager(current_user: User = Depends(get_current_active_user)) -> User:
    """Dependency: only allow configured template managers."""
    if not can_manage_templates(getattr(current_user, "email", None)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage templates.",
        )
    return current_user
