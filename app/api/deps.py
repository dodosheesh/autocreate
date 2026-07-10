"""Dépendances d'authentification FastAPI."""

import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import User
from app.services.security import read_session


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """401 si pas de cookie de session valide."""
    token = request.cookies.get(get_settings().session_cookie, "")
    user_id = read_session(token)
    if not user_id:
        raise HTTPException(401, "Authentification requise")
    try:
        user = db.get(User, uuid.UUID(user_id))
    except ValueError:
        raise HTTPException(401, "Session invalide")
    if user is None or not user.is_active:
        raise HTTPException(401, "Session invalide")
    return user
