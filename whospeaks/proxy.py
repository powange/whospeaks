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
from .ha import HomeAssistant
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
        self.satellite: str | None = None
        self.analysis_future: asyncio.Future | None = None
        self.t_stop = 0.0

    # -------------------------------------------------------------- client -> amont
    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event((await self.proxy.get_info()).event())
            return True

        if Transcribe.is_type(event.type):
            context = event.data.get("context") or {}
            self.proxy.log_context(context)
            self.satellite = context.get("satellite_id") or context.get("device_id") or self.satellite

        if AudioStart.is_type(event.type):
            self.audio.clear()
            self.analysis_future = None

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

    def _start_analysis(self) -> None:
        service = self.proxy.service
        if service is None or not self.audio:
            return
        pcm = bytes(self.audio)
        loop = asyncio.get_running_loop()
        self.analysis_future = loop.run_in_executor(self.proxy.analyze_pool, service.analyze, pcm,
                                                    self.satellite)

    # -------------------------------------------------------------- amont -> client
    async def _read_upstream(self, client: AsyncClient) -> None:
        try:
            while True:
                event = await client.read_event()
                if event is None:
                    break
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

        service = self.proxy.service
        if service is not None and self.proxy.ha is not None:
            try:
                payload = service.ha_payload(analysis or Analysis("erreur"), text)
                await self.proxy.ha.publish(payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Notification HA en échec")

        out = event
        if analysis is not None:
            # Le texte reste celui de Whisper ; on ajoute seulement des champs.
            out = Event(type=event.type, data={**event.data, "speaker": analysis.etat,
                                                "speaker_score": analysis.score}, payload=event.payload)
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

    def _schedule_record(self, service: WhoSpeaks, future: asyncio.Future, text: str | None,
                         added_ms: float) -> None:
        loop = asyncio.get_running_loop()
        satellite = self.satellite

        def done(fut: asyncio.Future) -> None:
            if fut.cancelled() or fut.exception() is not None:
                return
            analysis = fut.result()
            loop.run_in_executor(self.proxy.record_pool, _safe_record, service, analysis, text,
                                 satellite, added_ms)

        future.add_done_callback(done)

    async def disconnect(self) -> None:
        if self.upstream_task is not None and not self.upstream_task.done():
            # Le client est parti : inutile d'attendre la suite de l'amont.
            self.upstream_task.cancel()
        if self.upstream is not None:
            try:
                await self.upstream.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.upstream = None


def _safe_record(service: WhoSpeaks, analysis: Analysis, text: str | None, satellite: str | None,
                 added_ms: float) -> None:
    try:
        service.record(analysis, text, satellite, added_ms)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Enregistrement de la phrase impossible")
