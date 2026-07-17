# autocreate

Content factory — moteur de génération de Reels IA en masse pour une model IA.
Seedance 2.0 via [kie.ai](https://kie.ai) (audio natif), voice-swap ElevenLabs, assemblage FFmpeg, stockage R2.

**État actuel : Phase 5 — stack complète + accès restreint.** Le logiciel est protégé par une page de connexion (`/login`) ; toutes les routes API (hors webhook kie.ai) exigent une session. Panneau de génération web sur `/` (compteurs par catégorie, toggles résolution/durée/bitrate, **coût estimé en direct**, budget cap), grille de review (préviews vidéo, approuver/rejeter, export JSON des approuvés), pipeline complet : composition dédupliquée → gate budget → Seedance (kie.ai) → QC face-match → voice-swap ElevenLabs → assemblage (caption Snapchat, musique) → R2, avec calibration estimé/réel automatique. Backlog des features suivantes : `BACKLOG.md`.

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
.venv/bin/uvicorn app.main:app --reload           # UI sur /, docs API sur /docs
.venv/bin/celery -A app.workers.celery_app worker --beat --loglevel=info   # worker + beat
```

L'UI sur `http://localhost:8000/` couvre tout le flow quotidien : choisir la model,
saisir les counts par catégorie, régler résolution/durée/bitrate, voir le coût brut +
effectif bouger en direct (taux QC calibré), poser un budget max, Generate, puis suivre
les jobs et approuver/rejeter/exporter dans la grille de review. Les banques et les
models se gèrent via l'API (`/docs`).

Tests : `.venv/bin/python -m pytest`

## Déploiement Railway

Le repo est prêt pour Railway (`Dockerfile` + `railway.json`). L'image embarque
FFmpeg et les polices — rien à installer à la main. On déploie **deux services à
partir du même repo** (web + worker) plus deux plugins (Postgres + Redis).

### 1. Base de données et broker
Dans le projet Railway : **New → Database → PostgreSQL**, puis **New → Database →
Redis**. Railway crée les variables `DATABASE_URL` et `REDIS_URL`.

### 2. Service web (API + UI)
**New → GitHub Repo → dodosheesh/autocreate**. Railway détecte le `Dockerfile` et
lit `railway.json` (pré-déploiement `python -m app.db.init_db`) ; la commande de
démarrage web vient du `Dockerfile` (`uvicorn` via `sh -c`, qui résout `$PORT`).
Ne remets **pas** de start command custom sur le web (sinon `$PORT` n'est pas
substitué → `Invalid value for '--port'`). Réglages du service :
- **Variables** (onglet Variables) :
  ```
  SECRET_KEY            = (openssl rand -hex 32  ou  python -c "import secrets;print(secrets.token_hex(32))")
  KIE_WEBHOOK_SECRET    = (python -c "import secrets;print(secrets.token_hex(24))")
  PUBLIC_BASE_URL       = https://<ton-service-web>.up.railway.app
  KIE_API_KEY           = sk-...
  ANTHROPIC_API_KEY     = sk-ant-...    # optionnel : décrit/analyse les images avec Claude
                                        # (sinon Gemini via kie.ai). cf. « Analyse d'images ».
  KIE_SEEDREAM_MODEL    = seedream/5-pro-image-to-image   # modèle photo (image-to-image
                                        # multi-références). Résolution 1K/2K = choisie
                                        # par job dans l'UI (le tarif s'adapte tout seul).
  ELEVENLABS_API_KEY    = ...           # si voice-swap
  R2_ACCOUNT_ID         = ...
  R2_ACCESS_KEY_ID      = ...
  R2_SECRET_ACCESS_KEY  = ...
  R2_BUCKET             = autocreate
  R2_PUBLIC_BASE_URL    = https://pub-xxxx.r2.dev
  DATABASE_URL          = ${{Postgres.DATABASE_URL}}   # référence le plugin
  REDIS_URL             = ${{Redis.REDIS_URL}}
  BOOTSTRAP_ADMIN_EMAIL = sydeincovind@gmail.com
  # BOOTSTRAP_ADMIN_PASSWORD laissé vide → mot de passe aléatoire imprimé dans les
  # logs du pré-déploiement au premier boot (à noter, non ré-affiché).
  ```
- **Settings → Healthcheck Path** : `/health` (web uniquement).
- Railway expose une URL publique : reporte-la dans `PUBLIC_BASE_URL` puis redéploie.

> `DATABASE_URL` fourni par Railway commence par `postgresql://` ; le code le
> normalise automatiquement en `postgresql+psycopg://` — rien à faire.

### 3. Service worker (Celery + beat) — requis pour la GÉNÉRATION
**New → GitHub Repo → même repo** (deuxième service, même image).
- **Settings → Custom Start Command** :
  ```
  celery -A app.workers.celery_app worker --beat --loglevel=info --concurrency=4
  ```
- **Settings → Healthcheck** : aucun (le worker ne sert pas de HTTP).
- **Variables** : les mêmes que le web (le plus simple : *Shared Variables* au
  niveau projet pour tout ce qui est commun, chaque service les hérite).

> ⚠️ **Sans ce service worker, la génération vidéo/photo reste bloquée** (les
> tâches Celery s'empilent dans Redis sans être consommées). L'auto-description
> des outfits/backgrounds, elle, tourne **en synchrone dans la requête web** :
> elle fonctionne même sans worker (bouton « Décrire les éléments en attente »
> pour rattraper d'anciens imports restés en `pending`).

### 4. Premier login
Au premier déploiement, le pré-déploiement `init_db` crée les tables, seed la
table `pricing` et le compte propriétaire. Récupère le mot de passe initial dans
les **logs de déploiement du service web**, connecte-toi sur
`https://<web>.up.railway.app/login`, puis change-le via
`POST /api/auth/change-password` (ou garde-le si tu as fixé `BOOTSTRAP_ADMIN_PASSWORD`).

### 5. Webhooks kie.ai
Les appels `createTask` envoient automatiquement `PUBLIC_BASE_URL/api/webhooks/kie?secret=KIE_WEBHOOK_SECRET`
comme `callBackUrl`. Aucune config côté kie.ai : vérifie juste que `PUBLIC_BASE_URL`
est bien l'URL publique https et que `KIE_WEBHOOK_SECRET` est identique sur les deux services.

## Analyse d'images (auto-description & reverse-engineering)

Deux fournisseurs, sélectionnés automatiquement par la clé présente :

- **Claude (Anthropic)** — dès que `ANTHROPIC_API_KEY` est fourni, l'analyse
  d'images passe par Claude (Messages API, modèle `ANTHROPIC_VISION_MODEL`,
  défaut `claude-haiku-4-5-20251001`). C'est le chemin recommandé (qualité,
  fiabilité).
- **Gemini via kie.ai** — sinon, chemin par défaut (`KIE_VISION_MODEL` /
  `KIE_VISION_BASE_URL`), réutilise la clé kie.ai existante.

L'auto-description des outfits/backgrounds importés en masse est **synchrone**
(elle tourne dans la requête web, appels vision concurrents) : aucune dépendance
au worker Celery, résultat immédiat (`ready`/`failed` par image).

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

## Copypaste (vidéo → vidéo)

Seedance 2 accepte des **vidéos de référence** : la feature copypaste remplace la fille
d'une vidéo uploadée par la model (prompt fixe « Replace the girl in the video with the
girl in the picture » + custom prompt optionnel, photo visage envoyée en référence image).
Chaque vidéo uploadée rejoint la **banque vidéo** (dédup par URL) ; `use_bank=true` pioche
au hasard une vidéo de la banque pour chaque item du batch.

