# Changelog

## 0.8.0

### Latence

- **Transcription anticipée** (`anticipation_active`, activée par défaut).
  - Dès `anticipation_silence_s` de silence après de la parole (0,4 s), Whisper transcrit l'audio reçu, sur une connexion à part, pendant que HA attend pour déclarer la fin de parole. L'identification part en même temps, sans effet tant qu'elle n'est pas retenue.
  - Abandon si la parole reprend, une seule à la fois. À l'`audio-stop`, le texte anticipé est renvoyé dès qu'il est prêt, et Whisper ne transcrit pas une deuxième fois.
  - Journal : `anticipation` (`utilisee`, `abandonnee`, `aucune`), nombre d'abandons, `gain_anticipation_s`.
- **Le texte part en premier.** Plus aucune attente de HA avant le texte : il n'y a plus la courte attente du capteur publié pendant Whisper. Nouvelle mesure `envoi_s` (réponse de Whisper → texte écrit vers HA) ; `total_s` va jusqu'à cette écriture. La surveillance de la boucle couvre la phrase jusqu'à l'envoi du texte.
- **Heures UTC à la milliseconde** dans le capteur de temps (`fin_flux_recue_utc`, `whisper_recu_utc`, `texte_envoye_utc`), à comparer avec `stt-vad-end` et `stt-end` du debug du pipeline de HA.
- **Mode direct** `UPSTREAM_OPENAI_URL` : WhoSpeaks appelle l'API OpenAI de `whisper-server` sans passer par `wyoming_openai`, avec le prompt du vocabulaire à chaque requête. `UPSTREAM_MODE` : `auto`, `wyoming` ou `openai`.

### Mesures

- **Banc d'essai « Mesurer Whisper »** (onglet Réglages, `POST /api/whisper/banc`) : trois extraits en français de 2, 4 et 8 s (domaine public, dans `whospeaks/bench/`), trois essais chacun, médiane, pour chaque chemin (Wyoming et API OpenAI directe).
- La carte Whisper indique le mode utilisé et l'adresse de l'API OpenAI.
- README : transcription anticipée, mode direct, banc d'essai, réglages conseillés de `whisper-server` pour le GEEKOM (modèle turbo, quantification, faisceau, repli en température).

## 0.7.0

### Latence

