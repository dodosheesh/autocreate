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
    payload = read_session(token)
    if not payload:
        raise HTTPException(401, "Authentification requise")
    try:
        user = db.get(User, uuid.UUID(payload["uid"]))
    except (ValueError, KeyError):
        raise HTTPException(401, "Session invalide")
    if user is None or not user.is_active:
        raise HTTPException(401, "Session invalide")
    # Jeton émis avant le dernier changement de mot de passe → révoqué
    if payload.get("ver", 0) != user.token_version:
        raise HTTPException(401, "Session expirée")
    return user


def require_owner(user: User = Depends(current_user)) -> User:
    """403 si l'utilisateur n'est pas propriétaire (gestion d'équipe réservée)."""
    if user.role != "owner":
        raise HTTPException(403, "Action réservée au propriétaire du compte")
    return user
