"""Proxy STT Wyoming : relaie l'audio vers Whisper et identifie le locuteur en parallèle."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler

from . import __version__
from .filters import is_hallucination
from .ha import SATELLITE_TIMEOUT, HomeAssistant
from .service import Analysis, WhoSpeaks

_LOGGER = logging.getLogger(__name__)

_ATTRIBUTION = Attribution(name="WhoSpeaks", url="https://github.com/powange/whospeaks")


class Proxy:
    """État partagé entre les connexions."""

    def __init__(
        self,
        upstream_uri: str,
        service: WhoSpeaks | None,
        ha: HomeAssistant | None,
        lang: str = "fr",
        id_extra_wait: float = 0.15,
        connect_timeout: float = 5.0,
    ) -> None:
        self.upstream_uri = upstream_uri
        self.service = service
        self.ha = ha
        self.lang = lang
        self.id_extra_wait = id_extra_wait
        self.connect_timeout = connect_timeout
        self.analyze_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whospeaks-id")
        self.record_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whospeaks-db")
        self._info: Info | None = None
        self._info_time = 0.0
        self._context_logged = False
        self._last_source: str | None = None
        # Enregistrements en cours : référence gardée jusqu'à la fin (la connexion peut être fermée)
        self._background: set[asyncio.Task] = set()

    def keep(self, task: asyncio.Task) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def log_satellite_source(self, source: str, satellite: str | None) -> None:
        """Indique d'où vient le satellite (context, ha ou inconnu), à chaque changement."""
        if source == self._last_source:
            return
        self._last_source = source
        _LOGGER.info("satellite source: %s%s", source, f" ({satellite})" if satellite else "")

    def log_context(self, context: dict) -> None:
        """Indique une seule fois si HA envoie un contexte (satellite) dans `transcribe`.

        On ne peut le savoir qu'à la première phrase reçue, pas au démarrage.
        """
        if self._context_logged:
            return
        self._context_logged = True
        _LOGGER.info("satellite context: %s%s", "present" if context else "absent",
                     f" ({sorted(context)})" if context else " (reprise sur l'entrée globale)")

    async def get_info(self) -> Info:
        if self._info is not None and time.monotonic() - self._info_time < 60:
            return self._info
        upstream: Info | None = None
        try:
            async with AsyncClient.from_uri(self.upstream_uri, connect_timeout=2.0, read_timeout=2.0) as client:
                await client.write_event(Describe().event())
                while True:
                    event = await client.read_event()
                    if event is None:
                        break
                    if Info.is_type(event.type):
                        upstream = Info.from_event(event)
                        break
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Infos de l'amont indisponibles : %s", err)
        info = self._build_info(upstream)
        if upstream is not None:
            self._info, self._info_time = info, time.monotonic()
        return self._info or info

    def _build_info(self, upstream: Info | None) -> Info:
        models: list[AsrModel] = []
        streaming = False
        description = "WhoSpeaks (reconnaissance du locuteur)"
        version = __version__
        if upstream is not None and upstream.asr:
            prog = upstream.asr[0]
            streaming = prog.supports_transcript_streaming
            description = f"WhoSpeaks → {prog.description or prog.name}"
            for program in upstream.asr:
                for m in program.models:
                    models.append(AsrModel(
                        name=m.name, attribution=m.attribution, installed=m.installed,
                        description=m.description, version=m.version, languages=list(m.languages),
                    ))
        if not models:
            models = [AsrModel(name="whospeaks", attribution=_ATTRIBUTION, installed=True,
                               description="Whisper via WhoSpeaks", version=version,
                               languages=[self.lang])]
        return Info(asr=[AsrProgram(
            name="whospeaks", attribution=_ATTRIBUTION, installed=True, description=description,
            version=version, models=models, supports_transcript_streaming=streaming,
        )])


