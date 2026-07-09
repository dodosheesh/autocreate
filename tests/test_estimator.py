import pytest

from app.services.estimator import (
    ItemSpec,
    MissingRateError,
    Rate,
    estimate_batch,
    max_videos_for_budget,
)

RATES = [
    Rate("seedance_2.0", "480p", True, "per_sec", 0.0575),
    Rate("seedance_2.0", "720p", True, "per_sec", 0.125),
    Rate("seedance_2.0", "1080p", True, "per_sec", 0.31),
    Rate("elevenlabs_s2s", None, None, "per_min", 0.10),
]


def test_exemple_du_brief_100_clips_10s_720p():
    """Brief §8 : 100 clips 10 s 720p avec ref → 125 $ brut, ~156 $ effectif à 80 %."""
    est = estimate_batch(
        [ItemSpec(count=100, duration_s=10, resolution="720p")],
        RATES,
        qc_success_rate=0.80,
    )
    assert est.gross_usd == pytest.approx(125.0)
    assert est.effective_usd == pytest.approx(156.25)
    assert est.total_items == 100


def test_voice_cost_ajoute_pour_categories_parlantes():
    silent = estimate_batch([ItemSpec(count=1, duration_s=10, resolution="720p")], RATES)
    speaking = estimate_batch(
        [ItemSpec(count=1, duration_s=10, resolution="720p", speaking=True)], RATES
    )
    # 10 s d'audio → (10/60) × 0.10 ≈ 0.0167 $
    assert speaking.gross_usd == pytest.approx(silent.gross_usd + 10 / 60 * 0.10, abs=1e-4)


def test_multi_categories():
    est = estimate_batch(
        [
            ItemSpec(count=10, duration_s=10, resolution="720p"),
            ItemSpec(count=5, duration_s=10, resolution="1080p"),
        ],
        RATES,
    )
    assert est.gross_usd == pytest.approx(10 * 1.25 + 5 * 3.10)
    assert est.total_items == 15


def test_tarif_manquant_leve_une_erreur():
    """Jamais de tarif deviné : combinaison absente de la table → erreur explicite."""
    with pytest.raises(MissingRateError):
        estimate_batch([ItemSpec(count=1, duration_s=10, resolution="4k")], RATES)


def test_budget_cap_inverse():
    # 1.25 $/clip brut → 1.5625 $ livré ; 100 $ → 64 vidéos
    spec = ItemSpec(count=1, duration_s=10, resolution="720p")
    assert max_videos_for_budget(100.0, spec, RATES, qc_success_rate=0.80) == 64


def test_qc_rate_invalide():
    with pytest.raises(ValueError):
        estimate_batch([ItemSpec(count=1, duration_s=10, resolution="720p")], RATES, 0)
