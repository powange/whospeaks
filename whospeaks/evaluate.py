"""Évaluation des modèles d'empreinte sur les données de la maison.

Exemples :
    python -m whospeaks.evaluate                          # échantillons de /data, tous les modèles
    python -m whospeaks.evaluate --modeles campplus-zh-en resnet34
    python -m whospeaks.evaluate --dossier ./voix         # voix/<personne>/*.wav
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from . import calibration
from .audio import SAMPLE_RATE, read_wav
from .engine import Embedder, Vad
from .models import SPEAKER_MODELS, VAD_MODEL, ensure_model, resolve_speaker_model


def load_from_db(data_dir: Path) -> dict[str, list[Path]]:
    conn = sqlite3.connect(data_dir / "whospeaks.db")
    rows = conn.execute(
        "SELECT p.nom, e.clip FROM echantillons e JOIN personnes p ON p.id=e.personne_id "
        "WHERE e.clip IS NOT NULL"
    ).fetchall()
    conn.close()
    out: dict[str, list[Path]] = {}
    seen: set[str] = set()
    for nom, clip in rows:
        path = data_dir / clip
        if clip in seen or not path.exists():
            continue
        seen.add(clip)
        out.setdefault(nom, []).append(path)
    return out


def load_from_dir(folder: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
        files = sorted(f for f in sub.rglob("*") if f.suffix.lower() in (".wav", ".flac", ".mp3", ".ogg"))
        if files:
            out[sub.name] = files
    return out


def evaluate_model(key: str, corpus: dict[str, list[Path]], models_dir: Path, vad: Vad,
                   seuil: float, marge: float, agregation: str, threads: int) -> dict:
    info = resolve_speaker_model(key)
    embedder = Embedder(ensure_model(info, models_dir), info.key, threads)
    names = list(corpus)
    persons: dict[int, np.ndarray] = {}
    times: list[float] = []
    for idx, name in enumerate(names):
        embs = []
        for path in corpus[name]:
            speech = vad.speech(read_wav(path))
            if len(speech) < 0.5 * SAMPLE_RATE:
                continue
            t0 = time.perf_counter()
            embs.append(embedder.embed(speech))
            times.append((time.perf_counter() - t0) * 1000)
        if embs:
            persons[idx] = np.stack(embs)
    report = calibration.calibrate(persons, agregation)
    trials = calibration.leave_one_out(persons, agregation)
    at = calibration.apply_thresholds(trials, seuil, marge)
    conf = calibration.confusion(trials, seuil, marge)
    # Précision « fermée » (sans rejet) : meilleure personne = bonne personne.
    top1 = sum(t.best_id == t.true_id for t in trials) / len(trials) if trials else 0.0
    report.update(
        modele=info.key,
        dim=embedder.dim,
        temps_ms_median=round(float(np.median(times)), 1) if times else None,
        top1=round(top1, 4),
        au_seuil={"seuil": seuil, "marge": marge, "precision": round(at.accuracy, 4),
                  "erreurs": round(at.error_rate, 4),
                  "inconnus": round(at.unknown / at.total, 4) if at.total else 0.0},
        confusion={names[t]: {(names[int(k)] if k != "inconnu" else k): v for k, v in row.items()}
                   for t, row in conf.items()},
    )
    report.pop("hist_meme", None)
    report.pop("hist_diff", None)
    return report


def _print_report(r: dict) -> None:
    print(f"\n=== {r['modele']} ({r['dim']} dim, {r['temps_ms_median']} ms/empreinte) ===")
    print(f"  personnes={r['personnes']} échantillons={r['echantillons']} "
          f"paires même/diff={r['paires_meme']}/{r['paires_diff']}")
    if r["eer"] is not None:
        print(f"  EER={r['eer']:.2%} (seuil {r['seuil_eer']})   top-1={r['top1']:.2%}")
    a = r["au_seuil"]
    print(f"  seuil {a['seuil']} / marge {a['marge']} : bonnes={a['precision']:.2%} "
          f"erreurs={a['erreurs']:.2%} inconnus={a['inconnus']:.2%}")
    p = r.get("proposition")
    if p:
        print(f"  proposé : seuil {p['seuil_identification']} / marge {p['marge_min']} → "
              f"bonnes={p['precision']:.2%} erreurs={p['taux_erreur']:.2%} inconnus={p['taux_inconnu']:.2%}")
    print("  confusion (vrai → prédit) :")
    for true, row in r["confusion"].items():
        print(f"    {true:>15} : " + ", ".join(f"{k}={v}" for k, v in sorted(row.items())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare les modèles d'empreinte vocale (validation croisée).")
    parser.add_argument("--data", type=Path, default=Path("/data"), help="dossier de données WhoSpeaks")
    parser.add_argument("--dossier", type=Path, help="corpus externe : <dossier>/<personne>/*.wav")
    parser.add_argument("--modeles", nargs="*", default=list(SPEAKER_MODELS), help="clés des modèles")
    parser.add_argument("--seuil", type=float, default=0.55)
    parser.add_argument("--marge", type=float, default=0.08)
    parser.add_argument("--agregation", choices=["max", "top3"], default="max")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--json", type=Path, help="écrit le rapport complet en JSON")
    args = parser.parse_args(argv)

    corpus = load_from_dir(args.dossier) if args.dossier else load_from_db(args.data)
    if len(corpus) < 2:
        print("Il faut au moins deux personnes avec de l'audio.", file=sys.stderr)
        return 1
    print("Corpus : " + ", ".join(f"{n} ({len(f)})" for n, f in corpus.items()))
    models_dir = args.data / "models"
    try:
        vad = Vad(ensure_model(VAD_MODEL, models_dir))
    except Exception as err:  # noqa: BLE001
        print(f"VAD Silero indisponible ({err}), repli sur l'énergie", file=sys.stderr)
        vad = Vad(None)
    reports = []
    for key in args.modeles:
        try:
            report = evaluate_model(key, corpus, models_dir, vad, args.seuil, args.marge,
                                    args.agregation, args.threads)
        except Exception as err:  # noqa: BLE001
            print(f"\n=== {key} : échec ({err})", file=sys.stderr)
            continue
        _print_report(report)
        reports.append(report)
    if reports:
        best = min((r for r in reports if r["eer"] is not None), key=lambda r: r["eer"], default=None)
        if best:
            print(f"\nMeilleur EER : {best['modele']} ({best['eer']:.2%})")
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
