"""Schéma complet (brief §4). Toutes les tables métier portent tenant_id
(single-tenant par défaut, non exposé côté UI pour l'instant)."""

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

DEFAULT_TENANT = "default"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Category(StrEnum):
    SKIT = "skit"
    STORYTELLING = "storytelling"
    SHOWING_BODY = "showing_body"
    MICRO_TROTTOIR = "micro_trottoir"
    PODCAST = "podcast"
    SNAPCHAT = "snapchat"


class JobStatus(StrEnum):
    PENDING = "pending"
    COMPOSING = "composing"
    DISPATCHED = "dispatched"
    BLOCKED_BUDGET = "blocked_budget"
    COMPLETED = "completed"
    FAILED = "failed"


class ItemStatus(StrEnum):
    COMPOSED = "composed"
    DISPATCHED = "dispatched"
    GENERATED = "generated"
    QC = "qc"
    VOICED = "voiced"
    ASSEMBLED = "assembled"
    DONE = "done"
    FAILED = "failed"


class QcStatus(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"  # Phase 1 : QC pas encore branché


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="owner")  # owner / member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Incrémenté à chaque changement de mot de passe → invalide tous les jetons
    # de session émis avant (révocation effective, pas seulement côté client).
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Model(Base):
    __tablename__ = "models"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    name: Mapped[str] = mapped_column(String(255))
    face_reference_url: Mapped[str] = mapped_column(Text)  # R2 — la photo visage unique
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    characteristics: Mapped[list["ModelCharacteristic"]] = relationship(
        back_populates="model", order_by="ModelCharacteristic.priority"
    )


class ModelCharacteristic(Base):
    """Traits distinctifs à retrouver dans chaque génération (brief §4.2)."""

    __tablename__ = "model_characteristics"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id"), index=True)
    label: Mapped[str] = mapped_column(String(255))
    reference_image_url: Mapped[str] = mapped_column(Text)  # photo de CE trait précis
    injection_hint: Mapped[str] = mapped_column(Text)  # description intégrable au prompt
    always_include: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    model: Mapped[Model] = relationship(back_populates="characteristics")


class Outfit(Base):
    __tablename__ = "outfits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    image_url: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    # ready | pending | failed — auto-description vision en cours = pending
    status: Mapped[str] = mapped_column(String(16), default="ready")
    error: Mapped[str | None] = mapped_column(Text)  # raison si status=failed


class Background(Base):
    __tablename__ = "backgrounds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    image_url: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(16), default="ready")
    error: Mapped[str | None] = mapped_column(Text)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    # Slots : {setting} {outfit} {background} {action} {camera} {mood}
    #         {characteristics} {dialogue} {caption}
    template_text: Mapped[str] = mapped_column(Text)
    speaking: Mapped[bool] = mapped_column(Boolean, default=False)
    recommended_model: Mapped[str | None] = mapped_column(String(64))
    default_duration_s: Mapped[int | None] = mapped_column(Integer)
    default_resolution: Mapped[str | None] = mapped_column(String(16))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    # Reverse-engineering vidéo : ready | pending | failed. Un template pending
    # (reverse-eng en cours) n'est pas tiré à la composition.
    status: Mapped[str] = mapped_column(String(16), default="ready")
    source_video_url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class DialogueLine(Base):
    """Pool fourni par l'utilisateur — texte taggé [H]/[F] ligne par ligne (§7.1)."""

    __tablename__ = "dialogue_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float, default=1.0)


class Caption(Base):
    """Pool fourni par l'utilisateur (barre de texte façon Snapchat, etc.)."""

    __tablename__ = "captions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float, default=1.0)


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    label: Mapped[str] = mapped_column(String(255))
    elevenlabs_voice_id: Mapped[str] = mapped_column(String(64))
    gender: Mapped[str] = mapped_column(String(8))  # male / female
    tag: Mapped[str] = mapped_column(String(8))  # H / F

    __table_args__ = (UniqueConstraint("tenant_id", "tag", name="uq_voice_tag_per_tenant"),)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id"))
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING)
    counts_per_category: Mapped[dict] = mapped_column(JSON, default=dict)
    resolution: Mapped[str] = mapped_column(String(16), default="720p")
    duration_s: Mapped[int] = mapped_column(Integer, default=10)
    bitrate: Mapped[str] = mapped_column(String(16), default="standard")  # standard / high
    aspect: Mapped[str] = mapped_column(String(8), default="9:16")
    model_variant: Mapped[str] = mapped_column(String(64), default="seedance_2.0")
    music_url: Mapped[str | None] = mapped_column(Text)  # piste mixée à l'assemblage
    budget_cap_usd: Mapped[float | None] = mapped_column(Float)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    actual_cost_usd: Mapped[float | None] = mapped_column(Float)
    # Combos uniques épuisés : {category: nb manquant} — jamais de cap silencieux
    compose_shortfall: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    items: Mapped[list["JobItem"]] = relationship(back_populates="job")


