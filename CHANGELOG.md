# Changelog

## Non publié

- Le workflow `image.yml` appelle le webhook Portainer (secret `PORTAINER_WEBHOOK_URL`) une fois la branche `deploy` avancée. Le redéploiement suit la publication de l'image en quelques secondes, au lieu d'attendre l'interrogation de Git. Sans secret, l'étape ne fait rien.

## 0.3.0

### Ajouts

- **Ne rien transcrire quand personne ne parle.** Après un faux réveil, Whisper inventait un texte (`" ..."`) qui partait vers Ollama, et Jarvis répondait à voix haute. Désormais, WhoSpeaks renvoie un texte vide, et HA termine le run sur `stt-no-text-recognized`, sans conversation ni réponse :
  - si le VAD mesure moins de `silence_max_s` de parole (0,3 s par défaut). La réponse part sans attendre Whisper, et son texte est ignoré. Résultat `ignoree_silence` ;
  - si le texte de Whisper, normalisé, est vide, n'est que de la ponctuation ou figure dans la liste `hallucinations` (entrées `~` : expressions régulières). Résultat `ignoree_hallucination`.
  - L'événement `transcript` porte le champ `ignored`. Le capteur `sensor.jarvis_interlocuteur` n'est pas publié pour ces phrases.
  - Les phrases sont gardées dans le journal, avec leur audio, sous deux nouveaux filtres. Elles ne nourrissent ni les groupes de voix inconnues ni les échantillons.
- Nouveaux réglages : `filtre_silence_actif` (défaut `true`), `silence_max_s` (0 à 2) et `hallucinations` (liste éditable, une entrée par ligne).
- `/api/etat` compte les phrases ignorées à part (`ignorees`).

### Changements

- Le VAD tourne même si le modèle d'empreinte n'est pas chargé, pour détecter le silence.
- Test sur vraies voix : attente d'identification élargie, car le test échouait sous forte charge, sans rapport avec ce changement.

## 0.2.1

### Ajouts

- **Déploiement automatique sur le Beelink (Portainer).**
  - Le workflow GitHub Actions `image.yml` lance les tests, publie `ghcr.io/powange/whospeaks:latest` et `:sha-<court>` (`linux/amd64`, cache GHA), puis avance la branche `deploy` une fois l'image publiée.
  - Une construction plus ancienne encore en cours est annulée, pour qu'elle ne republie pas une image plus vieille.
- `deploy/docker-compose.beelink.yml` : stack autonome pour Portainer (image GHCR, `pull_policy: always`, données sur disque local).
- Le commit de l'image (`WHOSPEAKS_COMMIT`, passé au build) est exposé dans `/api/etat` (`commit`). L'en-tête de la page affiche la version et ce commit.

### Documentation

- README : section « Déploiement Portainer (Beelink) ». Le `docker-compose.yml` d'origine reste pour un déploiement à côté de Whisper.

## 0.2.0

### Ajouts

- **Reprise de l'interlocuteur sur les phrases courtes.** Une phrase trop courte pour une empreinte (« stop », « allume la lumière ») reprend la personne identifiée ou enrôlée il y a moins de `reprise_duree_s` secondes, avec le nouveau résultat `reprise`.
  - Aucune empreinte n'est calculée et aucun échantillon n'est créé.
  - La mémoire n'est pas rafraîchie par une reprise.
  - La mémoire est gardée par satellite si HA l'indique dans `transcribe`, sinon globalement. Le journal indique une fois `satellite context: present/absent` à la première phrase reçue.
  - Nouveaux attributs du capteur : `reprise` et `age_s`.
  - Dans la page : badge « reprise » et filtre « Reprises » dans le journal. « Corriger… » ne change que l'étiquette ; « ✗ pas lui/elle » remet la phrase en « trop court ».
  - Nouveaux réglages : `reprise_active` (défaut `true`) et `reprise_duree_s` (défaut 120, de 0 à 600).
- **Validation des réglages** envoyés par la page : type attendu, et bornes pour `reprise_duree_s`. Une valeur invalide renvoie une erreur 400.
- **`TRUSTED_PROXIES`** : derrière un proxy inverse, l'adresse du client est lue dans `X-Forwarded-For` (première IP non fiable en partant de la droite) ou dans `X-Real-IP`, seulement si la requête vient d'un proxy de la liste. `UI_LOCAL_ONLY` s'applique à cette adresse. En-tête `Strict-Transport-Security` sur les requêtes HTTPS.

### Changements

- uvicorn est lancé avec `proxy_headers=False` : seul WhoSpeaks interprète les en-têtes `X-Forwarded-*`.
- `/api/etat` compte les reprises à part (`reprises`). `phrases` ne les inclut plus.
- L'enrichissement automatique ne concerne explicitement que les phrases `identifie`.

## 0.1.0

Première version : proxy STT Wyoming avec identification du locuteur, capteur et événement Home Assistant, regroupement des voix inconnues, enrôlement, enrichissement automatique, calibrage, page web d'étiquetage, script `whospeaks-evaluate`, image Docker.