- **Lecture de l'audio de HA sans blocage.** Chaque morceau est mis en file vers Whisper, et une tâche à part l'écrit au rythme de Whisper : un Whisper ou un relais lent ne ralentit plus la lecture de HA, ni sa détection de fin de parole. La conversion audio passe dans le thread d'analyse.
- **`TCP_NODELAY`** sur la connexion de HA et sur celle vers Whisper.
- **Texte envoyé sans attendre HA.** Le capteur `sensor.jarvis_interlocuteur` est publié dès l'identification, pendant que Whisper transcrit. À l'arrivée du texte, il n'y a plus d'attente de HA (jusqu'à 0,3 s auparavant). L'événement `jarvis_interlocuteur` (avec le texte) part juste après le texte.
- La page web n'interroge plus la base dans la boucle du proxy (`/api/etat`, `/api/vocabulaire`).
- Calcul du prompt linéaire (il grossissait au carré du nombre de termes).

### Mesures

- Nouveaux temps par phrase : `retard_audio_s` (retard pris par l'audio de HA, horloge de l'audio contre horloge murale), `blocage_boucle_s` (plus long blocage de WhoSpeaks pendant la phrase), `relais_whisper_s` (file vers Whisper). `whisper_s` est désormais le temps propre de Whisper, à partir de l'envoi effectif de la fin du flux. Les trois apparaissent dans le détail, dans les médianes sur 24 h et dans les capteurs de temps.
- Onglet Réglages : ce que Whisper annonce (programme, modèles, version) ; `GET /api/whisper`.
- README : comment lire ces mesures, et comment accélérer `whisper-server` (modèle turbo, faisceau, repli en température).

### Vocabulaire

- Mots mal compris ajoutés d'office : « Elle éteint » → « éteins », « Elle allume » → « allume » (retirables, pas remis ensuite).
- Détection de la recopie du prompt plus stricte : une vraie commande qui contient des termes du prompt (« Allume l'imprimante 3D ») n'est plus prise pour une recopie.

## 0.6.0

### Ajouts

- **Vocabulaire de la maison pour Whisper** (onglet Vocabulaire).
  - Construit depuis HA par l'API WebSocket, au démarrage puis toutes les 15 min : noms et alias des entités partagées avec Assist, pièces et étages. S'y ajoutent les prénoms enrôlés, les « Mots en plus » saisis à la main et les mots mal compris.
  - Le prompt est une liste de termes, tronquée par priorité sous `vocab_max_tokens` (200).
  - Le dernier relevé de HA est gardé dans `data/vocabulaire_ha.json`.
  - Transmission : `wyoming_openai` n'accepte pas de prompt par phrase. Le prompt se copie dans sa variable `STT_PROMPT` (marche à suivre dans le README ; `GET /api/vocabulaire/prompt`). Les termes sont aussi envoyés dans le champ Wyoming `transcript_terms`, ignoré par le pont pour l'instant.
  - Un texte qui recopie le prompt est classé `ignoree_hallucination`.
- **Temps de chaque étape** pour chaque phrase : audio reçu, parole, attente de la fin de parole côté HA, Whisper, identification et attente de l'identification, total.
  - Affichés dans le journal : colonne Temps, détail ⓘ, et médiane et 90e percentile sur 24 h par satellite (`GET /api/temps`).
  - Publiés dans HA : `sensor.whospeaks_temps_whisper` et `sensor.whospeaks_temps_total`.
- **Corriger le texte** d'une phrase (détail ⓘ du journal). Les mots remplacés (« impliment » → « imprimante ») passent en tête du vocabulaire, et sont listés dans « Mots souvent mal compris ». Le texte envoyé à HA reste celui de Whisper.
- Réglages `mots_en_plus` et `vocab_max_tokens` (20 à 224).
- Nouvelle dépendance : `websockets`.

## 0.5.1

### Ajouts

- **Renommer une personne** depuis l'onglet Personnes (bouton ✎). Les échantillons, le journal et la reprise suivent. La correspondance nom → `person.*` des Réglages et l'enrôlement en cours prennent le nouveau nom. Un nom vide ou déjà pris est refusé.

## 0.5.0

### Ajouts

- **Satellite d'origine de chaque phrase.** HA ne l'envoie pas dans `transcribe`, WhoSpeaks le lui demande au début de chaque phrase (`POST /api/template` : entité `assist_satellite.*` en écoute, la plus récente s'il y en a plusieurs). Le contexte HA reste prioritaire s'il arrive un jour.
  - La requête part en tâche de fond pendant l'audio, sans retarder Whisper ni la réponse. HA lent ou coupé donne un satellite inconnu (« ? »). Délai de 1 s, avertissement au plus une fois par minute.
  - Journal : pièce (ou nom du satellite) dans `phrases.satellite`, entité dans la nouvelle colonne `phrases.satellite_entity` (ajoutée aux bases existantes au démarrage).
  - Capteur `sensor.jarvis_interlocuteur` et événement `jarvis_interlocuteur` : attributs `satellite` et `satellite_entity`.
  - La reprise de l'interlocuteur est désormais réellement gardée par satellite (clé : l'entité).
  - Page Journal : badge du satellite en tête de chaque phrase, filtre par satellite (aussi appliqué à « Tout supprimer »), et phrases ignorées par satellite sur 24 h (surligné au-delà de la moitié).
  - API : paramètre `satellite` sur `GET /api/phrases`, `GET /api/phrases/compte` et `DELETE /api/phrases` (`?` : satellite inconnu) ; nouveau `GET /api/satellites`.
  - Journal du service : `satellite source: context|ha|inconnu` à chaque changement d'origine.

## 0.4.1

### Ajouts

- **Journal : « Tout supprimer »** pour les filtres « Trop courtes », « Ignorées (silence) », « Ignorées (hallucination) » et « Erreurs ». Le bouton supprime toutes les phrases du filtre et leur audio, pas seulement les 150 affichées, après confirmation du nombre. API : `GET /api/phrases/compte?filtre=…` et `DELETE /api/phrases?filtre=…`. Les autres filtres sont refusés (erreur 400).
- Filtre « Erreurs » ajouté au menu du journal.

## 0.4.0

### Ajouts

- **Enrôlement silencieux.** Pendant un enrôlement par les satellites, Jarvis ne répond plus à chaque phrase lue : WhoSpeaks renvoie un texte vide à HA, et le run se termine sur `stt-no-text-recognized`, sans appel à Ollama.
  - Concerne toutes les phrases reçues pendant l'enrôlement, comptées ou non (un « euh » trop court ne part pas non plus vers Ollama).
  - Les phrases comptent normalement, et restent dans le journal avec leur texte.
  - L'événement `transcript` porte `ignored: enrolement`. Pas de mise à jour du capteur.
  - Si l'analyse est trop lente, la décision repose sur l'état courant de l'enrôlement.
  - Réglage `enrolement_silencieux` (défaut `true`), indépendant du filtre de silence.
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
