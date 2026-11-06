"""
Semantic + lexical similarity between two field names.

Two layers, both deterministic and offline by default:

1. LEXICAL  - how alike the spellings are (difflib ratio / token overlap).
2. SEMANTIC - do the names *mean* the same thing, even when spelled
   differently?  Names are split into tokens (snake_case, camelCase, dotted
   paths), abbreviations are expanded (dob -> date + birth), and every token
   is mapped to a concept through a small registry/DPI-flavoured lexicon
   (citizen / owner / taxpayer / patient -> "party", id / number / code /
   reference -> "ident").  Two names are then compared as sets of concepts.

   "Qualifier families" stop look-alikes from matching: first_name vs
   last_name share the token "name" but carry opposing qualifiers
   (given vs family), valid_from vs valid_to likewise.

OPTIONAL: when MISTRAL_API_KEY is set, an embedding cosine (mistral-embed)
over the *humanised field names only* is blended in.  Only field names are
sent - never sample values - so the "no raw data before consent" promise of
the app still holds.  Set DPI_EMBEDDINGS=off to disable even with a key.
"""

from __future__ import annotations

import math
import os
import re
from difflib import SequenceMatcher
from typing import Callable, Optional

import requests

# --------------------------------------------------------------------------
# Tokenisation
# --------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^A-Za-z0-9]+")

FILLER = {"of", "the", "a", "an", "and", "full", "complete", "entire"}

# token -> list of tokens it expands to
ABBREVIATIONS: dict[str, list[str]] = {
    "dob": ["birth", "date"], "birthdate": ["birth", "date"], "birthday": ["birth", "date"],
    "born": ["birth"], "dt": ["date"], "no": ["number"], "num": ["number"], "nbr": ["number"],
    "addr": ["address"], "tel": ["phone"], "ph": ["phone"], "mob": ["mobile"],
    "qty": ["quantity"], "amt": ["amount"], "desc": ["description"], "dept": ["department"],
    "yr": ["year"], "reg": ["registration"], "veh": ["vehicle"], "emp": ["employee"],
    "org": ["organization"], "orgn": ["organization"],
    "zip": ["postal", "code"], "zipcode": ["postal", "code"], "postcode": ["postal", "code"],
    "pincode": ["postal", "code"], "fullname": ["name"], "firstname": ["first", "name"],
    "lastname": ["last", "name"], "middlename": ["middle", "name"],
    "tin": ["tax", "identifier"], "cid": ["citizen", "identifier"], "uid": ["identifier"],
    "uuid": ["identifier"], "guid": ["identifier"], "email": ["email"],
}

# concept -> words that mean the same thing for registry-style data
CONCEPTS: dict[str, set[str]] = {
    "ident": {"id", "identifier", "identification", "number", "code", "ref", "reference", "key"},
    "party": {"person", "citizen", "resident", "individual", "taxpayer", "patient", "owner",
              "holder", "applicant", "people"},
    "name": {"name", "label"},
    "given": {"first", "given", "forename"},
    "family": {"last", "family", "surname"},
    "middle": {"middle"},
    "birth": {"birth"},
    "date": {"date", "day"},
    "address": {"address", "residence", "domicile"},
    "phone": {"phone", "telephone", "mobile", "cell", "cellphone", "contact"},
    "email": {"email", "mail"},
    "gender": {"gender", "sex"},
    "postal": {"postal"},
    "amount": {"amount", "sum"},
    "quantity": {"quantity", "count"},
    "region": {"state", "province", "region"},
    "district": {"district", "county", "taluk", "tehsil", "mandal"},
    "city": {"city", "town", "municipality"},
    "country": {"country", "nation"},
    "company": {"company", "organization", "organisation", "firm", "business", "employer"},
    "income": {"salary", "wage", "income", "earnings"},
    "property": {"property", "parcel", "plot"},
    "description": {"description", "details", "remarks", "notes", "comment"},
    "start": {"start", "begin", "from", "issue", "issued", "effective"},
    "end": {"end", "expiry", "expire", "expires", "expiration", "until", "to"},
    "primary": {"primary", "main"},
    "secondary": {"secondary", "alternate", "alternative"},
    "permanent": {"permanent"},
    "temporary": {"temporary", "current"},
}

# Concepts from the same family are *opposites*: if both names carry one and
# they differ, the names describe different things (first vs last name...).
QUALIFIER_FAMILIES: list[set[str]] = [
    {"given", "family", "middle"},
    {"start", "end"},
    {"primary", "secondary"},
    {"permanent", "temporary"},
]


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