class ProxyHandler(AsyncEventHandler):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, proxy: Proxy) -> None:
        super().__init__(reader, writer)
        self.proxy = proxy
        self.upstream: AsyncClient | None = None
        self.upstream_task: asyncio.Task | None = None
        self.converter = AudioChunkConverter(rate=16000, width=2, channels=1)
        self.audio = bytearray()
        # Satellite de la phrase : pièce (ou nom) affichée, clé (entité HA ou identifiant du
        # contexte) et origine : « context », « ha » ou « inconnu » (None : pas encore connue)
        self.satellite: str | None = None
        self.satellite_entity: str | None = None
        self.satellite_source: str | None = None
        self.satellite_task: asyncio.Task | None = None
        self.analysis_future: asyncio.Future | None = None
        self.silence_task: asyncio.Task | None = None
        # Un transcript a déjà été renvoyé pour cette phrase : la suite de l'amont est ignorée.
        self.replied = False
        self.t_stop = 0.0

    # -------------------------------------------------------------- client -> amont
    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event((await self.proxy.get_info()).event())
            return True

        if Transcribe.is_type(event.type):
            context = event.data.get("context") or {}
            self.proxy.log_context(context)
            from_context = context.get("satellite_id") or context.get("device_id")
            if from_context:
                # Le contexte de HA reste prioritaire s'il est fourni
                self._set_satellite(str(from_context), str(from_context), "context")

        if AudioStart.is_type(event.type):
            self.audio.clear()
            self.analysis_future = None
            self.replied = False
            self._ask_satellite()

        if self.upstream is None and not await self._connect_upstream():
            await self.write_event(Error(text="Serveur STT amont injoignable", code="upstream").event())
            return False

        # L'événement est relayé tel quel, sans réencodage.
        await self.upstream.write_event(event)

        if AudioChunk.is_type(event.type):
            try:
                chunk = self.converter.convert(AudioChunk.from_event(event))
                self.audio.extend(chunk.audio)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Conversion audio impossible")
        elif AudioStop.is_type(event.type):
            self.t_stop = time.perf_counter()
            self._start_analysis()
        return True

    async def _connect_upstream(self) -> bool:
        try:
            client = AsyncClient.from_uri(self.proxy.upstream_uri, connect_timeout=self.proxy.connect_timeout)
            await client.connect()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Connexion à l'amont %s impossible : %s", self.proxy.upstream_uri, err)
            return False
        self.upstream = client
        self.upstream_task = asyncio.create_task(self._read_upstream(client), name="whospeaks upstream")
        return True

    # -------------------------------------------------------------- satellite
    def _set_satellite(self, label: str | None, entity: str | None, source: str) -> None:
        self.satellite, self.satellite_entity, self.satellite_source = label, entity, source
        self.proxy.log_satellite_source(source, label)

    def _ask_satellite(self) -> None:
        """Début de phrase : demande à HA quel satellite écoute, sans retarder l'audio."""
        if self.satellite_source == "context":
            return
        self.satellite = self.satellite_entity = self.satellite_source = None
        ha = self.proxy.ha
        self.satellite_task = asyncio.create_task(ha.active_satellite(), name="whospeaks satellite") \
            if ha is not None and ha.enabled else None

    async def _resolve_satellite(self, timeout: float) -> None:
        """Attend la réponse de HA au plus `timeout` secondes (0 : seulement si déjà là)."""
        if self.satellite_source is not None:
            return
        task = self.satellite_task
        if task is None:
            self._set_satellite(None, None, "inconnu")
            return
        try:
            found = await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            return  # pas encore de réponse : peut-être plus tard
        except Exception:  # noqa: BLE001
            found = None
        if found:
            self._set_satellite(found["piece"] or found["nom"] or found["entity_id"], found["entity_id"], "ha")
        else:
            self._set_satellite(None, None, "inconnu")

    def _start_analysis(self) -> None:
        service = self.proxy.service
        if service is None or not self.audio:
            return
        self.analysis_future = asyncio.ensure_future(self._analyze(service, bytes(self.audio)))
        if service.settings["filtre_silence_actif"]:
            self.silence_task = asyncio.create_task(self._reply_if_silence(self.analysis_future),
                                                    name="whospeaks silence")

    async def _analyze(self, service: WhoSpeaks, pcm: bytes) -> Analysis:
        # Clé de la reprise : le satellite s'il est déjà connu. En pratique HA a répondu
        # (demandé au début de la phrase) ; sinon, pas d'attente : HA lent ou coupé ne doit
        # pas retarder l'identification, et la reprise passe par l'entrée globale.
        await self._resolve_satellite(0)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.proxy.analyze_pool, service.analyze, pcm,
                                          self.satellite_entity)

    async def _reply_if_silence(self, future: asyncio.Future) -> None:
        """Pas de parole : texte vide tout de suite, sans attendre Whisper.

        HA termine alors le run sur « stt-no-text-recognized », sans conversation
        ni réponse vocale. Le texte de Whisper, s'il arrive, est ignoré.
        """
        try:
            analysis: Analysis = await asyncio.shield(future)
        except Exception:  # noqa: BLE001
            return  # l'erreur est traitée à l'arrivée du transcript
        if analysis.resultat != "ignoree_silence" or self.replied or future is not self.analysis_future:
            return
        self.analysis_future = None
        # Enregistré avant la réponse : HA coupe la connexion dès qu'il la reçoit.
        self._schedule_record(self.proxy.service, future, None, None, "silence")
        await self._write_empty("silence")
        _LOGGER.info("Phrase ignorée : pas de parole (%.2fs) | réponse vide envoyée %.0f ms après la fin",
                     analysis.duree_parole, (time.perf_counter() - self.t_stop) * 1000)

    async def _write_empty(self, reason: str) -> None:
        self.replied = True
        event = Transcript(text="").event()
        event.data["ignored"] = reason
        await self.write_event(event)

    # -------------------------------------------------------------- amont -> client
    async def _read_upstream(self, client: AsyncClient) -> None:
        try:
            while True:
                event = await client.read_event()
                if event is None:
                    break
                if self.replied:
                    continue  # réponse déjà faite (silence) : rien de l'amont ne doit passer
                if Transcript.is_type(event.type):
                    await self._send_transcript(event)
                else:
                    await self.write_event(event)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Erreur de relais depuis l'amont")
        finally:
            if self.upstream is client:
                self.upstream = None
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _send_transcript(self, event: Event) -> None:
        t_up = time.perf_counter()
        text = event.data.get("text")
        analysis: Analysis | None = None
        future = self.analysis_future
        self.analysis_future = None
        try:
            if future is not None:
                analysis = await asyncio.wait_for(asyncio.shield(future), timeout=self.proxy.id_extra_wait)
        except asyncio.TimeoutError:
            _LOGGER.warning("Identification trop lente, transcript envoyé sans locuteur")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Identification en échec")
            future = None

        if self.replied:
            return  # le silence a répondu pendant l'attente

        service = self.proxy.service
        filtered = self._filter_reason(analysis, text)
        if filtered:
            # Texte vide : HA s'arrête sur « stt-no-text-recognized ». Pas de capteur publié.
            if service is not None and future is not None:
                self._schedule_record(service, future, text, (time.perf_counter() - t_up) * 1000,
                                      filtered)
            await self._write_empty(filtered)
            _LOGGER.info("Phrase ignorée (%s) : « %s » | ajouté %.1f ms", filtered, text,
                         (time.perf_counter() - t_up) * 1000)
            return

        if service is not None and self.proxy.ha is not None:
            try:
                await self._resolve_satellite(0)  # sans attendre : la réponse ne doit pas tarder
                payload = service.ha_payload(analysis or Analysis("erreur"), text, self.satellite,
                                             self.satellite_entity)
                await self.proxy.ha.publish(payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Notification HA en échec")

        out = event
        if analysis is not None:
            # Le texte reste celui de Whisper ; on ajoute seulement des champs.
            out = Event(type=event.type, data={**event.data, "speaker": analysis.etat,
                                                "speaker_score": analysis.score}, payload=event.payload)
        self.replied = True
        await self.write_event(out)
        t_sent = time.perf_counter()
        added_ms = (t_sent - t_up) * 1000
        _LOGGER.info(
            "Phrase : %s (score=%s, marge=%s, parole=%.2fs) | empreinte %.0f ms | whisper %.0f ms | "
            "ajouté %.1f ms | « %s »",
            (analysis.etat + (" [reprise]" if analysis.resultat == "reprise" else "")) if analysis else "—",
            f"{analysis.score:.3f}" if analysis and analysis.score is not None else "—",
            f"{analysis.marge:.3f}" if analysis and analysis.marge is not None else "—",
            analysis.duree_parole if analysis else 0.0,
            analysis.temps_empreinte_ms if analysis else 0.0,
            (t_up - self.t_stop) * 1000 if self.t_stop else 0.0,
            added_ms, text,
        )
        if service is not None and future is not None:
            self._schedule_record(service, future, text, added_ms)

    def _filter_reason(self, analysis: Analysis | None, text: str | None) -> str | None:
        """« silence », « hallucination », « enrolement » ou None si le texte doit passer."""
        service = self.proxy.service
        if service is None:
            return None
        s = service.settings
        if analysis is not None and analysis.resultat == "ignoree_silence" and s["filtre_silence_actif"]:
            return "silence"
        # Analyse trop lente : on se fie à l'état courant de l'enrôlement.
        pendant = analysis.pendant_enrolement if analysis is not None else service.enrolement is not None
        if pendant and s["enrolement_silencieux"]:
            return "enrolement"
        if not s["filtre_silence_actif"]:
            return None
        if is_hallucination(text, service.settings["hallucinations"]):
            return "hallucination"
        return None

    def _schedule_record(self, service: WhoSpeaks, future: asyncio.Future, text: str | None,
                         added_ms: float | None, filtered: str | None = None) -> None:
        self.proxy.keep(asyncio.create_task(self._record(service, future, text, added_ms, filtered),
                                            name="whospeaks record"))

    async def _record(self, service: WhoSpeaks, future: asyncio.Future, text: str | None,
                      added_ms: float | None, filtered: str | None) -> None:
        try:
            analysis: Analysis = await asyncio.shield(future)
        except Exception:  # noqa: BLE001
            return
        if filtered in ("silence", "hallucination"):
            analysis.resultat = f"ignoree_{filtered}"
        # « enrolement » : la phrase garde son résultat et compte pour l'enrôlement.
        elif analysis.resultat == "ignoree_silence":
            # Analyse arrivée trop tard : le texte est déjà parti vers HA.
            analysis.resultat = "trop_court"
        # Le transcript est déjà parti : on peut attendre HA plus longtemps pour le journal.
        await self._resolve_satellite(SATELLITE_TIMEOUT)
        await asyncio.get_running_loop().run_in_executor(
            self.proxy.record_pool, _safe_record, service, analysis, text, self.satellite, added_ms,
            self.satellite_entity,
        )

    async def disconnect(self) -> None:
        for task in (self.upstream_task, self.silence_task):
            if task is not None and not task.done():
                # Le client est parti : inutile d'attendre la suite.
                task.cancel()
        if self.upstream is not None:
            try:
                await self.upstream.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.upstream = None


def _safe_record(service: WhoSpeaks, analysis: Analysis, text: str | None, satellite: str | None,
                 added_ms: float | None, satellite_entity: str | None = None) -> None:
    try:
        service.record(analysis, text, satellite, added_ms, satellite_entity)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Enregistrement de la phrase impossible")
