"""Product alias list (SPEC 6.9, AL-1, AL-2).

An alias is a lower-case, punctuation-free phrase that names an entity. Aliases come from the
entity label itself, the label without stop words, a singular or plural twin, and a short
hand-maintained list of equivalent phrases (``cd`` and ``share certificate``). An alias that
links to two or more entities is ambiguous and is never a unique match (AL-2).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

STOP_WORDS = frozenset({"a", "an", "the", "of", "for", "and", "to", "in", "with", "your", "our"})

# Groups of phrases that mean the same thing. If an entity contains one phrase of a group, the
# others are added as aliases with that phrase swapped in.
ABBREVIATIONS: tuple[tuple[str, ...], ...] = (
    ("cd", "share certificate", "certificate of deposit"),
    ("ira", "individual retirement account"),
    ("heloc", "home equity line of credit"),
    ("apy", "annual percentage yield"),
    ("apr", "annual percentage rate"),
)

_APOSTROPHES = re.compile(r"['’`]")
_NOT_ALNUM = re.compile(r"[\W_]+")
_PLURAL_MIN_LEN = 4


def canonical_id(label: str) -> str:
    """Lower-case, punctuation-stripped label (the canonical ``entity`` id)."""
    text = _APOSTROPHES.sub("", unicodedata.normalize("NFKC", label).lower())
    return _NOT_ALNUM.sub(" ", text).strip()


def _without_stop_words(entity: str) -> str:
    return " ".join(word for word in entity.split() if word not in STOP_WORDS)


def _plural_variants(phrase: str) -> set[str]:
    """The singular or plural twins of ``phrase`` (only its last word changes).

    A phrase that ends in ``s`` may be a plural (``branches``, ``lobbies``, ``mortgages``) or a
    singular (``class``, ``savings``), so every plausible twin is returned; one that matches no
    entity is harmless. A singular gets only its regular plural, so ``branchs`` is never made.
    """
    if not phrase:
        return set()
    twins: set[str] = set()
    if phrase.endswith("ss"):
        twins.add(phrase + "es")
    elif phrase.endswith("s") and len(phrase) >= _PLURAL_MIN_LEN:
        twins.add(phrase[:-1])
        if phrase.endswith(("ses", "xes", "zes", "ches", "shes")):
            twins.add(phrase[:-2])
        if phrase.endswith("ies") and len(phrase) > _PLURAL_MIN_LEN:
            twins.add(phrase[:-3] + "y")
    elif phrase.endswith(("x", "z", "ch", "sh")):
        twins.add(phrase + "es")
    elif phrase.endswith("y") and len(phrase) > 1 and phrase[-2] not in "aeiou ":
        twins.add(phrase[:-1] + "ies")
    else:
        twins.add(phrase + "s")
    return twins


def _swap_abbreviations(phrase: str) -> set[str]:
    variants: set[str] = set()
    padded = f" {phrase} "
    for group in ABBREVIATIONS:
        for found in group:
            if f" {found} " not in padded:
                continue
            for other in group:
                if other != found:
                    variants.add(padded.replace(f" {found} ", f" {other} ").strip())
    return variants


def _aliases_of(entity: str) -> set[str]:
    bases = {entity}
    stripped = _without_stop_words(entity)
    if stripped:
        bases.add(stripped)
    found = set(bases)
    for base in bases:
        found |= _plural_variants(base)
        found |= _swap_abbreviations(base)
    return {alias for alias in found if alias}


@dataclass(frozen=True)
class AliasIndex:
    """Alias to entity ids, and the set of entity ids themselves."""

    aliases: dict[str, frozenset[str]]
    entities: frozenset[str] = frozenset()

    @property
    def ambiguous(self) -> frozenset[str]:
        """Aliases that link to two or more entities."""
        return frozenset(alias for alias, ids in self.aliases.items() if len(ids) > 1)

    def resolve(self, text: str) -> str | None:
        """The one entity id ``text`` names, or ``None`` if unknown or ambiguous.

        The canonical id of an entity always names that entity, even when another entity
        shares the same text as an alias; every other alias must be unique.
        """
        phrase = canonical_id(text)
        if not phrase:
            return None
        if phrase in self.entities:
            return phrase
        for candidate in (phrase, _without_stop_words(phrase)):
            ids = self.aliases.get(candidate)
            if ids is not None and len(ids) == 1:
                return next(iter(ids))
        return None


def build_aliases(labels: Iterable[str]) -> AliasIndex:
    """Build the alias index from entity labels and page titles (AL-1)."""
    linked: dict[str, set[str]] = {}
    entities: set[str] = set()
    for label in labels:
        entity = canonical_id(label)
        if not entity:
            continue
        entities.add(entity)
        for alias in _aliases_of(entity):
            linked.setdefault(alias, set()).add(entity)
    return AliasIndex({a: frozenset(ids) for a, ids in sorted(linked.items())}, frozenset(entities))
