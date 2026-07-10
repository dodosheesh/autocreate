import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.dialogue import DialogueParseError, parse_tagged_script

Resolution = Literal["480p", "720p", "1080p"]
Bitrate = Literal["standard", "high"]


# --- Models / caractéristiques ---


class CharacteristicCreate(BaseModel):
    label: str
    reference_image_url: str
    injection_hint: str
    always_include: bool = True
    priority: int = 0


class CharacteristicOut(CharacteristicCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class ModelCreate(BaseModel):
    name: str
    face_reference_url: str
    notes: str | None = None


class ModelOut(ModelCreate):
    id: uuid.UUID
    characteristics: list[CharacteristicOut] = []

    model_config = {"from_attributes": True}


# --- Banques d'assets ---


class OutfitCreate(BaseModel):
    image_url: str
    tags: list[str] = []
    weight: float = Field(default=1.0, ge=0)


class OutfitOut(OutfitCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class BackgroundCreate(OutfitCreate):
    pass


class BackgroundOut(OutfitOut):
    pass


class TemplateCreate(BaseModel):
    category: str
    template_text: str = Field(
        description="Slots : {outfit} {background} {characteristics} {dialogue} {caption}"
    )
    speaking: bool = False
    recommended_model: str | None = None
    default_duration_s: int | None = None
    default_resolution: str | None = None
    weight: float = Field(default=1.0, ge=0)


class TemplateOut(TemplateCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class DialogueLineCreate(BaseModel):
    category: str
    raw_text: str = Field(description="Lignes taggées [H]/[F]/[beat], ordre chronologique")
    weight: float = Field(default=1.0, ge=0)

    @field_validator("raw_text")
    @classmethod
    def _valid_tagged_script(cls, value: str) -> str:
        try:
            parse_tagged_script(value)
        except DialogueParseError as exc:
            raise ValueError(str(exc)) from exc
        return value


class DialogueLineOut(DialogueLineCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class CaptionCreate(BaseModel):
    category: str
    text: str
    weight: float = Field(default=1.0, ge=0)


class CaptionOut(CaptionCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class VoiceProfileCreate(BaseModel):
    label: str
    elevenlabs_voice_id: str
    gender: Literal["male", "female"]
    tag: str = Field(pattern=r"^[A-Z]$", description="Tag du script (H ou F)")


class VoiceProfileOut(VoiceProfileCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


# --- Jobs (Phase 1 : trigger manuel, une catégorie, prompt fourni) ---


class JobCreate(BaseModel):
    model_id: uuid.UUID
    category: str = "skit"
    prompt: str = Field(description="Prompt de scène ; slot {characteristics} optionnel")
    dialogue_script: str | None = Field(
        default=None, description="Script taggé [H]/[F] (utilisé en Phase 3 pour le voice-swap)"
    )
    count: int = Field(default=1, ge=1, le=100)
    resolution: Resolution = "720p"
    duration_s: int = Field(default=10, ge=3, le=30)
    bitrate: Bitrate = "standard"
    model_variant: str = "seedance_2.0"
    budget_cap_usd: float | None = None
    extra_reference_urls: list[str] = Field(
        default_factory=list, description="Refs additionnelles (outfit, background) — Phase 1"
    )


class BatchJobCreate(BaseModel):
    """Job batch Phase 2 : composition depuis les banques, N items par catégorie."""

    model_id: uuid.UUID
    counts_per_category: dict[str, int] = Field(
        description='Ex : {"skit": 20, "podcast": 10}', min_length=1
    )
    resolution: Resolution = "720p"
    duration_s: int = Field(default=10, ge=3, le=30)
    bitrate: Bitrate = "standard"
    model_variant: str = "seedance_2.0"
    music_url: str | None = Field(default=None, description="Piste mixée à l'assemblage")
    budget_cap_usd: float | None = None


class ItemOut(BaseModel):
    id: uuid.UUID
    category: str
    status: str
    qc_status: str
    filled_prompt: str
    seedance_task_id: str | None
    raw_video_url: str | None
    final_video_url: str | None
    item_estimated_cost: float | None
    error: str | None

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: uuid.UUID
    status: str
    counts_per_category: dict[str, int] = {}
    resolution: str
    duration_s: int
    bitrate: str
    model_variant: str
    budget_cap_usd: float | None
    estimated_cost_usd: float | None
    actual_cost_usd: float | None
    compose_shortfall: dict[str, int] = {}
    error: str | None = None
    items: list[ItemOut] = []

    model_config = {"from_attributes": True}


# --- Estimation live ---


class EstimateRequest(BaseModel):
    count: int = Field(ge=1)
    duration_s: float = Field(gt=0)
    resolution: Resolution = "720p"
    model_variant: str = "seedance_2.0"
    speaking: bool = False
    qc_success_rate: float | None = Field(default=None, gt=0, le=1)
    budget_usd: float | None = None


class EstimateResponse(BaseModel):
    gross_usd: float
    effective_usd: float
    qc_success_rate: float
    cost_per_delivered_video_usd: float
    max_videos_for_budget: int | None = None
    over_budget: bool | None = None
