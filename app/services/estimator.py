"""Estimateur de coût (brief §8) — fonction PURE.

Zéro appel API, zéro accès DB : les tarifs sont passés en argument
(chargés depuis la table `pricing` par l'appelant). La même fonction sert
trois usages : estimation live UI, gate budget serveur avant dispatch,
log de calibration après coup.

    video_cost(item) = duration_s * rate(model, resolution, with_ref=True)
    voice_cost(item) = speaking ? (audio_s / 60) * rate_s2s : 0
    gross            = Σ item_cost
    effective        = gross / qc_success_rate   # coût par vidéo LIVRÉE
"""

from dataclasses import dataclass, field


class MissingRateError(LookupError):
    """Aucun tarif en table pour cette combinaison — on refuse d'estimer
    avec un tarif deviné (jamais de prix hardcodé)."""


@dataclass(frozen=True)
class Rate:
    model: str
    resolution: str | None
    with_ref: bool | None
    unit: str  # per_sec / per_min
    rate_usd: float


@dataclass(frozen=True)
class ItemSpec:
    """Un lot homogène d'items à estimer (une catégorie du batch)."""

    count: int
    duration_s: float
    resolution: str
    model: str = "seedance_2.0"
    with_ref: bool = True
    speaking: bool = False
    # Durée d'audio parlé à convertir (par défaut : toute la vidéo)
    audio_s: float | None = None


@dataclass(frozen=True)
class Estimate:
    gross_usd: float
    effective_usd: float
    qc_success_rate: float
    total_items: int
    per_item_usd: list[float] = field(default_factory=list)

    @property
    def cost_per_delivered_video_usd(self) -> float:
        if self.total_items == 0:
            return 0.0
        return self.effective_usd / self.total_items


def _find_rate(
    rates: list[Rate], model: str, resolution: str | None, with_ref: bool | None, unit: str
) -> float:
    for r in rates:
        if (
            r.model == model
            and r.resolution == resolution
            and r.with_ref == with_ref
            and r.unit == unit
        ):
            return r.rate_usd
    raise MissingRateError(
        f"Pas de tarif en table pour model={model} resolution={resolution} "
        f"with_ref={with_ref} unit={unit} — synchroniser la table pricing."
    )


def estimate_item(spec: ItemSpec, rates: list[Rate], s2s_model: str = "elevenlabs_s2s") -> float:
    """Coût d'UN item (vidéo + voice-swap éventuel)."""
    video_rate = _find_rate(rates, spec.model, spec.resolution, spec.with_ref, "per_sec")
    video_cost = spec.duration_s * video_rate
    voice_cost = 0.0
    if spec.speaking:
        s2s_rate = _find_rate(rates, s2s_model, None, None, "per_min")
        audio_s = spec.audio_s if spec.audio_s is not None else spec.duration_s
        voice_cost = (audio_s / 60.0) * s2s_rate
    return video_cost + voice_cost


def estimate_batch(
    specs: list[ItemSpec],
    rates: list[Rate],
    qc_success_rate: float = 0.80,
) -> Estimate:
    if not 0 < qc_success_rate <= 1:
        raise ValueError("qc_success_rate doit être dans ]0, 1]")
    per_item: list[float] = []
    for spec in specs:
        cost = estimate_item(spec, rates)
        per_item.extend([cost] * spec.count)
    gross = sum(per_item)
    return Estimate(
        gross_usd=round(gross, 4),
        effective_usd=round(gross / qc_success_rate, 4),
        qc_success_rate=qc_success_rate,
        total_items=len(per_item),
        per_item_usd=per_item,
    )


def estimate_pictures(
    count: int,
    rates: list[Rate],
    model: str = "nano_banana",
    qc_success_rate: float = 0.80,
    resolution: str | None = None,
) -> Estimate:
    """Estimation d'un batch d'images — tarif per_image (par résolution si fournie)."""
    if count < 0:
        raise ValueError("count négatif")
    if not 0 < qc_success_rate <= 1:
        raise ValueError("qc_success_rate doit être dans ]0, 1]")
    unit = _find_rate(rates, model, resolution, None, "per_image")
    per_item = [unit] * count
    gross = sum(per_item)
    return Estimate(
        gross_usd=round(gross, 4),
        effective_usd=round(gross / qc_success_rate, 4),
        qc_success_rate=qc_success_rate,
        total_items=count,
        per_item_usd=per_item,
    )


def max_pictures_for_budget(
    budget_usd: float,
    rates: list[Rate],
    model: str = "nano_banana",
    qc_success_rate: float = 0.80,
    resolution: str | None = None,
) -> int:
    unit = _find_rate(rates, model, resolution, None, "per_image") / qc_success_rate
    if unit <= 0:
        return 0
    return int(budget_usd // unit)


def max_videos_for_budget(
    budget_usd: float,
    spec: ItemSpec,
    rates: list[Rate],
    qc_success_rate: float = 0.80,
) -> int:
    """Budget cap inversé : combien de vidéos livrables à la config courante."""
    unit_cost = estimate_item(spec, rates) / qc_success_rate
    if unit_cost <= 0:
        return 0
    return int(budget_usd // unit_cost)