```bash
# Banque : ajouter / lister / supprimer des vidéos de référence (URL d'un upload R2)
curl -X POST localhost:8000/api/copypaste/videos -H 'Content-Type: application/json' \
  -d '{"video_url": "https://<r2>/uploads/<tenant>/ref.mp4", "label": "ref outfit rouge"}'
curl localhost:8000/api/copypaste/videos

# Job : N vidéos depuis UNE vidéo (auto-ajoutée à la banque)…
curl -X POST localhost:8000/api/copypaste/jobs -H 'Content-Type: application/json' -d '{
  "model_id": "<model_id>", "count": 3,
  "reference_video_url": "https://<r2>/uploads/<tenant>/ref.mp4",
  "custom_prompt": "keep the exact same outfit and camera movement"
}'
# …ou en piochant au hasard dans la banque
curl -X POST localhost:8000/api/copypaste/jobs -H 'Content-Type: application/json' \
  -d '{"model_id": "<model_id>", "count": 10, "use_bank": true}'
# → GenerationJob standard (catégorie copypaste) : suivi/review/export via /api/jobs
```

Le nom du champ input kie.ai portant les vidéos de référence est configurable via
`KIE_SEEDANCE_VIDEO_REF_FIELD` (défaut `reference_video_urls`) — à ajuster si la doc
Seedance change sans avoir à redéployer le code.

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

## Import d'assets & gestion (Réglages)

