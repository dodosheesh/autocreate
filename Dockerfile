# Image unique pour les deux services Railway (web + worker).
# FFmpeg + polices sont embarqués : indispensables à l'assemblage vidéo et à
# l'overlay caption (drawtext). insightface (QC) reste hors image (extra lourd,
# off par défaut) — l'installer via un build custom si QC_ENABLED=true.
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fontconfig \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Couche deps mise en cache tant que pyproject ne change pas
COPY pyproject.toml README.md ./
COPY app ./app

# Editable install : les fichiers statiques (app/static) restent résolus depuis
# /app par app.main, et les dépendances sont installées.
RUN pip install --no-cache-dir -e .

# On NE hardcode PAS PORT : Railway l'injecte au runtime et l'app le suit.
# 8080 sert seulement de repli en local. (EXPOSE est documentaire, Railway
# l'ignore et route via la variable PORT.)
EXPOSE 8080

# Commande par défaut = service web, en FORME SHELL (indispensable : la forme
# exec JSON n'expanserait pas $PORT → uvicorn recevrait la chaîne littérale).
# Le service worker surcharge cette commande dans Railway (voir README).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
