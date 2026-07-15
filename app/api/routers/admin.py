"""Gestion d'équipe (réservée au propriétaire).

Le propriétaire crée les comptes des membres de son équipe ; il n'existe AUCUNE
inscription publique, donc seuls les comptes créés ici peuvent se connecter — un
inconnu ne peut pas accéder au tool. Les membres partagent le même tenant que le
propriétaire (même espace de travail : modèles, banques, jobs, coûts).
"""

import secrets
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_owner
from app.db.base import get_db
from app.db.models import User
from app.services.security import hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])


class MemberCreate(BaseModel):
    email: EmailStr
    # Optionnel : si vide, un mot de passe fort est généré et renvoyé UNE fois.
    password: str | None = Field(default=None, min_length=8)


class MemberOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class MemberCreated(BaseModel):
    member: MemberOut
    # Mot de passe initial à transmettre au membre (affiché une seule fois).
    generated_password: str | None = None


def _member_in_tenant(db: Session, member_id: uuid.UUID, owner: User) -> User:
    member = db.get(User, member_id)
    if member is None or member.tenant_id != owner.tenant_id:
        raise HTTPException(404, "Membre introuvable")
    return member


@router.get("/members", response_model=list[MemberOut])
def list_members(owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    """Tous les comptes de l'espace de travail (propriétaire + membres)."""
    return db.scalars(
        select(User).where(User.tenant_id == owner.tenant_id).order_by(User.created_at)
    ).all()


@router.post("/members", response_model=MemberCreated)
def create_member(
    payload: MemberCreate,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Crée un compte membre (rôle `member`) dans l'espace du propriétaire."""
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Un compte existe déjà avec cet e-mail")
    password = payload.password or secrets.token_urlsafe(12)
    member = User(
        email=email,
        password_hash=hash_password(password),
        role="member",
        tenant_id=owner.tenant_id,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    # On ne renvoie le mot de passe QUE s'il a été généré (l'owner l'a sinon déjà).
    return MemberCreated(
        member=MemberOut.model_validate(member),
        generated_password=None if payload.password else password,
    )


@router.post("/members/{member_id}/reset-password", response_model=MemberCreated)
def reset_member_password(
    member_id: uuid.UUID,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Régénère un mot de passe pour un membre et révoque ses sessions en cours."""
    member = _member_in_tenant(db, member_id, owner)
    if member.id == owner.id:
        raise HTTPException(400, "Change ton propre mot de passe via ton profil")
    password = secrets.token_urlsafe(12)
    member.password_hash = hash_password(password)
    member.token_version += 1  # invalide toutes ses sessions actives
    db.commit()
    db.refresh(member)
    return MemberCreated(member=MemberOut.model_validate(member), generated_password=password)


@router.post("/members/{member_id}/deactivate", response_model=MemberOut)
def deactivate_member(
    member_id: uuid.UUID,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Coupe l'accès d'un membre (compte conservé). Révoque ses sessions."""
    member = _member_in_tenant(db, member_id, owner)
    if member.id == owner.id:
        raise HTTPException(400, "Tu ne peux pas te désactiver toi-même")
    member.is_active = False
    member.token_version += 1
    db.commit()
    db.refresh(member)
    return member


@router.post("/members/{member_id}/activate", response_model=MemberOut)
def activate_member(
    member_id: uuid.UUID,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Réactive l'accès d'un membre."""
    member = _member_in_tenant(db, member_id, owner)
    member.is_active = True
    db.commit()
    db.refresh(member)
    return member


@router.delete("/members/{member_id}")
def delete_member(
    member_id: uuid.UUID,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Supprime définitivement un compte membre (jamais un propriétaire ni soi)."""
    member = _member_in_tenant(db, member_id, owner)
    if member.id == owner.id:
        raise HTTPException(400, "Tu ne peux pas te supprimer toi-même")
    if member.role == "owner":
        raise HTTPException(400, "Impossible de supprimer un propriétaire")
    db.delete(member)
    db.commit()
    return {"ok": True}
