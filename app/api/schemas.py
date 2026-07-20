import uuid
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, Field, field_validator

from app.services.dialogue import (
    MAX_VOICE_SWITCHES,
    DialogueParseError,
    count_model_voice_switches,
    parse_tagged_script,
)

_VOICE_SWITCH_ERROR = (
    "Jamais plus d'UN changement de voix de la model par vidéo : réécris le "
    "script avec au plus une bascule [H]↔[F] (ex. tout en [H] puis la fin en [F])."
)


def _http_url(value: str) -> str:
    """Vérifie à l'entrée d'API que l'URL est bien http(s) avec un hôte
    (défense en profondeur, sans I/O réseau). Le filtrage anti-SSRF complet
    — résolution DNS + blocage des IP privées — se fait au téléchargement
    (app/net.safe_download), pour ne pas faire de DNS dans le handler."""
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL invalide : http(s) avec un hôte requis")
    return value


HttpUrlStr = Annotated[str, AfterValidator(_http_url)]

Resolution = Literal["480p", "720p", "1080p"]
Bitrate = Literal["standard", "high"]


# --- Models / caractéristiques ---


class CharacteristicCreate(BaseModel):
    label: str
    reference_image_url: HttpUrlStr
    injection_hint: str
    always_include: bool = True  # hérité (non utilisé par la composition)
    # VIDÉO : récurrent (chaque média) vs pool (1 aléatoire/média).
    recurring: bool = False
    # PHOTO (Seedream) : image utilisée comme référence à chaque génération photo.
    seedream: bool = False
    priority: int = 0


class CharacteristicOut(CharacteristicCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class ModelCreate(BaseModel):
    name: str
    face_reference_url: HttpUrlStr
    notes: str | None = None


class ModelOut(ModelCreate):
    id: uuid.UUID
    characteristics: list[CharacteristicOut] = []

    model_config = {"from_attributes": True}


class ModelUpdate(BaseModel):
    """Édition partielle (ex. remplacer la photo visage après re-config R2)."""

    name: str | None = None
    face_reference_url: HttpUrlStr | None = None
    notes: str | None = None


class CharacteristicUpdate(BaseModel):
    label: str | None = None
    reference_image_url: HttpUrlStr | None = None
    injection_hint: str | None = None
    always_include: bool | None = None
    recurring: bool | None = None
    seedream: bool | None = None
    priority: int | None = None


# --- Banques d'assets ---


class OutfitCreate(BaseModel):
    image_url: HttpUrlStr
    tags: list[str] = []
    weight: float = Field(default=1.0, ge=0)


class OutfitOut(OutfitCreate):
    id: uuid.UUID
    status: str = "ready"
    error: str | None = None

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
    status: str = "ready"
    source_video_url: str | None = None
    # Paroles transcrites (format long 30 s) — présence = template exploitable
    # en storytelling_long.
    transcript: str | None = None
    error: str | None = None

    model_config = {"from_attributes": True}


class ReverseVideoRequest(BaseModel):
    source_video_url: HttpUrlStr = Field(description="Vidéo de référence uploadée (R2)")
    category: str = Field(description="Catégorie du template généré")
    speaking: bool = False


class BulkReverseVideoRequest(BaseModel):
    """Réplique en masse : N vidéos → N templates (même catégorie + flag parlant)."""

    source_video_urls: list[HttpUrlStr] = Field(min_length=1)
    category: str = Field(description="Catégorie appliquée à tous les templates")
    speaking: bool = False


class BulkDescribeRequest(BaseModel):
    """Import en masse d'outfits/backgrounds avec auto-description (vision)."""

    image_urls: list[HttpUrlStr] = Field(min_length=1)
    # Suffixe FOURNI PAR L'UTILISATEUR, ajouté à la fin de chaque description
    # générée (ex. un détail à toujours inclure). Optionnel, jamais en dur.
    suffix: str = ""
    weight: float = Field(default=1.0, ge=0)


class DescribePendingRequest(BaseModel):
    """Relance la description des assets restés en `pending`. Suffixes optionnels
    (le suffixe d'origine n'est pas mémorisé sur la ligne)."""

    outfit_suffix: str = ""
    background_suffix: str = ""


class DialogueLineCreate(BaseModel):
    category: str
    raw_text: str = Field(
        description="Lignes taggées, ordre chrono. [H]/[F] = la model (voix grave / "
        "voix féminine), [M] = un autre homme (ex. intervieweur), [W] = une autre femme, "
        "[beat] = pause. Voice_profiles requis pour [H]/[F] seulement : [M]/[W] "
        "gardent la voix générée par Seedance."
    )
    weight: float = Field(default=1.0, ge=0)

    @field_validator("raw_text")
    @classmethod
    def _valid_tagged_script(cls, value: str) -> str:
        try:
            parse_tagged_script(value)
        except DialogueParseError as exc:
            raise ValueError(str(exc)) from exc
        # Règle éditoriale (tous types de contenu) : au plus UNE bascule [H]↔[F].
        if count_model_voice_switches(value) > MAX_VOICE_SWITCHES:
            raise ValueError(_VOICE_SWITCH_ERROR)
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

    @field_validator("dialogue_script")
    @classmethod
    def _max_one_voice_switch(cls, value: str | None) -> str | None:
        if not value:
            return value
        try:
            switches = count_model_voice_switches(value)
        except DialogueParseError:
            return value  # script libre non taggé (Phase 1) : pas de contrainte ajoutée
        if switches > MAX_VOICE_SWITCHES:
            raise ValueError(_VOICE_SWITCH_ERROR)
        return value

    count: int = Field(default=1, ge=1, le=100)
    resolution: Resolution = "720p"
    duration_s: int = Field(default=10, ge=4, le=15)  # Seedance 2.0 : 4–15 s/clip
    bitrate: Bitrate = "standard"
    model_variant: str = "seedance_2.0"
    budget_cap_usd: float | None = None
    extra_reference_urls: list[HttpUrlStr] = Field(
        default_factory=list, description="Refs additionnelles (outfit, background) — Phase 1"
    )


class BatchJobCreate(BaseModel):
    """Job batch Phase 2 : composition depuis les banques, N items par catégorie."""

    model_id: uuid.UUID
    counts_per_category: dict[str, int] = Field(
        description='Ex : {"skit": 20, "podcast": 10}', min_length=1
    )
    resolution: Resolution = "720p"
    duration_s: int = Field(default=10, ge=4, le=15)  # Seedance 2.0 : 4–15 s/clip
    bitrate: Bitrate = "standard"
    model_variant: str = "seedance_2.0"
    music_url: HttpUrlStr | None = Field(default=None, description="Piste mixée à l'assemblage")
    # Demande custom one-shot fusionnée au prompt final de chaque vidéo du batch.
    custom_prompt: str = ""
    # Case « pas de background » : aucun décor tiré, on ne mentionne aucun lieu.
    omit_background: bool = False
    budget_cap_usd: float | None = None


class ItemOut(BaseModel):
    id: uuid.UUID
    category: str
    status: str
    qc_status: str
    review_status: str
    filled_prompt: str
    caption_text: str | None = None
    face_match_score: float | None = None
    reference_video_url: str | None = None  # copypaste : vidéo de référence
    seedance_task_id: str | None
    raw_video_url: str | None
    final_video_url: str | None
    item_estimated_cost: float | None
    item_actual_cost: float | None = None
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


class JobSummaryOut(BaseModel):
    id: uuid.UUID
    status: str
    counts_per_category: dict[str, int] = {}
    resolution: str
    duration_s: int
    estimated_cost_usd: float | None
    actual_cost_usd: float | None
    compose_shortfall: dict[str, int] = {}
    created_at: object = None

    model_config = {"from_attributes": True}


class ReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]


