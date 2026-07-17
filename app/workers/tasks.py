"""Chaîne Celery — Phases 1-2.

    compose_job(job) ──► estimate_and_gate(job) ──► dispatch_seedance(item)
                              │ (budget cap)              │
                              ▼                           ▼
                        blocked_budget           kie.ai ──► webhook FastAPI
                                                              │
                                                              ▼
                                                   process_generated(item)
                                                   (download → assemble → R2)

Phase 3 insérera qc_check et swap_voice entre generated et assembled ;
en attendant qc_status reste `skipped`.
"""

import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db.base import db_session
from app.net import safe_download
from app.db.models import (
    Background,
    Caption,
    DialogueLine,
    GenerationJob,
    ItemStatus,
    JobItem,
    JobStatus,
    Model,
    Outfit,
    PromptTemplate,
    QcStatus,
)
from app.integrations import kie, r2
from app.media.assemble import AssembleParams, assemble, concat_clips
from app.media.frames import extract_last_frame
from app.services import calibration, variation
from app.services.composer import CharacteristicInput
from app.services.estimator import ItemSpec, estimate_item
from app.services.pricing import load_rates
from app.services.voice_swap import VoiceSwapError, swap_voices
from app.workers.celery_app import celery_app

# Format long 30 s : 2 clips de 15 s enchaînés (le clip 2 démarre sur la dernière
# frame du clip 1). Constantes définies dans le moteur de variation.
LONG_FORM_CLIP_S = variation.LONG_FORM_CLIP_S
LONG_FORM_TOTAL_S = variation.LONG_FORM_TOTAL_S