- **Auto-description en masse** : `POST /api/banks/{outfits,backgrounds}/bulk-describe`
  `{image_urls[], suffix}` — chaque image est décrite automatiquement par le LLM
  vision (outfit → phrase après « wearing », background → phrase de lieu), avec un
  **suffixe fourni par l'utilisateur** ajouté en fin de chaque description (jamais
  hardcodé). Les assets restent en `pending` (non tirés à la composition) jusqu'à
  ce que la description soit prête.
- **Gestion** : toutes les banques (outfits, backgrounds, templates, dialogues,
  captions, voix) et les models/caractéristiques sont listées dans Réglages avec
  suppression. `DELETE /api/models/{id}`, `DELETE /api/models/{id}/characteristics/{cid}`,
  et les `DELETE /api/banks/{kind}/{id}` existants.
- **Migrations** : `init_db` applique des migrations additives idempotentes
  (`ALTER TABLE … ADD COLUMN IF NOT EXISTS`) au pré-déploiement → pas besoin d'Alembic
  ni de reset pour les colonnes récemment ajoutées.

## Répliquer une vidéo (reverse-engineering vidéo)

Upload d'une vidéo à répliquer → `POST /api/banks/templates/reverse-video`
`{source_video_url, category, speaking}` :
1. FFmpeg extrait ~6 images-clés réparties sur la durée.
2. Le LLM vision (Gemini) en tire un **template réutilisable** capturant l'action,
   le mouvement de caméra, le cadrage, le rythme et l'ambiance — **sans** l'outfit,
   le décor ni le visage (remplacés). `ensure_slots` garantit les slots
   `{outfit} {background} {characteristics} {dialogue}`.
3. Le template est stocké comme `prompt_template` (statut `pending → ready`) dans sa
   catégorie ; un template `pending` n'est pas tiré à la composition.
4. Ensuite il se génère **comme n'importe quel template** : ta model remplace la
   personne d'origine, tes assets se mélangent. Le prompt est gardé à vie.

Modèles configurés : **Seedance 2.0 Fast** (720p, moins cher, `bytedance/seedance-2-fast`)
et **Nano Banana Pro** (`google/nano-banana-pro`, meilleure consistance personnage).

## Format long 30 s (`storytelling_long`)

Seedance 2.0 Fast plafonne à 15 s par génération. Pour des vidéos de **30 s**,
reverse-engineer une vidéo dans la catégorie **`storytelling_long`** : la scène ET
les paroles sont reprises de la vidéo de référence (jamais des dialogues manuels),
et le décor reste celui de la vidéo (aucune banque de backgrounds n'est piochée —
`ensure_slots(..., with_background=False)`).

À la génération, `generate_long_form_item` enchaîne **2 clips de 15 s** :

1. Le transcript (voix de la vidéo, transcrit par ElevenLabs Scribe) est **coupé en
   deux** (`split_transcript_halves`, équilibré par mots, de préférence entre phrases) :
   moitié 1 sur le clip 1, moitié 2 sur le clip 2.
2. **Clip 1** est généré (moitié 1 des paroles), puis sa **dernière frame** est
   extraite (`extract_last_frame`) et uploadée sur R2.
3. **Clip 2** démarre depuis cette dernière frame (référence prioritaire) avec la
   moitié 2 des paroles → **même décor, même tenue, enchaînement continu**.
4. Voice-swap par clip (best-effort), `concat_clips` (2×15 s → 30 s), assemblage,
   upload R2.

Seuls la **model** (fixe par job) et l'**outfit** (tiré par item) varient d'une vidéo
long-format à l'autre. La chaîne est **synchrone** (poll `kie.ai/recordInfo` entre les
étapes) car le clip 2 dépend du clip 1.

## Pictures — nano banana (génération de photos)

Même logique que la vidéo, appliquée à l'image fixe :

1. **Upload d'une image de référence** (vers R2) → `POST /api/pictures/prompts`
   `{source_image_url}` : l'image est **reverse-engineerée en prompt** par un LLM
   vision (endpoint OpenAI-compatible kie.ai, modèle configurable via
   `KIE_VISION_MODEL` / `KIE_VISION_BASE_URL`). Le prompt est **sauvegardé à vie**
   (`picture_prompts`, statut `pending → ready`) et réutilisable.
2. **Génération en masse** → `POST /api/pictures/jobs`
   `{model_id, count, image_size, budget_cap_usd}` : le moteur tire un prompt +
   un outfit, injecte les caractéristiques, envoie **visage + caractéristiques +
   outfit** en référence à `nano-banana-edit` (cap 10 refs) → consistance
   personnage. Compose dédupliqué → gate budget → dispatch → webhook.