class ExportOut(BaseModel):
    job_id: uuid.UUID
    approved_count: int
    videos: list[dict]


# --- Copypaste (vidéo → vidéo) ---


class ReferenceVideoCreate(BaseModel):
    video_url: HttpUrlStr = Field(description="Vidéo de référence uploadée (R2)")
    label: str = ""
    theme: str = Field(default="", max_length=64, description="Thème de rangement (gym, plage…)")
    weight: float = Field(default=1.0, ge=0)


class ReferenceVideoUpdate(BaseModel):
    """Rangement d'une vidéo de la banque : seuls les champs fournis changent."""

    theme: str | None = Field(default=None, max_length=64)
    label: str | None = None
    weight: float | None = Field(default=None, ge=0)


class ReferenceVideoOut(BaseModel):
    id: uuid.UUID
    video_url: str
    label: str = ""
    theme: str = ""
    weight: float
    duration_s: float | None = None  # sondée à l'ajout ; > 15 s = inutilisable
    fps: float | None = None  # hors 23,8–60 = inutilisable (🔧 pour normaliser)
    created_at: object = None

    model_config = {"from_attributes": True}


class CopypasteJobCreate(BaseModel):
    """Job copypaste : N vidéos générées depuis une vidéo de référence (ou la
    banque vidéo, piochée au hasard) + la photo visage de la model."""

    model_id: uuid.UUID
    count: int = Field(default=1, ge=1, le=100)
    # Vidéo uploadée pour ce job. Optionnelle si use_bank=true (la banque
    # suffit alors).
    reference_video_url: HttpUrlStr | None = None
    # Ajoute la vidéo uploadée à la banque (décocher pour tester une vidéo
    # sans polluer la banque si elle est de mauvaise qualité).
    save_to_bank: bool = True
    # Ajoute à CHAQUE vidéo les caractéristiques (récurrentes + 1 aléatoire du
    # pool, comme le moteur vidéo) et un outfit tiré de la banque — JAMAIS de
    # background : le décor reste celui de la vidéo de référence.
    add_random_assets: bool = True
    # Pioche AU HASARD une vidéo de la banque pour chaque item du batch.
    use_bank: bool = False
    # Restreint la pioche use_bank à UN thème de la banque ("" = tous).
    theme: str = Field(default="", max_length=64)
    # Thème donné à la vidéo uploadée quand elle rejoint la banque.
    video_theme: str = Field(default="", max_length=64)
    # Sélection PRÉCISE de vidéos de la banque : chaque vidéo cochée est
    # utilisée, et `count` devient le nombre de générations PAR vidéo
    # sélectionnée (total = count × sélection, plafonné à 200). Prioritaire
    # sur use_bank et reference_video_url.
    reference_video_ids: list[uuid.UUID] = []
    # Ajouté au prompt fixe « Replace the girl in the video… ».
    custom_prompt: str = ""
    resolution: Resolution = "720p"
    duration_s: int = Field(default=10, ge=4, le=15)  # Seedance 2.0 : 4–15 s/clip
    bitrate: Bitrate = "standard"
    model_variant: str = "seedance_2.0"
    # Qualité Seedance : « standard » (bytedance/seedance-2, meilleure qualité,
    # 1080p dispo — défaut de la feature) ou « fast » (moins cher, 480p/720p).
    seedance_quality: Literal["standard", "fast"] = "standard"
    budget_cap_usd: float | None = None