def _pk(value: str | uuid.UUID) -> uuid.UUID:
    """Les IDs passent en str à travers le broker Celery ; les colonnes Uuid
    exigent des objets uuid.UUID."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


def _download(url: str, dest: Path) -> None:
    """Téléchargement durci anti-SSRF (schéma http/s, IP publiques, taille max,
    redirections re-validées) — cf. app/net.safe_download."""
    safe_download(url, dest)


def _fail_item(item_id: str, error: str) -> None:
    with db_session() as db:
        item = db.get(JobItem, _pk(item_id))
        if item:
            item.status = ItemStatus.FAILED
            item.error = error[:4000]


def _fail_job(job_id: str, error: str) -> None:
    with db_session() as db:
        job = db.get(GenerationJob, _pk(job_id))
        if job:
            job.status = JobStatus.FAILED
            job.error = error[:4000]


def _build_pools(db, categories: list[str], tenant_id: str) -> dict[str, variation.CategoryPools]:
    """Charge les banques Postgres en pools purs pour le moteur de variation,
    scopées au tenant du job. Outfits/backgrounds sont partagés entre catégories ;
    templates, dialogues et captions sont filtrés par catégorie."""
    outfits = [
        variation.outfit_option(str(o.id), o.tags, o.image_url, o.weight)
        for o in db.scalars(
            select(Outfit).where(Outfit.tenant_id == tenant_id, Outfit.status == "ready")
        ).all()
    ]
    backgrounds = [
        variation.background_option(str(b.id), b.tags, b.image_url, b.weight)
        for b in db.scalars(
            select(Background).where(
                Background.tenant_id == tenant_id, Background.status == "ready"
            )
        ).all()
    ]
    pools: dict[str, variation.CategoryPools] = {}
    for category in categories:
        templates = [
            variation.TemplateOption(
                id=str(t.id),
                template_text=t.template_text,
                speaking=t.speaking,
                weight=t.weight,
                transcript=t.transcript,
            )
            for t in db.scalars(
                select(PromptTemplate).where(
                    PromptTemplate.tenant_id == tenant_id,
                    PromptTemplate.category == category,
                    PromptTemplate.status == "ready",
                )
            ).all()
        ]
        dialogues = [
            variation.Option(id=str(d.id), weight=d.weight, text=d.raw_text)
            for d in db.scalars(
                select(DialogueLine).where(
                    DialogueLine.tenant_id == tenant_id,
                    DialogueLine.category == category,
                )
            ).all()
        ]
        captions = [
            variation.Option(id=str(c.id), weight=c.weight, text=c.text)
            for c in db.scalars(
                select(Caption).where(
                    Caption.tenant_id == tenant_id, Caption.category == category
                )
            ).all()
        ]
        pools[category] = variation.CategoryPools(
            templates=templates,
            outfits=outfits,
            backgrounds=backgrounds,
            dialogues=dialogues,
            captions=captions,
        )
    return pools


def _compose_failure_message(db, tenant_id: str, empty_categories: list[str]) -> str:
    """Message d'échec précis quand aucun item n'a pu être composé. Pour le format
    long, distingue « aucun template » de « template prêt mais SANS transcript »."""
    parts: list[str] = []
    for cat in empty_categories:
        if cat == variation.LONG_FORM_CATEGORY:
            ready = db.scalars(
                select(PromptTemplate).where(
                    PromptTemplate.tenant_id == tenant_id,
                    PromptTemplate.category == cat,
                    PromptTemplate.status == "ready",
                )
            ).all()
            with_tr = [t for t in ready if (t.transcript or "").strip()]
            if ready and not with_tr:
                parts.append(
                    f"{cat} : {len(ready)} template(s) prêt(s) mais AUCUN avec paroles "
                    "transcrites. La vidéo de référence n'avait pas de piste audio "
                    "exploitable, ou la transcription (ElevenLabs Scribe) a échoué. "
                    "Vérifie que ELEVENLABS_API_KEY est bien configurée côté worker, "
                    "puis re-uploade la vidéo dans la section « Vidéos longues 30 s »."
                )
            else:
                parts.append(
                    f"{cat} : aucun template prêt. Uploade une vidéo dans la section "
                    "« Vidéos longues 30 s » et attends qu'elle passe de « pending » à « ready »."
                )
        else:
            parts.append(
                f"{cat} : aucun template PRÊT. Ajoute un template pour ce style dans Réglages "
                "(ou attends que la réplique vidéo passe de « pending » à « ready »)."
            )
    return " ".join(parts) or "Aucun template composable."


@celery_app.task
def compose_job(job_id: str) -> None:
    """Compose les items d'un job batch depuis les banques (brief §5),
    puis enchaîne sur le gate budget."""
    try:
        with db_session() as db:
            job = db.get(GenerationJob, _pk(job_id))
            if job is None or job.status != JobStatus.PENDING:
                return
            job.status = JobStatus.COMPOSING
            model = db.get(Model, job.model_id)
            characteristics = [
                CharacteristicInput(
                    id=str(c.id),
                    label=c.label,
                    reference_image_url=c.reference_image_url,
                    injection_hint=c.injection_hint,
                    priority=c.priority,
                    recurring=c.recurring,
                )
                for c in model.characteristics
            ]
            counts = {k: int(v) for k, v in (job.counts_per_category or {}).items()}
            pools = _build_pools(db, list(counts), job.tenant_id)

            result = variation.compose_batch(
                counts_per_category=counts,
                pools_by_category=pools,
                characteristics=characteristics,
                face_reference_url=model.face_reference_url,
                max_refs=get_settings().seedance_max_refs,
                custom_prompt=job.custom_prompt or "",
                omit_background=job.omit_background,
            )
            for composed in result.items:
                db.add(
                    JobItem(
                        job_id=job.id,
                        category=composed.category,
                        template_id=_pk(composed.template_id),
                        outfit_id=_pk(composed.outfit_id) if composed.outfit_id else None,
                        background_id=(
                            _pk(composed.background_id) if composed.background_id else None
                        ),
                        dialogue_line_id=(
                            _pk(composed.dialogue_id) if composed.dialogue_id else None
                        ),
                        caption_id=_pk(composed.caption_id) if composed.caption_id else None,
                        caption_text=composed.caption_text,
                        characteristic_ids=composed.characteristic_ids,
                        combo_hash=composed.combo_hash,
                        filled_prompt=composed.filled_prompt,
                        dialogue_script=composed.dialogue_script,
                        filled_prompt_2=composed.filled_prompt_2,
                        dialogue_script_2=composed.dialogue_script_2,
                        reference_image_urls=composed.reference_image_urls,
                    )
                )
            job.compose_shortfall = result.shortfall_per_category
            if not result.items:
                # Rien de composable : on échoue le job avec un message actionnable,
                # spécifique au format long (transcript manquant) le cas échéant.
                job.status = JobStatus.FAILED
                job.error = _compose_failure_message(
                    db, job.tenant_id, sorted(result.shortfall_per_category)
                )
                return
        estimate_and_gate.delay(job_id)
    except variation.ComposeError as exc:
        _fail_job(job_id, f"compose: {exc}")
    except Exception as exc:
        _fail_job(job_id, f"compose: {exc}")
        raise


@celery_app.task
def estimate_and_gate(job_id: str) -> None:
    """Estime le coût item par item (table pricing), bloque si budget_cap
    dépassé — AVANT toute dépense — sinon dispatche vers kie.ai."""
    try:
        settings = get_settings()
        with db_session() as db:
            job = db.get(GenerationJob, _pk(job_id))
            if job is None or job.status != JobStatus.COMPOSING:
                return
            rates = load_rates(db)
            items = db.scalars(
                select(JobItem).where(
                    JobItem.job_id == job.id, JobItem.status == ItemStatus.COMPOSED
                )
            ).all()
            gross = 0.0
            for item in items:
                # Format long 30 s = 2 clips de 15 s → facturé sur 30 s.
                is_long = item.category == variation.LONG_FORM_CATEGORY
                cost = estimate_item(
                    ItemSpec(
                        count=1,
                        duration_s=LONG_FORM_TOTAL_S if is_long else job.duration_s,
                        resolution=job.resolution,
                        model=job.model_variant,
                        speaking=item.dialogue_script is not None,
                    ),
                    rates,
                )
                item.item_estimated_cost = round(cost, 4)
                gross += cost
            job.estimated_cost_usd = round(gross, 4)
            if job.budget_cap_usd is not None and gross > job.budget_cap_usd:
                job.status = JobStatus.BLOCKED_BUDGET
                return
            job.status = JobStatus.DISPATCHED
            long_ids = [str(i.id) for i in items if i.category == variation.LONG_FORM_CATEGORY]
            short_ids = [str(i.id) for i in items if i.category != variation.LONG_FORM_CATEGORY]
        for item_id in short_ids:
            dispatch_seedance.delay(item_id)
        for item_id in long_ids:
            generate_long_form_item.delay(item_id)
    except Exception as exc:
        _fail_job(job_id, f"estimate_and_gate: {exc}")
        raise


@celery_app.task(bind=True, max_retries=3)
def dispatch_seedance(self, item_id: str) -> None:
    """Envoie l'item en génération chez kie.ai."""
    try:
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.COMPOSED:
                return
            job = item.job
            payload = kie.build_seedance_input(
                prompt=item.filled_prompt,
                reference_image_urls=item.reference_image_urls,
                resolution=job.resolution,
                duration_s=job.duration_s,
                aspect_ratio=job.aspect,
                # Copypaste : la vidéo de référence part avec la photo visage.
                reference_video_urls=(
                    [item.reference_video_url] if item.reference_video_url else None
                ),
            )
            # Slug par job (copypaste : Fast vs Standard), sinon modèle global.
            model_slug = job.kie_model or get_settings().kie_seedance_model
            task_id = kie.create_task(model_slug, payload)
            item.seedance_task_id = task_id
            item.status = ItemStatus.DISPATCHED
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.DISPATCHED
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            _fail_item(item_id, f"dispatch: {exc}")
            raise
        raise self.retry(exc=exc)


