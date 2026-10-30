# WhoSpeaks

Reconnaissance du locuteur pour l'assistant vocal Home Assistant « Jarvis ».

WhoSpeaks se place entre Home Assistant et Whisper sous la forme d'un proxy STT Wyoming. Pendant que Whisper transcrit, il calcule l'empreinte vocale de la phrase et la compare aux personnes connues. Il met ensuite à jour `sensor.jarvis_interlocuteur` dans HA, puis renvoie le texte de Whisper sans le modifier.

Les voix non reconnues sont conservées et regroupées. On peut les attribuer après coup depuis une page web (port 8095).

> ⚠️ **Ce n'est pas un système de sécurité.** Une voix enregistrée peut tromper le modèle. Ne pas s'en servir pour une serrure, une alarme ou un achat.

```
Satellite → HA (pipeline Jarvis) ──Wyoming──▶ WhoSpeaks :10302 ──▶ wyoming-whisper-gpu :10300
                                                 ├─ VAD (Silero) + empreinte (sherpa-onnx, CPU)
                                                 ├─ POST HA : capteur + événement (avant le texte)
                                                 └─ SQLite + extraits WAV ◀── page web :8095
```

## Installation (GEEKOM, Docker Desktop)

1. Cloner le dépôt à côté du `docker-compose.yml` de `wyoming-whisper-gpu`, ou laisser Compose construire depuis GitHub.
2. Copier le service `whospeaks` de [docker-compose.yml](docker-compose.yml) dans le `docker-compose.yml` existant. Le mettre dans le même fichier donne un réseau commun, et l'amont `tcp://wyoming-whisper-gpu:10300` est alors résolu.
3. Ajouter au `.env` (voir [.env.example](.env.example)) :
   - `WHOSPEAKS_HA_TOKEN` : jeton longue durée HA (Profil → Sécurité) ;
   - `WHOSPEAKS_UI_TOKEN` : une chaîne aléatoire qui protège la page.
4. Lancer `docker compose up -d --build whospeaks`.

