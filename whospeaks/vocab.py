"""Vocabulaire de la maison pour Whisper : termes, prompt initial, mots mal compris."""

from __future__ import annotations

import difflib
import math
import re

from .filters import normalize_text

# Catégories, de la plus prioritaire à la moins prioritaire quand le prompt est trop long :
# mots déjà mal compris, mots ajoutés à la main, prénoms enrôlés, alias, noms d'entités,
# pièces et étages.
CATEGORIES = ("correction", "manuel", "personne", "alias", "entite", "piece")
CATEGORY_LABELS = {
    "correction": "mal compris",
    "manuel": "ajouté à la main",
    "personne": "personne",
    "alias": "alias",
    "entite": "appareil",
    "piece": "pièce / étage",
}
DEFAULT_MAX_TOKENS = 200

_ELISIONS = ("l", "d", "j", "m", "n", "s", "t", "c", "qu", "jusqu", "lorsqu", "puisqu")
_WORD = re.compile(r"[\w'’-]+", re.UNICODE)


def parse_ha(exposed: dict, registry: list, states: list, areas: list, floors: list) -> dict:
    """Réponses de l'API WebSocket de HA -> {entities, areas, floors}.

    Seules les entités partagées avec Assist (`conversation: true`) sont gardées.
    """
    shared = {
        entity_id
        for entity_id, options in (exposed.get("exposed_entities") or {}).items()
        if isinstance(options, dict) and options.get("conversation")
    }
    names = {
        s.get("entity_id"): (s.get("attributes") or {}).get("friendly_name")
        for s in states or []
        if isinstance(s, dict)
    }
    reg = {e.get("entity_id"): e for e in registry or [] if isinstance(e, dict)}
    entities = []
    for entity_id in sorted(shared):
        entry = reg.get(entity_id, {})
        name = entry.get("name") or names.get(entity_id) or entry.get("original_name")
        entities.append({"entity_id": entity_id, "name": name or "",
                         "aliases": sorted(a for a in entry.get("aliases") or [] if a)})

    def named(items: list) -> list:
        return [{"name": i.get("name") or "", "aliases": sorted(a for a in i.get("aliases") or [] if a)}
                for i in items or [] if isinstance(i, dict)]

    return {"entities": entities, "areas": named(areas), "floors": named(floors)}


def build_terms(ha: dict | None, persons: list[str], manual: list[str],
                corrections: list[str]) -> list[tuple[str, str]]:
    """Termes uniques (sans tenir compte de la casse ni de la ponctuation), par priorité."""
    ha = ha or {}
    groups: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    groups["correction"] = list(corrections)
    groups["manuel"] = list(manual)
    groups["personne"] = list(persons)
    for item in ha.get("entities", []):
        groups["alias"].extend(item.get("aliases", []))
        groups["entite"].append(item.get("name", ""))
    for item in ha.get("areas", []) + ha.get("floors", []):
        groups["alias"].extend(item.get("aliases", []))
        groups["piece"].append(item.get("name", ""))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for category in CATEGORIES:
        for term in groups[category]:
            term = " ".join(str(term).split())
            key = normalize_text(term)
            if key and key not in seen:
                seen.add(key)
                out.append((term, category))
    return out


def estimate_tokens(text: str) -> int:
    """Nombre de tokens Whisper, estimé large : environ 3 caractères par token en français."""
    return math.ceil(len(text) / 3)


def build_prompt(terms: list[tuple[str, str]], max_tokens: int = DEFAULT_MAX_TOKENS) -> tuple[str, list[str]]:
    """Prompt initial : liste naturelle des termes, tronquée par priorité sous la limite.

    Renvoie (prompt, termes retenus). Une liste sans verbe ressemble moins à une commande :
    une vraie commande (« allume l'imprimante 3D ») n'est pas prise pour une recopie du prompt.
    """
    kept: list[str] = []
    for term, _ in terms:
        candidate = ", ".join([*kept, term]) + "."
        if estimate_tokens(candidate) > max_tokens:
            continue  # trop long : on tente les termes suivants, plus courts
        kept.append(term)
    return (", ".join(kept) + "." if kept else ""), kept


def is_prompt_copy(text: str | None, prompt: str, terms: list[str]) -> bool:
    """Whisper a-t-il recopié le prompt (tel quel ou presque) au lieu de transcrire ?

    Il faut un texte d'au moins 15 caractères, couvert à 90 % par des morceaux du prompt
    (de 4 caractères ou plus), et qui reprend au moins 3 termes du prompt.
    """
    t = normalize_text(text)
    p = normalize_text(prompt)
    if len(t) < 15 or not p:
        return False
    matcher = difflib.SequenceMatcher(None, t, p, autojunk=False)
    covered = sum(b.size for b in matcher.get_matching_blocks() if b.size >= 4)
    if covered < 0.9 * len(t):
        return False
    found = sum(1 for term in terms if (k := normalize_text(term)) and k in t)
    return found >= 3


def _strip_elision(word: str) -> str:
    parts = re.split(r"['’]", word, maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in _ELISIONS and parts[1]:
        return parts[1]
    return word


def _words(text: str) -> list[str]:
    return [_strip_elision(w).strip("-") for w in _WORD.findall(text or "")]


def corrected_words(whisper: str, corrected: str) -> list[tuple[str, str]]:
    """Mots qui diffèrent entre le texte de Whisper et le texte corrigé : (faux, vrai).

    « Allume l'impliment 3D » / « Allume l'imprimante 3D » -> [("impliment", "imprimante")].
    Seuls les mots remplacés comptent : un mot oublié ou en trop n'apprend rien sur la
    façon d'écrire un nom, et les petits mots (articles…) sont ignorés.
    """
    wrong, right = _words(whisper), _words(corrected)
    matcher = difflib.SequenceMatcher(None, [w.casefold() for w in wrong], [w.casefold() for w in right],
                                      autojunk=False)
    pairs: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        right_part = " ".join(right[j1:j2])
        if op == "replace" and len(normalize_text(right_part)) >= 3:
            pairs.append((" ".join(wrong[i1:i2]), right_part))
    return pairs