def _voice_map(db) -> dict[str, str]:
    """tag ([H]/[F]) → elevenlabs_voice_id depuis la table voice_profiles."""
    from app.db.models import VoiceProfile

    return {
        profile.tag: profile.elevenlabs_voice_id
        for profile in db.scalars(select(VoiceProfile)).all()
    }


def _finalize_job_if_done(db, job) -> None:
    """Quand tous les items sont terminaux : statut final du job + ligne de
    calibration (estimé vs réel + taux QC observé)."""
    statuses = {i.status for i in job.items}
    if not statuses or not statuses <= {ItemStatus.DONE, ItemStatus.FAILED}:
        return
    job.status = JobStatus.COMPLETED if ItemStatus.DONE in statuses else JobStatus.FAILED
    calibration.record_job_calibration(db, job)


@celery_app.task(bind=True, max_retries=3)
def process_generated(self, item_id: str) -> None:
    """Après retour kie.ai : download → QC face-match → voice-swap (si
    catégorie parlante) → assemblage (caption/musique) → upload R2."""
    settings = get_settings()
    try:
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.GENERATED:
                return
            job = item.job
            model = db.get(Model, job.model_id)
            raw_url = item.raw_video_url
            dialogue_script = item.dialogue_script
            caption_text = item.caption_text
            resolution, bitrate = job.resolution, job.bitrate
            music_url = job.music_url
            face_url = model.face_reference_url
            voices = _voice_map(db) if dialogue_script else {}
            job_id = str(job.id)

        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "raw.mp4"
            _download(raw_url, raw_path)
            current_path = raw_path

            # --- QC gate (brief §9) ---
            if settings.qc_enabled:
                from app.services import qc

                face_ref = Path(tmp) / "face_ref.jpg"
                _download(face_url, face_ref)
                score, passed = qc.check_video(str(raw_path), str(face_ref), tmp)
                with db_session() as db:
                    item = db.get(JobItem, _pk(item_id))
                    item.face_match_score = round(score, 4)
                    item.qc_status = QcStatus.PASS if passed else QcStatus.FAIL
                    if not passed:
                        item.status = ItemStatus.FAILED
                        item.error = f"qc: face_match_score={score:.3f} < {settings.qc_threshold}"
                        _finalize_job_if_done(db, item.job)
                        return
                    item.status = ItemStatus.QC
            else:
                with db_session() as db:
                    item = db.get(JobItem, _pk(item_id))
                    item.qc_status = QcStatus.SKIPPED

            # --- Voice-swap (brief §7) ---
            if dialogue_script and settings.voice_swap_enabled:
                swapped_path = Path(tmp) / "swapped.mp4"
                swap_voices(
                    str(current_path), str(swapped_path), dialogue_script, voices, tmp
                )
                current_path = swapped_path
                with db_session() as db:
                    item = db.get(JobItem, _pk(item_id))
                    item.status = ItemStatus.VOICED

            # --- Assemblage (brief §10) ---
            music_path = None
            if music_url:
                music_path = str(Path(tmp) / "music_input")
                _download(music_url, Path(music_path))
            final_path = Path(tmp) / "final.mp4"
            assemble(
                str(current_path),
                str(final_path),
                AssembleParams(
                    resolution=resolution, bitrate=bitrate, music_path=music_path
                ),
                caption_text=caption_text,
                workdir=tmp,
            )
            final_url = r2.upload_file(str(final_path), f"outputs/{job_id}/{item_id}.mp4")

        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            item.final_video_url = final_url
            item.status = ItemStatus.DONE
            _finalize_job_if_done(db, item.job)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            _fail_item(item_id, f"process: {exc}")
            with db_session() as db:
                item = db.get(JobItem, _pk(item_id))
                if item:
                    _finalize_job_if_done(db, item.job)
            raise
        raise self.retry(exc=exc)


