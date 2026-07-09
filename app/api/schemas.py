import uuid
from typing import Literal

from pydantic import BaseModel, Field

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
    resolution: str
    duration_s: int
    bitrate: str
    model_variant: str
    budget_cap_usd: float | None
    estimated_cost_usd: float | None
    actual_cost_usd: float | None
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