class JobItem(Base):
    """Une ligne par variante générée."""

    __tablename__ = "job_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("prompt_templates.id"))
    outfit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outfits.id"))
    background_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("backgrounds.id"))
    dialogue_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dialogue_lines.id"))
    caption_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("captions.id"))
    caption_text: Mapped[str | None] = mapped_column(Text)
    characteristic_ids: Mapped[list] = mapped_column(JSON, default=list)
    combo_hash: Mapped[str] = mapped_column(String(64), index=True)
    filled_prompt: Mapped[str] = mapped_column(Text)
    dialogue_script: Mapped[str | None] = mapped_column(Text)  # taggé [H]/[F]
    reference_image_urls: Mapped[list] = mapped_column(JSON, default=list)
    seedance_task_id: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_video_url: Mapped[str | None] = mapped_column(Text)
    qc_status: Mapped[str] = mapped_column(String(16), default=QcStatus.PENDING)
    face_match_score: Mapped[float | None] = mapped_column(Float)
    final_video_url: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.PENDING)
    item_estimated_cost: Mapped[float | None] = mapped_column(Float)
    item_actual_cost: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default=ItemStatus.COMPOSED)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[GenerationJob] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("job_id", "combo_hash", name="uq_combo_per_job"),)


class PromptStatus(StrEnum):
    PENDING = "pending"  # reverse-engineering en cours
    READY = "ready"
    FAILED = "failed"


class PicturePrompt(Base):
    """Prompt reverse-engineeré depuis une image de référence uploadée,
    sauvegardé à vie pour réutilisation (feature Pictures / nano banana)."""

    __tablename__ = "picture_prompts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    source_image_url: Mapped[str] = mapped_column(Text)  # l'upload de référence (R2)
    prompt_text: Mapped[str | None] = mapped_column(Text)  # rempli par le reverse-engineering
    status: Mapped[str] = mapped_column(String(16), default=PromptStatus.PENDING)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PictureJob(Base):
    __tablename__ = "picture_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT, index=True)
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id"))
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING)
    count: Mapped[int] = mapped_column(Integer, default=1)
    image_size: Mapped[str] = mapped_column(String(16), default="1:1")  # aspect ratio
    output_format: Mapped[str] = mapped_column(String(8), default="png")
    model_variant: Mapped[str] = mapped_column(String(64), default="nano_banana")
    budget_cap_usd: Mapped[float | None] = mapped_column(Float)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    actual_cost_usd: Mapped[float | None] = mapped_column(Float)
    compose_shortfall: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    items: Mapped[list["PictureItem"]] = relationship(back_populates="job")


class PictureItem(Base):
    __tablename__ = "picture_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("picture_jobs.id"), index=True)
    prompt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("picture_prompts.id"))
    outfit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outfits.id"))
    characteristic_ids: Mapped[list] = mapped_column(JSON, default=list)
    combo_hash: Mapped[str] = mapped_column(String(64), index=True)
    filled_prompt: Mapped[str] = mapped_column(Text)
    reference_image_urls: Mapped[list] = mapped_column(JSON, default=list)
    kie_task_id: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_image_url: Mapped[str | None] = mapped_column(Text)  # sortie brute nano banana
    qc_status: Mapped[str] = mapped_column(String(16), default=QcStatus.PENDING)
    face_match_score: Mapped[float | None] = mapped_column(Float)
    final_image_url: Mapped[str | None] = mapped_column(Text)  # scrubbée, prête au DL
    review_status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.PENDING)
    item_estimated_cost: Mapped[float | None] = mapped_column(Float)
    item_actual_cost: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default=ItemStatus.COMPOSED)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[PictureJob] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("job_id", "combo_hash", name="uq_pic_combo_per_job"),)


class Pricing(Base):
    """Source de vérité tarifaire — jamais hardcodée (brief §4.11).
    Synchronisée depuis kie.ai/pricing."""

    __tablename__ = "pricing"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    model: Mapped[str] = mapped_column(String(64))
    resolution: Mapped[str | None] = mapped_column(String(16))  # None pour les rates audio
    with_ref: Mapped[bool | None] = mapped_column(Boolean)
    unit: Mapped[str] = mapped_column(String(16))  # per_sec / per_min
    rate_usd: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("model", "resolution", "with_ref", "unit", name="uq_pricing_key"),
    )


class CalibrationLog(Base):
    """Réconciliation estimé vs réel → recalibrage auto du taux QC (brief §4.12)."""

    __tablename__ = "calibration_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id"))
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    qc_success_rate: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