# ---------------- Format long 30 s (chaîne synchrone) ----------------
#
# À la différence du flux standard (webhook + poll asynchrone), le 30 s enchaîne
# 2 générations DÉPENDANTES : le clip 2 démarre sur la dernière frame du clip 1.
# On exécute donc la chaîne de bout en bout dans UNE tâche Celery qui poll kie.ai
# (get_task) entre chaque étape — plus simple et sans état intermédiaire fragile.


def _poll_kie_until_done(task_id: str, timeout_s: int = 1800, interval_s: int = 15) -> kie.KieTaskResult:
    """Attend qu'une tâche kie.ai atteigne success/fail (poll bloquant)."""
    waited = 0
    while True:
        result = kie.get_task(task_id)
        if result.state in ("success", "fail"):
            return result
        if waited >= timeout_s:
            raise TimeoutError(f"kie.ai n'a pas terminé la tâche {task_id} en {timeout_s}s")
        time.sleep(interval_s)
        waited += interval_s


class _PermanentGenerationError(RuntimeError):
    """Refus DÉFINITIF de kie.ai (copyright, policy, contenu sensible…) : re-tenter
    ne passera pas et RE-FACTURE une génération → on échoue tout de suite."""


def _generate_long_clip(prompt: str, refs: list[str], resolution: str, aspect: str,
                        dest: Path) -> tuple[str, float | None]:
    """Lance un clip Seedance de 15 s, attend, télécharge → chemin local brut.
    Renvoie (chemin, coût_usd). Lève _PermanentGenerationError sur un refus
    définitif (pas de re-essai), RuntimeError sinon (re-essai possible)."""
    payload = kie.build_seedance_input(
        prompt=prompt,
        reference_image_urls=refs,
        resolution=resolution,
        duration_s=LONG_FORM_CLIP_S,
        aspect_ratio=aspect,
    )
    task_id = kie.create_seedance_task(payload)
    result = _poll_kie_until_done(task_id)
    if result.state != "success" or not result.result_urls:
        msg = result.fail_msg or "génération échouée"
        # Refus définitif (copyright/policy…) → surtout PAS de re-essai (chaque
        # re-essai régénère et re-facture un clip pour rien).
        if not kie.is_transient_failure(msg):
            raise _PermanentGenerationError(msg)
        raise RuntimeError(msg)
    _download(result.result_urls[0], dest)
    return str(dest), result.cost_usd


