"""Catalogue des modèles ONNX (sherpa-onnx) et téléchargement au premier démarrage."""

from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
# Le nom de la release contient bien la faute « recongition ».
_SPEAKER = f"{_RELEASES}/speaker-recongition-models"


@dataclass(frozen=True)
class ModelInfo:
    key: str
    filename: str
    url: str
    description: str


SPEAKER_MODELS: dict[str, ModelInfo] = {
    m.key: m
    for m in [
        ModelInfo(
            "campplus-zh-en",
            "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
            f"{_SPEAKER}/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
            "3D-Speaker CAM++ (zh/en, 192 dim) — rapide, bon défaut",
        ),
        ModelInfo(
            "campplus-voxceleb",
            "wespeaker_en_voxceleb_CAM++.onnx",
            f"{_SPEAKER}/wespeaker_en_voxceleb_CAM++.onnx",
            "WeSpeaker CAM++ VoxCeleb (512 dim)",
        ),
        ModelInfo(
            "eres2net-base",
            "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
            f"{_SPEAKER}/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
            "3D-Speaker ERes2Net base (512 dim) — plus lent",
        ),
        ModelInfo(
            "eres2netv2",
            "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx",
            f"{_SPEAKER}/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx",
            "3D-Speaker ERes2NetV2 (192 dim)",
        ),
        ModelInfo(
            "resnet34",
            "wespeaker_en_voxceleb_resnet34.onnx",
            f"{_SPEAKER}/wespeaker_en_voxceleb_resnet34.onnx",
            "WeSpeaker ResNet34 VoxCeleb (256 dim)",
        ),
        ModelInfo(
            "resnet34-lm",
            "wespeaker_en_voxceleb_resnet34_LM.onnx",
            f"{_SPEAKER}/wespeaker_en_voxceleb_resnet34_LM.onnx",
            "WeSpeaker ResNet34-LM VoxCeleb (256 dim)",
        ),
        ModelInfo(
            "titanet-small",
            "nemo_en_titanet_small.onnx",
            f"{_SPEAKER}/nemo_en_titanet_small.onnx",
            "NeMo TitaNet small (192 dim)",
        ),
    ]
}

VAD_MODEL = ModelInfo(
    "silero-vad",
    "silero_vad.onnx",
    f"{_RELEASES}/asr-models/silero_vad.onnx",
    "Silero VAD",
)


def resolve_speaker_model(name: str) -> ModelInfo:
    """Accepte une clé du catalogue, un nom de fichier .onnx ou une URL."""
    if name in SPEAKER_MODELS:
        return SPEAKER_MODELS[name]
    for info in SPEAKER_MODELS.values():
        if name in (info.filename, info.filename.removesuffix(".onnx")):
            return info
    if name.startswith(("http://", "https://")):
        filename = name.rsplit("/", 1)[-1]
        return ModelInfo(filename.removesuffix(".onnx"), filename, name, "Modèle personnalisé")
    if name.endswith(".onnx"):
        return ModelInfo(name.removesuffix(".onnx"), name, "", "Modèle local")
    raise ValueError(f"Modèle inconnu : {name}")


def ensure_model(info: ModelInfo, models_dir: Path, timeout: float = 120.0) -> Path:
    """Renvoie le chemin local du modèle, en le téléchargeant si besoin."""
    models_dir.mkdir(parents=True, exist_ok=True)
    path = models_dir / info.filename
    if path.exists() and path.stat().st_size > 0:
        return path
    if not info.url:
        raise FileNotFoundError(f"Modèle introuvable : {path}")
    _LOGGER.info("Téléchargement du modèle %s depuis %s", info.key, info.url)
    tmp = path.with_suffix(".part")
    with urllib.request.urlopen(info.url, timeout=timeout) as resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    tmp.rename(path)
    _LOGGER.info("Modèle %s enregistré (%.1f Mo)", info.key, path.stat().st_size / 1e6)
    return path
