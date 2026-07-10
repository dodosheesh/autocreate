from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.config import get_settings
from app.db.base import get_db
from app.db.models import User
from app.services.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    issue_session,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserOut(BaseModel):
    email: str
    role: str

    model_config = {"from_attributes": True}


def _set_session_cookie(response: Response, user: User) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie,
        issue_session(str(user.id), user.token_version),
        max_age=settings.session_ttl_s,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


@router.post("/login", response_model=UserOut)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    # verify_password sur un hash factice AU MÊME COÛT (240k rounds) si l'user
    # n'existe pas → latence identique, pas d'énumération de comptes par timing.
    stored = user.password_hash if user else DUMMY_PASSWORD_HASH
    if not verify_password(payload.password, stored) or user is None or not user.is_active:
        raise HTTPException(401, "Identifiants invalides")
    _set_session_cookie(response, user)
    return user


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(get_settings().session_cookie)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.post("/change-password", response_model=UserOut)
def change_password(
    payload: PasswordChange,
    response: Response,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(403, "Mot de passe actuel incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(422, "Nouveau mot de passe : 8 caractères minimum")
    user.password_hash = hash_password(payload.new_password)
    # Invalide TOUS les jetons émis avant (y compris ceux d'un attaquant sur
    # un autre appareil) puis ré-émet un cookie valide pour la session courante.
    user.token_version += 1
    db.commit()
    _set_session_cookie(response, user)
    return user