def _maybe_swap_long(clip_path: str, dialogue_script: str | None,
                     voices: dict[str, str], tmp: str, name: str) -> str:
    """Voice-swap best-effort d'un clip long : garde l'audio Seedance d'origine si
    aucun profil de voix ne couvre les tags (ou si le swap échoue)."""
    if not (dialogue_script and get_settings().voice_swap_enabled and voices):
        return clip_path
    try:
        out = str(Path(tmp) / f"{name}_voiced.mp4")
        swap_voices(clip_path, out, dialogue_script, voices, tmp)
        return out
    except VoiceSwapError:
        return clip_path  # pas de profil pour ce tag → on garde la voix générée


@celery_app.task(bind=True, max_retries=1)
def generate_long_form_item(self, item_id: str) -> None:
    """Génère UNE vidéo 30 s : clip 1 (moitié 1 des paroles) → dernière frame →
    clip 2 (moitié 2, démarré depuis cette frame) → concat → assemblage → R2.

    Chaîne synchrone (poll kie.ai) car le clip 2 dépend du clip 1."""
    try:
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.COMPOSED:
                return
            job = item.job
            prompt1 = item.filled_prompt
            prompt2 = item.filled_prompt_2 or item.filled_prompt
            dialogue1 = item.dialogue_script
            dialogue2 = item.dialogue_script_2
            refs = list(item.reference_image_urls or [])
            resolution, bitrate, aspect = job.resolution, job.bitrate, job.aspect
            music_url = job.music_url
            voices = _voice_map(db) if (dialogue1 or dialogue2) else {}
            job_id = str(job.id)
            item.status = ItemStatus.DISPATCHED
            item.qc_status = QcStatus.SKIPPED

        max_refs = get_settings().seedance_max_refs
        total_cost = 0.0
        with tempfile.TemporaryDirectory() as tmp:
            # --- Clip 1 (moitié 1 des paroles) ---
            clip1_raw, c1 = _generate_long_clip(
                prompt1, refs, resolution, aspect, Path(tmp) / "clip1.mp4"
            )
            total_cost += c1 or 0.0

            # --- Dernière frame du clip 1 → R2 → référence PRIORITAIRE du clip 2 ---
            frame_path = extract_last_frame(clip1_raw, str(Path(tmp) / "last_frame.jpg"))
            frame_url = r2.upload_file(
                frame_path, f"longform/{job_id}/{item_id}/frame.jpg",
                content_type="image/jpeg",
            )
            refs2 = ([frame_url] + refs)[:max_refs]

            # --- Clip 2 (moitié 2, enchaîné depuis la frame) ---
            clip2_raw, c2 = _generate_long_clip(
                prompt2, refs2, resolution, aspect, Path(tmp) / "clip2.mp4"
            )
            total_cost += c2 or 0.0

            # --- Voice-swap par clip (best-effort) ---
            clip1 = _maybe_swap_long(clip1_raw, dialogue1, voices, tmp, "clip1")
            clip2 = _maybe_swap_long(clip2_raw, dialogue2, voices, tmp, "clip2")

            # --- Concat 2×15 s → 30 s, puis normalisation format de livraison ---
            joined = str(Path(tmp) / "joined.mp4")
            concat_clips([clip1, clip2], joined)

            music_path = None
            if music_url:
                music_path = str(Path(tmp) / "music_input")
                _download(music_url, Path(music_path))
            final_path = str(Path(tmp) / "final.mp4")
            assemble(
                joined, final_path,
                AssembleParams(resolution=resolution, bitrate=bitrate, music_path=music_path),
                workdir=tmp,
            )
            final_url = r2.upload_file(final_path, f"outputs/{job_id}/{item_id}.mp4")

        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None:
                return
            item.raw_video_url = None
            item.final_video_url = final_url
            if total_cost:
                item.item_actual_cost = round(total_cost, 4)
            item.status = ItemStatus.DONE
            _finalize_job_if_done(db, item.job)
    except Exception as exc:
        # Refus DÉFINITIF (copyright/policy…) → échec immédiat, JAMAIS de re-essai
        # (chaque re-essai régénère et re-facture 2 clips pour rien).
        permanent = isinstance(exc, _PermanentGenerationError)
        if not permanent and self.request.retries < self.max_retries:
            # Remet l'item en COMPOSED pour un ré-essai propre (les clips déjà
            # générés sont perdus, mais la chaîne 30 s doit repartir de zéro).
            with db_session() as db:
                item = db.get(JobItem, _pk(item_id))
                if item is not None:
                    item.status = ItemStatus.COMPOSED
            raise self.retry(exc=exc, countdown=20)
        _fail_item(item_id, f"long_form: {exc}")
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item:
                _finalize_job_if_done(db, item.job)
        if permanent:
            return  # échec propre géré, pas la peine de re-lever pour Celery
        raise


