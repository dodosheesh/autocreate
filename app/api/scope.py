"""Helpers d'isolation multi-tenant.

Chaque ressource métier porte `tenant_id`. On estampille à la création avec
le tenant de l'utilisateur courant, et on filtre toute lecture/écriture par ce
tenant. Un accès à une ressource d'un autre tenant renvoie 404 (on ne révèle
pas son existence).

Les tables enfants (model_characteristics, job_items, picture_items) n'ont pas
de `tenant_id` propre : leur isolation passe par le parent (Model, GenerationJob,
PictureJob), qu'on charge toujours de façon scopée d'abord.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User


def owned(db: Session, entity_cls, entity_id: uuid.UUID, user: User):
    """Charge une entité du tenant de l'utilisateur, sinon 404."""
    entity = db.get(entity_cls, entity_id)
    if entity is None or entity.tenant_id != user.tenant_id:
        raise HTTPException(404, f"{entity_cls.__name__} introuvable")
    return entity


def tenant_query(entity_cls, user: User):
    """SELECT filtré sur le tenant de l'utilisateur."""
    return select(entity_cls).where(entity_cls.tenant_id == user.tenant_id)
