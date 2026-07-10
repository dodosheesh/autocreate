# autocreate

Content factory — moteur de génération de Reels IA en masse pour une model IA.
Seedance 2.0 via [kie.ai](https://kie.ai) (audio natif), voice-swap ElevenLabs, assemblage FFmpeg, stockage R2.

**État actuel : Phase 3** — pipeline complet post-génération : QC face-match (ArcFace vs photo de référence), **voice-swap ElevenLabs** (deux timbres fixes `[H]`/`[F]` par reel, lèvres synchro préservées), assemblage avec **overlay caption façon Snapchat** et piste musique, **calibration estimé/réel** qui affine l'estimateur batch après batch. Les flows Phase 1 (mono-prompt) et Phase 2 (batch depuis les banques) restent disponibles.

## Stack

Python 3.13 · FastAPI · Celery · Redis · PostgreSQL · Cloudflare R2 · FFmpeg · Railway

## Architecture

```
POST /api/jobs/batch ──► compose_job : tirage pondéré template+outfit+background
                         (+dialogue si speaking, +caption si slot), dédup combo_hash,
                         injection caractéristiques + refs ≤ 12
                     ──► estimate_and_gate : coût item par item (table pricing),
                         blocage budget_cap AVANT dépense
                     ──► dispatch_seedance ──► kie.ai createTask
                                                    │  (callback, coût réel capturé)
POST /api/webhooks/kie ◄────────────────────────────┘
                     ──► process_generated :
                           download → QC face-match (frame → ArcFace → seuil)
                           → voice-swap (démux → VAD → ElevenLabs sts par segment
                             → timeline reconstruite → remux, lèvres intactes)
                           → assemblage (9:16, bitrate, caption Snapchat, musique)
                           → upload R2
                     ──► job terminé → calibration_log (estimé vs réel + taux QC)
```

Statuts job : `pending → composing → dispatched → completed | blocked_budget | failed`
Statuts item : `composed → dispatched → generated → (qc → voiced, Phase 3) → assembled → done | failed`

Si l'espace de combos uniques s'épuise avant d'atteindre le count demandé, le job
reporte le manque dans `compose_shortfall` (jamais de doublon ni de cap silencieux).

## Démarrage local

```bash
# 1. Postgres + Redis
docker compose up -d

# 2. Dépendances (+ ffmpeg requis : apt install ffmpeg fontconfig)
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"
# QC face-match (optionnel, lourd — insightface/onnxruntime) :
# .venv/bin/pip install -e ".[qc]" puis QC_ENABLED=true dans .env

# 3. Config
cp .env.example .env   # remplir KIE_API_KEY, R2_*, PUBLIC_BASE_URL

# 4. Tables + seed pricing
.venv/bin/python -m app.db.init_db

# 5. Lancer
.venv/bin/uvicorn app.main:app --reload           # API (docs sur /docs)
.venv/bin/celery -A app.workers.celery_app worker --loglevel=info   # worker
```

Tests : `.venv/bin/python -m pytest`

## Utilisation Phase 1

```bash
# Créer la model (photo visage = référence unique de consistance)
curl -X POST localhost:8000/api/models -H 'Content-Type: application/json' -d '{
  "name": "Ma model",
  "face_reference_url": "https://<r2>/face.jpg"
}'

# Ajouter une caractéristique spéciale (photo du trait précis + hint d'injection)
curl -X POST localhost:8000/api/models/<model_id>/characteristics -H 'Content-Type: application/json' -d '{
  "label": "tatouage avant-bras gauche",
  "reference_image_url": "https://<r2>/tattoo.jpg",
  "injection_hint": "a delicate floral tattoo on her left forearm",
  "always_include": true,
  "priority": 0
}'

# Estimation live (fonction pure sur la table pricing)
curl -X POST localhost:8000/api/estimate -H 'Content-Type: application/json' -d '{
  "count": 100, "duration_s": 10, "resolution": "720p", "budget_usd": 100
}'
# → {"gross_usd": 125.0, "effective_usd": 156.25, "max_videos_for_budget": 64, ...}

# Lancer un batch (trigger manuel, une catégorie)
curl -X POST localhost:8000/api/jobs -H 'Content-Type: application/json' -d '{
  "model_id": "<model_id>",
  "category": "skit",
  "prompt": "A woman walking on the beach, {characteristics}, golden hour, handheld camera.",
  "dialogue_script": "[H] premiere ligne\n[F] deuxieme ligne",
  "count": 3, "resolution": "720p", "duration_s": 10, "bitrate": "standard",
  "budget_cap_usd": 10
}'

# Suivre
curl localhost:8000/api/jobs/<job_id>
```

## Utilisation Phase 2 — banques + batch

```bash
# Remplir les banques (répéter par entrée ; contenu fourni par l'utilisateur)
curl -X POST localhost:8000/api/banks/outfits -H 'Content-Type: application/json' -d '{
  "image_url": "https://<r2>/outfit-red-dress.jpg", "tags": ["red dress"], "weight": 1.0
}'
curl -X POST localhost:8000/api/banks/backgrounds -H 'Content-Type: application/json' -d '{
  "image_url": "https://<r2>/bg-beach.jpg", "tags": ["beach at sunset"], "weight": 1.0
}'
curl -X POST localhost:8000/api/banks/templates -H 'Content-Type: application/json' -d '{
  "category": "podcast",
  "template_text": "A woman on a podcast set, {outfit}, {background}, {characteristics}. {dialogue}",
  "speaking": true, "weight": 1.0
}'
curl -X POST localhost:8000/api/banks/dialogues -H 'Content-Type: application/json' -d '{
  "category": "podcast", "raw_text": "[H] ligne voix grave\n[beat]\n[F] ligne voix kawaii"
}'
curl -X POST localhost:8000/api/banks/captions -H 'Content-Type: application/json' -d '{
  "category": "snapchat", "text": "légende façon snapchat"
}'
curl -X POST localhost:8000/api/banks/voices -H 'Content-Type: application/json' -d '{
  "label": "grave masculin", "elevenlabs_voice_id": "<voice_id>", "gender": "male", "tag": "H"
}'

# Lancer un batch multi-catégories : le moteur compose N variantes dédupliquées
curl -X POST localhost:8000/api/jobs/batch -H 'Content-Type: application/json' -d '{
  "model_id": "<model_id>",
  "counts_per_category": {"podcast": 10, "skit": 20},
  "resolution": "720p", "duration_s": 10, "bitrate": "standard",
  "budget_cap_usd": 60
}'
# → jobs asynchrone : GET /api/jobs/<id> montre statut, items, coûts, compose_shortfall
```

Slots disponibles dans `template_text` : `{outfit}` `{background}` `{characteristics}`
`{dialogue}` `{caption}` — les slots inconnus sont laissés intacts (visibles dans le
prompt final pour repérer une faute de frappe). Un template `speaking` exige une banque
de dialogues non vide pour sa catégorie ; un slot `{caption}` exige une banque de captions.
Les scripts taggés sont validés à l'entrée (`[H]`/`[F]`/`[beat]`, une ligne = une voix).

Le slot `{characteristics}` dans le prompt reçoit les `injection_hint` des caractéristiques
`always_include` (triées par `priority`) ; sans slot, ils sont ajoutés en fin de description.
Les images de référence envoyées à Seedance = visage + photos des caractéristiques + refs
additionnelles, **plafonnées à 12** (contrainte Seedance 2.0), le visage n'étant jamais évincé.

## Pipeline voix (Phase 3)

1. Deux `voice_profiles` fixes à créer une fois (`tag` `H` = masculin grave, `F` = féminin) —
   le timbre est 100 % consistant sur tous les reels.
2. Seedance génère la vidéo **avec son audio** (lèvres synchro). Le script taggé sert de
   carte de segments : VAD par énergie détecte les blocs de parole, bloc i ↔ ligne i.
   Plus de blocs que de lignes → fusion des plus petits silences ; moins → item rejeté
   (erreur explicite plutôt qu'une voix sur la mauvaise ligne).
3. Chaque segment passe par ElevenLabs `speech-to-speech` (timbre changé, timing/émotion
   préservés), est calé à la durée exacte du slot, puis la timeline est reconstruite
   (ambiances/silences d'origine conservés) et remuxée — **zéro lipsync nécessaire**.

Réglages : `ELEVENLABS_STABILITY` (delivery stable inter-reels),
`ELEVENLABS_SIMILARITY_BOOST` (collé au timbre cible), `VOICE_SWAP_ENABLED=false`
pour bypasser en test.

## QC face-match (Phase 3)

`QC_ENABLED=true` + extra `[qc]` : une frame de la vidéo brute → embedding ArcFace
(insightface buffalo_l) → similarité cosinus vs `face_reference_url` → sous
`QC_THRESHOLD` (0.35 par défaut) l'item est rejeté (`qc_status=fail`) avant de dépenser
le voice-swap. Le taux de réussite observé alimente `calibration_log` et recalibre
automatiquement le coût effectif affiché par `/api/estimate`.

## Notes

- **Pricing** : la table `pricing` est la seule source de vérité tarifaire (seedée avec les
  valeurs indicatives du brief, à resynchroniser sur kie.ai/pricing). Une combinaison absente
  de la table fait échouer l'estimation explicitement — jamais de tarif deviné.
- **Webhooks** : chemin nominal ; `poll_pending_items` (à brancher sur celery beat) sert de
  filet de sécurité si un callback se perd.
- **Contenu utilisateur** : les dialogues taggés `[H]`/`[F]` et les captions sont fournis par
  l'utilisateur ; le moteur remplit les slots, il ne génère pas ce contenu.
- **Multi-tenant** : `tenant_id` présent sur toutes les tables métier (default `default`),
  non exposé côté API pour l'instant.

## Roadmap

- ~~**Phase 2** — banques d'assets + moteur de composition pondéré + dédup `combo_hash`~~ ✔
- ~~**Phase 3** — QC face-match, pipeline voix, calibration estimé/réel~~ ✔
- **Phase 4** — panneau de génération multi-catégories (UI), estimateur live UI, grille de
  review (approuver/rejeter/exporter), celery beat pour le polling filet de sécurité
- **Phase 5** — multi-tenant, billing, Alembic (create_all suffit tant que la base
  n'est pas encore déployée)
