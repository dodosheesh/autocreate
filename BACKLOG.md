# Backlog — features notées pour après la stack actuelle

## ✅ Feature « Pictures » (nano banana) — LIVRÉE

Implémentée : tables `picture_prompts`/`picture_jobs`/`picture_items`, reverse-
engineering vision (`app/integrations/vision.py`), génération nano-banana-edit
(consistance visage + caractéristiques + outfits), scrub métadonnées Pillow
(`app/media/scrub.py`), API `/api/pictures/*`, section UI. Voir README §Pictures.

Rappel de la limite tenue : scrub métadonnées (EXIF/XMP/IPTC/C2PA) oui,
retrait du watermark pixel SynthID non.

---

## (Archive de la spec initiale) Feature « Pictures » (nano banana)

Génération de photos en masse de la model, même logique que la vidéo :

- **Upload de contenu de référence** : l'utilisateur uploade des images ;
  chaque image est reverse-engineerée en prompt (via un modèle vision),
  puis le prompt est **adapté à la description de la model**.
- **Banque de prompts persistante** : chaque prompt reverse-engineeré est
  sauvegardé à vie (table `picture_prompts`) et réutilisable pour de futures
  générations.
- **Consistance du personnage** : la photo visage de la model
  (`face_reference_url`) est passée en référence à nano banana, plus les
  caractéristiques spéciales (`model_characteristics`, photos + hints)
  pour la précision du personnage — même mécanique que Seedance.
- **Mix d'assets** : les outfits (et autres banques) se mélangent au prompt
  de base, tirage pondéré + dédup, comme le moteur de variation vidéo.
- **1 clic → batch** : N photos qualitatives et fidèles, mêmes patterns que
  `/api/jobs/batch` (compose → estimate/gate → dispatch → webhook → post).
- **Nettoyage avant téléchargement** : suppression complète des métadonnées
  lisibles (EXIF, XMP, IPTC, C2PA) sur chaque photo livrée.
  ⚠️ Limite actée : SynthID est un watermark de provenance encodé dans les
  pixels par Google — sa suppression ne sera pas implémentée (contournement
  d'un mécanisme d'identification de contenu IA). Le scrub couvre toutes
  les métadonnées, pas le watermark pixel.

Infra pressentie : nano banana via kie.ai (même gateway, même table pricing —
une ligne à ajouter), nouvelle catégorie d'items `picture` ou table dédiée,
réutilisation de compose/estimate/QC face-match tels quels.