_WORD_TO_CONCEPT: dict[str, str] = {}
for _concept, _words in CONCEPTS.items():
    for _w in _words:
        _WORD_TO_CONCEPT[_stem(_w)] = _concept


def raw_tokens(name: str) -> list[str]:
    """Split a field name / dotted path into lowercase tokens."""
    spaced = _CAMEL.sub(" ", name)
    return [t.lower() for t in _SPLIT.split(spaced) if t]


def concept_tokens(name: str) -> list[str]:
    """Tokens after abbreviation expansion, filler removal, stemming and concept mapping."""
    out: list[str] = []
    for tok in raw_tokens(name):
        for piece in ABBREVIATIONS.get(tok, [tok]):
            if piece in FILLER:
                continue
            stemmed = _stem(piece)
            out.append(_WORD_TO_CONCEPT.get(stemmed, stemmed))
    return out


def humanise(name: str) -> str:
    return " ".join(raw_tokens(name))


# --------------------------------------------------------------------------
# Lexical + lexicon-semantic similarity
# --------------------------------------------------------------------------

def lexical_similarity(a: str, b: str) -> float:
    ta, tb = raw_tokens(a), raw_tokens(b)
    if not ta or not tb:
        return 0.0
    seq = SequenceMatcher(None, "".join(ta), "".join(tb)).ratio()
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    return max(seq, jac)


def lexicon_similarity(a: str, b: str) -> float:
    ca, cb = concept_tokens(a), concept_tokens(b)
    sa, sb = set(ca), set(cb)
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    jaccard = len(inter) / len(sa | sb)
    containment = len(inter) / min(len(sa), len(sb))
    score = 0.5 * jaccard + 0.5 * containment

    # Opposing qualifiers (first/last, start/end ...) -> different things.
    for family in QUALIFIER_FAMILIES:
        qa, qb = sa & family, sb & family
        if qa and qb and qa != qb:
            score *= 0.25
            break
    return score


# --------------------------------------------------------------------------
# Optional embeddings (field names only)
# --------------------------------------------------------------------------

MISTRAL_EMBED_URL = "https://api.mistral.ai/v1/embeddings"
EMBED_MODEL = os.environ.get("MISTRAL_EMBED_MODEL", "mistral-embed")
_embed_cache: dict[str, list[float]] = {}


def embeddings_enabled() -> bool:
    return bool(os.environ.get("MISTRAL_API_KEY")) and os.environ.get("DPI_EMBEDDINGS", "on").lower() != "off"


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """One batched call to Mistral's embeddings endpoint. Raises on failure."""
    resp = requests.post(
        MISTRAL_EMBED_URL,
        headers={"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"model": EMBED_MODEL, "input": texts},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]


def prefetch_embeddings(names: list[str], embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None) -> bool:
    """Embed every humanised name once. Returns False (and leaves the cache
    untouched) if embeddings are disabled or the call fails."""
    if embed_fn is None:
        if not embeddings_enabled():
            return False
        embed_fn = _embed_texts
    texts = sorted({humanise(n) for n in names if humanise(n)} - set(_embed_cache))
    if not texts:
        return True
    try:
        for text, vec in zip(texts, embed_fn(texts)):
            _embed_cache[text] = vec
        return True
    except Exception:
        return False


def _cosine(u: list[float], v: list[float]) -> float:
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(x * x for x in v))
    return sum(x * y for x, y in zip(u, v)) / (nu * nv) if nu and nv else 0.0


def embedding_similarity(a: str, b: str) -> Optional[float]:
    """Cosine rescaled so unrelated short strings land near 0.
    Raw cosines for short labels sit high (~0.5-0.7 even when unrelated);
    the 0.55-0.95 window is a provisional calibration - tune it on real pairs."""
    va, vb = _embed_cache.get(humanise(a)), _embed_cache.get(humanise(b))
    if va is None or vb is None:
        return None
    return max(0.0, min(1.0, (_cosine(va, vb) - 0.55) / 0.40))


def semantic_similarity(a: str, b: str) -> tuple[float, str]:
    """Returns (score, method) where method is 'lexicon' or 'lexicon+embedding'."""
    lex = lexicon_similarity(a, b)
    emb = embedding_similarity(a, b)
    if emb is None:
        return lex, "lexicon"
    # Opposing qualifiers are a hard signal embeddings tend to blur.
    blended = 0.5 * lex + 0.5 * emb
    if lex < 0.2 and any(
        (set(concept_tokens(a)) & f) and (set(concept_tokens(b)) & f)
        and (set(concept_tokens(a)) & f) != (set(concept_tokens(b)) & f)
        for f in QUALIFIER_FAMILIES
    ):
        blended = min(blended, lex)
    return blended, "lexicon+embedding"
