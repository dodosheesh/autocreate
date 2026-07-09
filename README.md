# autocreate

Content factory — moteur de génération de Reels IA en masse pour une model IA.
Seedance 2.0 via [kie.ai](https://kie.ai) (audio natif), voice-swap ElevenLabs, assemblage FFmpeg, stockage R2.

**État actuel : Phase 1** — pipeline cœur : trigger manuel → génération Seedance → webhook → assemblage 9:16 → upload R2. Le schéma Postgres complet (12 tables) est déjà en place pour les phases suivantes.

## Stack

Python 3.13 · FastAPI · Celery · Redis · PostgreSQL · Cloudflare R2 · FFmpeg · Railway

## Architecture

```
POST /api/jobs ──► compose (injection caractéristiques + refs ≤ 12)
               ──► estimate + budget gate (table pricing, jamais de tarif en dur)
               ──► Celery dispatch_seedance ──► kie.ai createTask
                                                    │  (callback)
POST /api/webhooks/kie ◄────────────────────────────┘
               ──► process_generated : download → assemble FFmpeg (9:16, bitrate) → R2
```

Statuts item : `composed → dispatched → generated → (qc → voiced, Phase 3) → assembled → done | failed`

## Démarrage local

```bash
# 1. Postgres + Redis
docker compose up -d

# 2. Dépendances
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"

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

Le slot `{characteristics}` dans le prompt reçoit les `injection_hint` des caractéristiques
`always_include` (triées par `priority`) ; sans slot, ils sont ajoutés en fin de description.
Les images de référence envoyées à Seedance = visage + photos des caractéristiques + refs
additionnelles, **plafonnées à 12** (contrainte Seedance 2.0), le visage n'étant jamais évincé.

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

- **Phase 2** — banques d'assets (outfits, backgrounds, templates) + moteur de composition
  pondéré + dédup `combo_hash` + Alembic
- **Phase 3** — QC face-match (insightface), pipeline voix (VAD → ElevenLabs speech-to-speech
  par segment → reconcat), calibration estimé/réel
- **Phase 4** — panneau de génération multi-catégories, estimateur live UI, grille de review
- **Phase 5** — multi-tenant, billing