Au premier démarrage, les modèles (Silero VAD, puis le modèle d'empreinte) sont téléchargés depuis les releases GitHub de sherpa-onnx et mis en cache dans `./data/models`. Pendant ce téléchargement, ou si le modèle est absent, le proxy reste transparent : le texte passe sans identification.

Ce [docker-compose.yml](docker-compose.yml) sert à un déploiement **à côté de Whisper**, sur le GEEKOM. Pour le Beelink, voir la section suivante.

## Déploiement Portainer (Beelink)

Sur le Beelink (192.168.0.13), Portainer suit une stack Git et met WhoSpeaks à jour tout seul à chaque push sur `main` :

1. Le workflow [image.yml](.github/workflows/image.yml) lance les tests. S'ils passent, il construit l'image `linux/amd64` et la publie sous `ghcr.io/powange/whospeaks:latest` et `:sha-<commit court>`.
2. **Ensuite seulement**, il avance la branche `deploy` sur ce commit. Portainer ne voit donc jamais un commit dont l'image n'existe pas encore.
3. Portainer voit que `deploy` a bougé, tire `latest` et recrée le conteneur.

Portainer ne reconstruit pas toujours une image `build:` lors d'un redéploiement Git. C'est pour ça qu'il ne fait que tirer une image construite par GitHub Actions.

### Mise en place (une fois)

1. **Premier passage du workflow** : pousser sur `main`, ou le lancer à la main (Actions → Image → Run workflow). Il crée la branche `deploy` et le paquet GHCR.
2. **Rendre le paquet public** : GitHub → profil → Packages → `whospeaks` → Package settings → Change visibility → Public. Sinon, Portainer a besoin d'identifiants de registre pour `ghcr.io`.
3. **Créer la stack dans Portainer** : Stacks → Add stack → Repository.
   - Repository URL : `https://github.com/powange/whospeaks`
   - Repository reference : `refs/heads/deploy`
   - Compose path : `deploy/docker-compose.beelink.yml`
   - GitOps updates : activé, mécanisme **Polling**, intervalle `5m`
   - **Re-pull image** : activé (indispensable : le tag `latest` ne change pas de nom)
4. **Variables d'environnement de la stack** :

   | Variable | Obligatoire | Défaut |
   |---|---|---|
   | `WHOSPEAKS_HA_TOKEN` | oui | — |
   | `WHOSPEAKS_UI_TOKEN` | oui | — |
   | `UPSTREAM_URI` | non | `tcp://192.168.0.11:10301` (Whisper du GEEKOM) |
   | `HA_URL` | non | `http://192.168.0.201:8123` |
   | `FRAME_ANCESTORS` | non | `https://ha.powange.com` |
   | `TRUSTED_PROXIES` | non | vide : aucun proxy de confiance. Mettre l'IP de NPM si la page passe par lui |
   | `MODEL` | non | `campplus-zh-en` |
   | `WHOSPEAKS_DATA` | non | `/home/powange/docker_workspaces/whospeaks/data` |

`WHOSPEAKS_DATA` doit être sur un **disque local**, jamais sur le NFS : SQLite (en mode WAL) n'y est pas fiable.

Après un push, la page affiche la version et le commit de l'image dans l'en-tête, par exemple `v0.2.1 (abc1234)`. On vérifie ainsi que Portainer a redéployé. Compter jusqu'à 5 min après la fin du workflow.

En cas de problème, le tag `:sha-<commit>` permet de revenir temporairement à une image précise, en le mettant à la place de `latest` dans la stack.

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `UPSTREAM_URI` | `tcp://wyoming-whisper-gpu:10300` | serveur STT Wyoming amont |
| `HA_URL` | — | ex. `http://192.168.0.201:8123` |
| `HA_TOKEN` | — | jeton longue durée HA |
| `UI_TOKEN` | — | jeton de la page et de l'API (en-tête `Authorization: Bearer`, `X-Token` ou `?token=`) |
| `MODEL` | `campplus-zh-en` | modèle d'empreinte (voir ci-dessous) ; la page Réglages peut le changer |
| `LANG` | `fr` | langue annoncée si l'amont ne répond pas |
| `UI_LOCAL_ONLY` | `true` | refuse les clients hors réseau privé |
| `TRUSTED_PROXIES` | — (vide) | proxys inverses de confiance : IP ou réseaux, séparés par des virgules ou des espaces (voir ci-dessous) |
| `FRAME_ANCESTORS` | — | origines en plus de `HA_URL` autorisées à afficher la page en iframe |
| `HA_SENSOR` / `HA_EVENT` | `sensor.jarvis_interlocuteur` / `jarvis_interlocuteur` | noms côté HA |
| `HA_TIMEOUT` | `0.3` | délai max (s) de la notification HA |
| `ID_EXTRA_WAIT` | `0.15` | attente max (s) de l'identification après le texte de Whisper |
| `NUM_THREADS` | `4` | threads ONNX pour l'empreinte |
| `TZ` | — | fuseau horaire (horodatages) |

### Derrière un proxy inverse

Derrière un proxy inverse (Nginx Proxy Manager, Traefik, Caddy…), la page voit l'adresse du proxy et non celle du client. `UI_LOCAL_ONLY` laisserait alors passer tout le monde, puisque le proxy a une adresse privée. Il faut déclarer le proxy :

```yaml
TRUSTED_PROXIES: 172.18.0.0/16, 192.168.0.20
```

- Les en-têtes `X-Forwarded-For`, `X-Real-IP` et `X-Forwarded-Proto` ne sont lus que si la connexion vient d'une adresse de cette liste. Venant d'ailleurs, ils sont ignorés.
- Dans `X-Forwarded-For`, l'adresse du client est **la première adresse non fiable en partant de la droite**. Chaque proxy ajoute à droite l'adresse qu'il voit ; ce qui est plus à gauche a pu être écrit par le client lui-même. Un `X-Forwarded-For: 192.168.0.50` envoyé depuis Internet ne fait donc pas passer pour local.
- `X-Real-IP` ne sert qu'en l'absence de `X-Forwarded-For`. Le proxy doit le **remplacer**, pas le recopier depuis la requête (dans Nginx : `proxy_set_header X-Real-IP $remote_addr;`).
- Le filtre `UI_LOCAL_ONLY` s'applique à l'adresse ainsi obtenue.
- Quand la requête arrive en HTTPS (`X-Forwarded-Proto: https` venant d'un proxy de confiance), la réponse porte `Strict-Transport-Security: max-age=31536000`. L'en-tête est sans `includeSubDomains`, pour ne pas imposer HTTPS aux autres services du domaine.
- uvicorn est lancé avec `proxy_headers=False` : seul WhoSpeaks interprète ces en-têtes, selon `TRUSTED_PROXIES`. La variable `FORWARDED_ALLOW_IPS` d'uvicorn est donc sans effet.

Ne mettre dans la liste que les proxys eux-mêmes, au plus juste. Déclarer tout le réseau local permettrait à n'importe quelle machine du réseau d'annoncer une fausse adresse.

Ce réglage ne concerne que la page web (8095), pas le port Wyoming (10302).

### Modèles disponibles

| Clé | Modèle | Dim. |
|---|---|---|
| `campplus-zh-en` (défaut) | 3D-Speaker CAM++ zh/en | 192 |
| `campplus-voxceleb` | WeSpeaker CAM++ VoxCeleb | 512 |
| `eres2net-base` | 3D-Speaker ERes2Net base | 512 |
| `eres2netv2` | 3D-Speaker ERes2NetV2 | 192 |
| `resnet34` / `resnet34-lm` | WeSpeaker ResNet34 VoxCeleb | 256 |
| `titanet-small` | NeMo TitaNet small | 192 |

`MODEL` accepte aussi un nom de fichier `.onnx` placé dans `data/models/`, ou une URL.

Si l'on change de modèle, toutes les empreintes sont recalculées à partir de l'audio conservé. C'est pourquoi l'option « garder l'audio des échantillons » est activée par défaut.

## Côté Home Assistant

1. **Intégration** : ajouter une intégration Wyoming vers `192.168.0.11:10302`, puis choisir ce moteur STT dans le pipeline Jarvis. L'ancien `10301` reste disponible en secours.
2. **Capteur** : `sensor.jarvis_interlocuteur` est créé au premier message.
   - État : nom de la personne, `inconnu` ou `trop court`.
   - Attributs : `person_entity`, `score`, `marge`, `cluster`, `transcript`, `horodatage`, `duree_parole`, `resultat`, `reprise`, `age_s` (voir « Comment une phrase est classée »).
3. **Événement** : `jarvis_interlocuteur` porte les mêmes données, avec `interlocuteur` pour le nom. Les deux messages partent **avant** le renvoi du texte.
4. **Prompt Ollama** : utiliser `last_updated` plutôt que `last_changed`. Si la même personne parle deux fois de suite, l'état ne change pas, donc `last_changed` non plus, alors que `last_updated` suit l'attribut `horodatage`.
   ```jinja
   Interlocuteur probable : {{ states('sensor.jarvis_interlocuteur') if (now() - states.sensor.jarvis_interlocuteur.last_updated).total_seconds() < 20 else 'inconnu' }}
   ```
5. **Enrôlement depuis HA, carte iframe, annonce de l'avancement** : voir [homeassistant/whospeaks.yaml](homeassistant/whospeaks.yaml). La carte iframe se configure avec `url: http://192.168.0.11:8095/?token=<UI_TOKEN>`.

Le texte transmis à HA est strictement celui de Whisper. Seuls deux champs sont ajoutés à l'événement Wyoming `transcript` : `speaker` et `speaker_score`.

## Utilisation

- **Enrôler** (onglet Personnes) : créer la personne, cliquer sur « Enrôler par les satellites », puis dire 12 phrases variées d'au moins 2 s (« Jarvis… » puis la phrase). Chaque phrase d'au moins 1,5 s est comptée, quel que soit le résultat d'identification. L'événement `whospeaks_enrolement` {personne, n, total} suit l'avancement. On peut aussi envoyer des fichiers WAV/MP3 ou enregistrer dans le navigateur (le micro du navigateur exige HTTPS ou localhost).
- **Journal** : chaque phrase, avec son résultat, son score et son extrait audio.
  - « Corriger… » attribue la phrase à une autre personne. L'éventuel échantillon automatique fautif est retiré.
  - « ✗ pas lui/elle » remet la phrase en voix inconnue.
- **Voix inconnues** : groupes triés par taille, avec trois extraits chacun. On peut les attribuer, créer une personne, les fusionner, les ignorer (télé, invité) ou les supprimer. « Recalculer les groupes » relance un regroupement complet (agglomératif) en tâche de fond.
- **Réglages** : seuils, enrichissement automatique, rétention, modèle, calibrage et sauvegarde (export ou import zip).
  - Le **calibrage** propose `seuil_identification` et `marge_min`, avec les taux d'erreur estimés par validation croisée sur les échantillons.

### Comment une phrase est classée

1. Le VAD retire le silence. Une phrase de moins de `duree_min_parole` (1,0 s) de parole est trop courte pour une empreinte fiable :
   - **reprise** : si une personne a été identifiée (ou enrôlée) il y a moins de `reprise_duree_s` (120 s), la phrase lui est attribuée avec le résultat `reprise`. Le capteur garde donc le prénom sur « stop » ou « allume la lumière » au milieu d'un échange.
     - Aucune empreinte n'est calculée, et la phrase ne devient jamais un échantillon : ni automatique, ni d'enrôlement, ni par « Corriger… », qui ne change que l'étiquette.
     - La mémoire n'est pas rafraîchie par une reprise : une suite de phrases courtes ne prolonge pas le délai.
     - Elle est gardée par satellite quand HA indique le satellite dans l'événement `transcribe` (pas de reprise d'un satellite à l'autre), et sinon globalement. Le journal affiche une fois `satellite context: present/absent` à la première phrase reçue. Les versions actuelles de HA n'envoient pas ce contexte : c'est alors l'entrée globale qui sert.
     - Le capteur porte `reprise: true` et `age_s`, l'âge en secondes de l'identification reprise. Les autres résultats portent `reprise: false`.
     - Se désactive avec `reprise_active` dans les Réglages.
   - sinon : `trop court`, sans regroupement.
2. On calcule la similarité cosinus avec les échantillons de chaque personne : le maximum, ou la moyenne des 3 meilleurs.
3. La personne est retenue si son score atteint `seuil_identification` (0,55) **et** dépasse la deuxième personne d'au moins `marge_min` (0,08). Sinon, la phrase est `inconnu` :
   - elle est comparée à la liste à ignorer (`seuil_ignore`) ;
   - sinon, elle rejoint le groupe le plus proche (`seuil_cluster`, 0,6), ou en crée un nouveau.
4. Si le score dépasse `seuil_auto` (0,75) et la marge 0,15, la phrase devient un échantillon `auto`. Au-delà de `max_echantillons` (200), on retire les échantillons les plus redondants, les automatiques en premier.

## Évaluer les modèles

```bash
# sur les échantillons enrôlés (audio conservé dans /data)
docker exec -it whospeaks whospeaks-evaluate
# sur un corpus : voix/<personne>/*.wav
docker exec -it whospeaks whospeaks-evaluate --dossier /data/voix --modeles campplus-zh-en titanet-small
```

Pour chaque modèle, le script affiche :
- l'EER et le top-1 ;
- les résultats au seuil choisi (bonnes identifications, erreurs, inconnus) ;
- les seuils proposés ;
- la matrice de confusion.

## Développement

```bash
uv venv && uv pip install -e '.[test]'
pytest
```

Les tests de [tests/test_proxy.py](tests/test_proxy.py) utilisent un faux Whisper (serveur Wyoming) et un faux HA. Ils vérifient :
- la transparence : texte identique sur 20 phrases ;
- l'ordre : HA est prévenu avant le texte ;
- la robustesse : identification qui plante ou trop lente, HA injoignable, modèle absent, amont coupé.

Un test supplémentaire porte sur de vraies voix et le vrai modèle CAM++. Il s'exécute si `.cache/models` et `.cache/sr-data` sont présents :

```bash
mkdir -p .cache/models && cd .cache
git clone --depth 1 https://github.com/csukuangfj/sr-data
B=https://github.com/k2-fsa/sherpa-onnx/releases/download
curl -L -o models/silero_vad.onnx $B/asr-models/silero_vad.onnx
curl -L -o models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx \
  $B/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
```

## Données

`./data` contient :
- `whospeaks.db` (SQLite) ;
- `clips/AAAA/MM/JJ/<id>.wav` : extraits des phrases ;
- `clips/echantillons/…` : fichiers envoyés pour l'enrôlement ;
- `models/`.

L'audio est supprimé après `retention_jours` (30). Les empreintes sont gardées sans limite. Avec `garder_audio=false`, rien n'est enregistré en audio.

Une base corrompue est mise de côté (`whospeaks.db.corrompue-…`) et remplacée par une base vide. Aucune donnée ne quitte le réseau local, à part le téléchargement initial des modèles.
