"""API REST et page web d'étiquetage (port 8095)."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import __version__
from .audio import decode_file
from .config import DEFAULT_SETTINGS, Config, IPNetwork
from .models import SPEAKER_MODELS
from .service import ServiceError, WhoSpeaks

STATIC_DIR = Path(__file__).parent / "static"
_COOKIE = "whospeaks_token"


class PersonIn(BaseModel):
    nom: str
    person_entity: str | None = None


class PersonPatch(BaseModel):
    nom: str | None = None
    person_entity: str | None = None


class AssignIn(BaseModel):
    personne_id: int | None = None
    nouveau_nom: str | None = None
    person_entity: str | None = None


class MergeIn(BaseModel):
    source: int
    cible: int


class RenameIn(BaseModel):
    nom: str | None = None


class EnrolIn(BaseModel):
    personne_id: int | None = None
    personne: str | None = None  # nom, pratique depuis un rest_command HA
    n: int = 12
    duree_max: float = 900


def create_app(service: WhoSpeaks, config: Config) -> FastAPI:
    app = FastAPI(title="WhoSpeaks", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")

    frame_origins = " ".join(["'self'", *([config.ha_url] if config.ha_url else []), *config.frame_ancestors])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        peer = request.client.host if request.client else None
        trusted = _in_networks(peer, config.trusted_proxies)
        client = client_ip(peer, request.headers, config.trusted_proxies)
        if config.ui_local_only and not _is_local(client):
            return JSONResponse({"detail": "Accès réservé au réseau local"}, status_code=403)
        response = await call_next(request)
        # Autorise l'affichage dans une carte iframe de Home Assistant.
        response.headers["Content-Security-Policy"] = f"frame-ancestors {frame_origins}"
        if _scheme(request, trusted) == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        token = request.query_params.get("token")
        if token and config.ui_token and hmac.compare_digest(token, config.ui_token):
            # Le cookie évite de répéter le jeton dans chaque URL (lecteurs audio…).
            # SameSite=None est nécessaire dans l'iframe de HA, mais impose HTTPS :
            # en HTTP, le jeton reste dans l'URL et la page le recopie dans ses appels.
            response.set_cookie(_COOKIE, token, httponly=True, samesite="lax", max_age=365 * 86400)
        return response

    def auth(request: Request) -> None:
        if not config.ui_token:
            return
        header = request.headers.get("authorization", "")
        candidates = [
            header[7:] if header.lower().startswith("bearer ") else None,
            request.headers.get("x-token"),
            request.query_params.get("token"),
            request.cookies.get(_COOKIE),
        ]
        if not any(c and hmac.compare_digest(c, config.ui_token) for c in candidates):
            raise HTTPException(401, "Jeton invalide ou absent")

    api = Depends(auth)

    async def run(func, *args) -> Any:
        try:
            return await asyncio.get_running_loop().run_in_executor(None, func, *args)
        except ServiceError as err:
            raise HTTPException(400, str(err)) from err

    def resolve_person(body: AssignIn) -> int:
        if body.personne_id is not None:
            return body.personne_id
        if body.nouveau_nom:
            return service.create_person(body.nouveau_nom, body.person_entity)
        raise HTTPException(400, "personne_id ou nouveau_nom requis")

    # ---------------------------------------------------------------- état
    @app.get("/api/etat", dependencies=[api])
    async def etat():
        return service.status() | {"version": __version__, "ha_url": config.ha_url}

    @app.get("/api/modeles", dependencies=[api])
    async def modeles():
        return [{"cle": m.key, "description": m.description} for m in SPEAKER_MODELS.values()]

    # -------------------------------------------------------------- journal
    @app.get("/api/phrases", dependencies=[api])
    async def phrases(limit: int = 100, offset: int = 0, filtre: str | None = None):
        return await run(service.list_phrases, min(limit, 500), offset, filtre)

    @app.get("/api/phrases/{phrase_id}/audio", dependencies=[api])
    async def phrase_audio(phrase_id: int):
        try:
            return FileResponse(service.phrase_clip(phrase_id), media_type="audio/wav")
        except ServiceError as err:
            raise HTTPException(404, str(err)) from err

    @app.post("/api/phrases/{phrase_id}/attribuer", dependencies=[api])
    async def phrase_attribuer(phrase_id: int, body: AssignIn):
        pid = await run(resolve_person, body)
        await run(service.assign_phrase, phrase_id, pid)
        return {"ok": True, "personne_id": pid}

    @app.post("/api/phrases/{phrase_id}/desattribuer", dependencies=[api])
    async def phrase_desattribuer(phrase_id: int):
        await run(service.unassign_phrase, phrase_id)
        return {"ok": True}

    @app.post("/api/phrases/{phrase_id}/ignorer", dependencies=[api])
    async def phrase_ignorer(phrase_id: int):
        await run(service.ignore_phrase, phrase_id)
        return {"ok": True}

    @app.delete("/api/phrases/{phrase_id}", dependencies=[api])
    async def phrase_supprimer(phrase_id: int):
        await run(service.delete_phrase, phrase_id)
        return {"ok": True}

    @app.post("/api/derniere/attribuer", dependencies=[api])
    async def derniere_attribuer(body: AssignIn):
        """Attribue la dernière phrase reçue (pratique depuis un script HA)."""
        rows = service.list_phrases(1)
        if not rows:
            raise HTTPException(404, "Aucune phrase")
        pid = await run(resolve_person, body)
        await run(service.assign_phrase, rows[0]["id"], pid)
        return {"ok": True, "phrase_id": rows[0]["id"], "personne_id": pid}

    # ------------------------------------------------------------ personnes
    @app.get("/api/personnes", dependencies=[api])
    async def personnes():
        return await run(service.list_persons)

    @app.post("/api/personnes", dependencies=[api])
    async def personne_creer(body: PersonIn):
        return {"id": await run(service.create_person, body.nom, body.person_entity)}

    @app.patch("/api/personnes/{person_id}", dependencies=[api])
    async def personne_modifier(person_id: int, body: PersonPatch):
        await run(service.update_person, person_id, body.nom, body.person_entity)
        return {"ok": True}

    @app.delete("/api/personnes/{person_id}", dependencies=[api])
    async def personne_supprimer(person_id: int):
        await run(service.delete_person, person_id)
        return {"ok": True}

    @app.get("/api/personnes/{person_id}/echantillons", dependencies=[api])
    async def echantillons(person_id: int):
        return await run(service.list_samples, person_id)

    @app.post("/api/personnes/{person_id}/fichiers", dependencies=[api])
    async def personne_fichiers(person_id: int, fichiers: list[UploadFile] = File(...),
                                source: str = "fichier"):
        if source not in ("fichier", "navigateur"):
            source = "fichier"
        total, erreurs = 0, []
        for f in fichiers:
            data = await f.read()
            try:
                audio = await run(decode_file, data)
            except HTTPException:
                raise
            except Exception as err:  # noqa: BLE001
                erreurs.append(f"{f.filename} : format non lu ({err})")
                continue
            try:
                total += await run(service.add_audio_samples, person_id, audio, source)
            except HTTPException as err:
                erreurs.append(f"{f.filename} : {err.detail}")
        return {"ajoutes": total, "erreurs": erreurs}

    @app.get("/api/echantillons/{sample_id}/audio", dependencies=[api])
    async def echantillon_audio(sample_id: int):
        try:
            return FileResponse(service.sample_clip(sample_id), media_type="audio/wav")
        except ServiceError as err:
            raise HTTPException(404, str(err)) from err

    @app.delete("/api/echantillons/{sample_id}", dependencies=[api])
    async def echantillon_supprimer(sample_id: int):
        await run(service.delete_sample, sample_id)
        return {"ok": True}

    # ----------------------------------------------------------- enrôlement
    @app.post("/api/enrolement", dependencies=[api])
    async def enrolement(body: EnrolIn):
        if body.personne_id is not None:
            pid = body.personne_id
        elif body.personne:
            try:
                pid = service.find_person(body.personne)
            except ServiceError:
                pid = await run(service.create_person, body.personne, None)
        else:
            raise HTTPException(400, "personne_id ou personne requis")
        enrol = await run(service.start_enrolement, pid, body.n, body.duree_max)
        return {"personne": enrol.nom, "total": enrol.total}

    @app.delete("/api/enrolement", dependencies=[api])
    async def enrolement_stop():
        service.stop_enrolement()
        return {"ok": True}

    # --------------------------------------------------------------- groupes
    @app.get("/api/groupes", dependencies=[api])
    async def groupes():
        return await run(service.list_clusters)

    @app.post("/api/groupes/{cluster_id}/attribuer", dependencies=[api])
    async def groupe_attribuer(cluster_id: int, body: AssignIn):
        pid = await run(resolve_person, body)
        n = await run(service.assign_cluster, cluster_id, pid)
        return {"ok": True, "personne_id": pid, "phrases": n}

    @app.patch("/api/groupes/{cluster_id}", dependencies=[api])
    async def groupe_renommer(cluster_id: int, body: RenameIn):
        await run(service.rename_cluster, cluster_id, body.nom)
        return {"ok": True}

    @app.post("/api/groupes/fusionner", dependencies=[api])
    async def groupe_fusionner(body: MergeIn):
        await run(service.merge_clusters, body.source, body.cible)
        return {"ok": True}

    @app.post("/api/groupes/{cluster_id}/ignorer", dependencies=[api])
    async def groupe_ignorer(cluster_id: int):
        await run(service.ignore_cluster, cluster_id)
        return {"ok": True}

    @app.delete("/api/groupes/{cluster_id}", dependencies=[api])
    async def groupe_supprimer(cluster_id: int):
        await run(service.delete_cluster, cluster_id)
        return {"ok": True}

    @app.post("/api/groupes/recalculer", dependencies=[api])
    async def groupes_recalculer():
        if not service.run_task("regroupement", service.recluster):
            raise HTTPException(409, "Une tâche est déjà en cours")
        return {"ok": True}

    @app.get("/api/ignores", dependencies=[api])
    async def ignores():
        return await run(service.list_ignores)

    @app.delete("/api/ignores/{ignore_id}", dependencies=[api])
    async def ignore_supprimer(ignore_id: int):
        await run(service.delete_ignore, ignore_id)
        return {"ok": True}

    # -------------------------------------------------------------- réglages
    @app.get("/api/reglages", dependencies=[api])
    async def reglages():
        return {"valeurs": service.settings, "defauts": DEFAULT_SETTINGS, "modele_env": config.model}

    @app.put("/api/reglages", dependencies=[api])
    async def reglages_modifier(values: dict[str, Any]):
        unknown = [k for k in values if k not in DEFAULT_SETTINGS]
        if unknown:
            raise HTTPException(400, f"Réglages inconnus : {', '.join(unknown)}")
        return await run(service.update_settings, values)

    @app.post("/api/calibrage", dependencies=[api])
    async def calibrage(max_erreur: float = 0.02):
        return await run(service.calibrate, max_erreur)

    @app.post("/api/empreintes/recalculer", dependencies=[api])
    async def empreintes_recalculer():
        if not service.run_task("recalcul_empreintes", service.reembed_all):
            raise HTTPException(409, "Une tâche est déjà en cours")
        return {"ok": True}

    @app.post("/api/retention", dependencies=[api])
    async def retention():
        return {"supprimes": await run(service.purge_clips)}

    @app.get("/api/export", dependencies=[api])
    async def export(audio: bool = True):
        tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
        await run(service.export_zip, tmp, audio)
        name = f"whospeaks-{time.strftime('%Y%m%d-%H%M')}.zip"
        return FileResponse(tmp, filename=name, media_type="application/zip",
                            background=BackgroundTask(tmp.unlink, missing_ok=True))

    @app.post("/api/import", dependencies=[api])
    async def import_(archive: UploadFile = File(...)):
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            while chunk := await archive.read(1 << 20):
                tmp.write(chunk)
        path = Path(tmp.name)
        try:
            return await run(service.import_zip, path)
        finally:
            path.unlink(missing_ok=True)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "modele": service.embedder.key if service.embedder else None}

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def client_ip(peer: str | None, headers: Mapping[str, str], trusted: list[IPNetwork]) -> str | None:
    """Adresse du client réel.

    Les en-têtes ne sont lus que si la connexion vient d'un proxy de confiance.
    X-Forwarded-For est parcouru de droite à gauche : chaque proxy ajoute à droite
    l'adresse qu'il voit, donc la première adresse non fiable est celle du client ;
    tout ce qui est plus à gauche a pu être inventé par lui.
    """
    if not _in_networks(peer, trusted):
        return peer
    forwarded = ",".join(headers.getlist("x-forwarded-for")) if hasattr(headers, "getlist") \
        else headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    if hops:
        for hop in reversed(hops):
            if not _in_networks(hop, trusted):
                return hop
        return hops[0]  # toute la chaîne est de confiance
    real_ip = (headers.get("x-real-ip") or "").strip()
    return real_ip or peer


def _scheme(request: Request, trusted: bool) -> str:
    if trusted:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto:
            return proto
    return request.url.scheme


def _parse_ip(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not host:
        return None
    host = host.strip()
    # Formes « [::1]:8080 » et « 1.2.3.4:8080 » parfois envoyées par les proxys.
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def _in_networks(host: str | None, networks: list[IPNetwork]) -> bool:
    addr = _parse_ip(host)
    return addr is not None and any(addr in net for net in networks)


def _is_local(host: str | None) -> bool:
    if host in ("testclient", "localhost"):
        return True
    addr = _parse_ip(host)
    if addr is None:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local