3. **Post-traitement** : QC face-match (embedding directement sur l'image) →
   **scrub complet des métadonnées** (EXIF/XMP/IPTC/ICC/**C2PA**) → upload R2.
4. **Review / export** : `POST /api/pictures/items/{id}/review`,
   `GET /api/pictures/jobs/{id}/export`.

Estimation live : `POST /api/pictures/estimate` (tarif `per_image` depuis la
table `pricing`, taux QC calibré).

> ⚠️ **SynthID** : le watermark de provenance de Google est encodé dans les
> **pixels**, pas dans les métadonnées. Le scrub retire toute métadonnée lisible
> (y compris l'étiquette C2PA « généré par IA » lue par les plateformes) mais
> **ne retire pas** le watermark pixel — ce n'est pas ce que fait ce module et
> ce n'est pas ce qui déclenche le labeling/ban côté plateforme (c'est le C2PA).

## Sécurité

Durcissements en place (revue de sécurité passée) :
- **Auth** : sessions signées HMAC liées à `token_version` (un changement de mot
  de passe révoque tous les jetons émis avant, y compris volés) ; hachage PBKDF2
  salé + comparaison constante ; login à temps constant (hash factice au même coût
  pour un email inconnu → pas d'énumération).
- **Fail-fast prod** : l'app refuse de démarrer en https si `SECRET_KEY`
  (< 32 octets ou valeur par défaut) ou `KIE_WEBHOOK_SECRET` manquent.
- **Webhook** : `POST /api/webhooks/kie` exige le secret partagé (`?secret=…`) ;
  refusé (503) si aucun secret n'est configuré.
- **Anti-SSRF** : tout téléchargement (`app/net.safe_download`) n'accepte que
  http/s public — DNS résolu, IP privées/loopback/link-local (169.254.169.254…)
  bloquées, redirections re-validées à chaque saut, taille plafonnée. Les URLs
  d'assets sont aussi validées (schéma) à l'entrée d'API.
- **XSS** : le frontend échappe toute donnée non fiable (prompts LLM, captions,
  erreurs) avant insertion, et n'accepte que des URLs http(s) en `src`.
- **FFmpeg** : commandes en listes d'args (jamais `shell=True`) ; `drawtext`
  avec `expansion=none` (le caption utilisateur n'interprète pas `%{…}`).
- **Pillow** : garde-fou decompression bomb (`MAX_IMAGE_PIXELS`) sur les images
  tierces.

- **Isolation multi-tenant** : chaque ressource (models, banques, jobs, prompts,
  contenu généré) est estampillée avec le `tenant_id` de son créateur ; toute
  lecture/écriture est filtrée par le tenant de l'utilisateur, et un accès
  cross-tenant renvoie **404** (on ne révèle pas l'existence). La composition
  Celery ne tire que dans les banques du tenant du job. Le seul état partagé
  reste la table `pricing` (tarifs) et le `calibration_log` (heuristique QC).

## Authentification (Phase 5)

Accès restreint par session (cookie signé HMAC, mot de passe haché PBKDF2 —
zéro dépendance externe). `init_db` crée le compte propriétaire depuis
`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` (défaut : `sydeincovind@gmail.com`).

À faire au premier démarrage en prod :
1. Générer une vraie clé : `SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")`
   (changer `SECRET_KEY` invalide toutes les sessions existantes).
2. Se connecter sur `/login`, puis changer le mot de passe via
   `POST /api/auth/change-password` (`{current_password, new_password}`).
3. Définir `KIE_WEBHOOK_SECRET` : le webhook kie.ai n'utilise pas de cookie
   (l'appelant est kie.ai), il est authentifié par ce secret partagé passé
   en query (`/api/webhooks/kie?secret=…`, injecté automatiquement dans le
   `callBackUrl`).

Endpoints : `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`,
`POST /api/auth/change-password`. Table `users` (`tenant_id`, `email`,
`password_hash`, `role` owner/member) — prête pour le multi-tenant.

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
- ~~**Phase 4** — panneau de génération (UI), estimateur live, grille de review, celery beat~~ ✔
- ~~**Phase 5** — auth + accès restreint (login, sessions, compte propriétaire)~~ ✔
- **Reste Phase 5+** — multi-tenant complet (scoping des données par `tenant_id`),
  billing, Alembic (create_all suffit tant que la base n'est pas encore déployée)
- ~~**Pictures** — nano banana : upload → reverse-engineering → banque de prompts →
  génération de photos consistantes → scrub métadonnées~~ ✔ (voir section dédiée)