@celery_app.task
def poll_pending_items() -> None:
    """Filet de sécurité si un webhook se perd : interroge kie.ai pour les
    items dispatchés. À brancher sur celery beat (optionnel en Phase 1)."""
    with db_session() as db:
        items = db.scalars(
            select(JobItem).where(JobItem.status == ItemStatus.DISPATCHED)
        ).all()
        stale = [(str(i.id), i.seedance_task_id) for i in items if i.seedance_task_id]
    for item_id, task_id in stale:
        result = kie.get_task(task_id)
        apply_kie_result(item_id, result)


def recheck_video_job(job_id: str) -> int:
    """Pull à la demande des résultats kie.ai pour un job vidéo (sans webhook)."""
    with db_session() as db:
        items = db.scalars(
            select(JobItem).where(
                JobItem.status == ItemStatus.DISPATCHED, JobItem.job_id == _pk(job_id)
            )
        ).all()
        stale = [(str(i.id), i.seedance_task_id) for i in items if i.seedance_task_id]
    for item_id, task_id in stale:
        try:
            apply_kie_result(item_id, kie.get_task(task_id))
        except Exception:
            pass
    return len(stale)


def apply_kie_result(item_id: str, result: kie.KieTaskResult) -> None:
    """Applique un résultat kie.ai (webhook ou polling) et enchaîne."""
    if result.state == "success" and result.result_urls:
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.DISPATCHED:
                return
            item.raw_video_url = result.result_urls[0]
            if result.cost_usd is not None:
                item.item_actual_cost = result.cost_usd
            item.status = ItemStatus.GENERATED
        process_generated.delay(item_id)
    elif result.state == "fail":
        retry = False
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.DISPATCHED:
                return  # idempotent : webhook + poll ne doivent pas se cumuler
            msg = result.fail_msg or "génération échouée"
            if result.cost_usd is not None:
                item.item_actual_cost = result.cost_usd
            if kie.is_transient_failure(msg) and item.generation_attempts < get_settings().generation_max_retries:
                item.generation_attempts += 1
                item.status = ItemStatus.COMPOSED
                item.error = f"kie.ai (re-essai {item.generation_attempts}): {msg}"
                retry = True
            else:
                item.status = ItemStatus.FAILED
                item.error = f"kie.ai: {msg}"
                _finalize_job_if_done(db, item.job)
        if retry:
            dispatch_seedance.apply_async((item_id,), countdown=15)