# --- Pictures (nano banana) ---

# Ratios acceptés par Seedream 5.0 Pro (schéma officiel) — PAS de 4:5.
ImageSize = Literal["1:1", "4:3", "3:4", "16:9", "9:16", "2:3", "3:2"]


class PicturePromptCreate(BaseModel):
    source_image_url: HttpUrlStr = Field(description="Image de référence uploadée (R2)")
    tags: list[str] = []
    weight: float = Field(default=1.0, ge=0)


class BulkPicturePromptRequest(BaseModel):
    """Reverse-engineering en masse : N images → N prompts réutilisables."""

    source_image_urls: list[HttpUrlStr] = Field(min_length=1)
    tags: list[str] = []
    weight: float = Field(default=1.0, ge=0)


class PicturePromptOut(BaseModel):
    id: uuid.UUID
    source_image_url: str
    prompt_text: str | None
    status: str
    tags: list[str] = []
    weight: float
    error: str | None = None

    model_config = {"from_attributes": True}


PhotoStyle = Literal["facecam_selfie", "amateur", "professional", "amateur_blurry"]


class PictureJobCreate(BaseModel):
    model_id: uuid.UUID
    count: int = Field(ge=1, le=200)
    image_size: ImageSize = "4:3"
    image_resolution: Literal["1K", "2K"] = "2K"
    output_format: Literal["png", "jpeg"] = "png"
    model_variant: str = "seedream"
    # Styles photo cochés (0..n) ; vide = prompt brut, sans modificateur de style.
    styles: list[PhotoStyle] = []
    budget_cap_usd: float | None = None


class PictureItemOut(BaseModel):
    id: uuid.UUID
    status: str
    qc_status: str
    review_status: str
    filled_prompt: str
    face_match_score: float | None = None
    kie_task_id: str | None
    raw_image_url: str | None
    final_image_url: str | None
    item_estimated_cost: float | None
    item_actual_cost: float | None = None
    error: str | None

    model_config = {"from_attributes": True}


class PictureJobOut(BaseModel):
    id: uuid.UUID
    status: str
    count: int
    image_size: str
    image_resolution: str = "2K"
    output_format: str
    styles: list[str] = []
    model_variant: str
    budget_cap_usd: float | None
    estimated_cost_usd: float | None
    actual_cost_usd: float | None
    compose_shortfall: int = 0
    error: str | None = None
    items: list[PictureItemOut] = []

    model_config = {"from_attributes": True}


class PictureEstimateRequest(BaseModel):
    count: int = Field(ge=1)
    model_variant: str = "seedream"
    image_resolution: Literal["1K", "2K"] = "2K"
    qc_success_rate: float | None = Field(default=None, gt=0, le=1)
    budget_usd: float | None = None


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


class BatchEstimateRequest(BaseModel):
    """Estimation live du panneau de génération : mêmes paramètres que
    BatchJobCreate, sans rien créer."""

    counts_per_category: dict[str, int] = Field(min_length=1)
    duration_s: float = Field(default=10, gt=0)
    resolution: Resolution = "720p"
    model_variant: str = "seedance_2.0"
    qc_success_rate: float | None = Field(default=None, gt=0, le=1)
    budget_usd: float | None = None


class BatchEstimateResponse(EstimateResponse):
    total_items: int
    per_category_usd: dict[str, float] = {}
