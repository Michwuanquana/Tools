"""Úpravy textu před syntézou: mezery, lexikon, značky, dělení na části."""

from __future__ import annotations

import re

# Značky, které Gemini 3.8 TTS doporučuje (v API se píšou jako <tag>).
# V souboru knihy se píšou neutrálně v hranatých závorkách: [sigh], [short pause].
KNOWN_TAGS: frozenset[str] = frozenset(
    {
        "argh",
        "breath",
        "heavy breath",
        "exhales",
        "cackle",
        "cheer",
        "chuckle",
        "chuckles",
        "cough",
        "cry",
        "gasp",
        "giggle",
        "groan",
        "growl",
        "grunt",
        "grr",
        "hiss",
        "laugh",
        "laughter",
        "moan",
        "pant",
        "pff",
        "phew",
        "scream",
        "shout",
        "shriek",
        "sigh",
        "sighs",
        "sneeze",
        "snicker",
        "snort",
        "sob",
        "throat-clearing",
        "tsk",
        "whimper",
        "whispers",
        "whispering",
        "yawn",
        "short pause",
        "long pause",
    }
)

_TAG_RE = re.compile(r"\[([A-Za-z][A-Za-z -]*[A-Za-z])\]")


def normalize_whitespace(text: str) -> str:
    """Sloučí zalomení řádků a vícenásobné mezery (YAML bloky `|` / `>`)."""
    return " ".join(text.split())


def find_tags(text: str) -> tuple[list[str], list[str]]:
    """Vrátí (známé, neznámé) značky v hranatých závorkách."""
    known, unknown = [], []
    for m in _TAG_RE.finditer(text):
        tag = m.group(1).lower()
        (known if tag in KNOWN_TAGS else unknown).append(m.group(1))
    return known, unknown


def convert_tags(text: str) -> str:
    """[sigh] → <sigh>. Neznámé hranaté závorky (např. [pozn. red.]) nechá být."""

    def repl(m: re.Match[str]) -> str:
        tag = m.group(1).lower()
        return f"<{tag}>" if tag in KNOWN_TAGS else m.group(0)

    return _TAG_RE.sub(repl, text)


def strip_tags(text: str) -> str:
    """Odstraní známé značky (pro počítání délky textu)."""
    return normalize_whitespace(_TAG_RE.sub(lambda m: "" if m.group(1).lower() in KNOWN_TAGS else m.group(0), text))


def compile_lexicon(lexicon: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    rules = []
    # Delší klíče první, ať "sv. Václav" vyhraje nad "sv.".
    for key in sorted(lexicon, key=len, reverse=True):
        pattern = re.escape(key)
        if key[0].isalnum() or key[0] == "_":
            pattern = r"(?<!\w)" + pattern
        if key[-1].isalnum() or key[-1] == "_":
            pattern = pattern + r"(?!\w)"
        rules.append((re.compile(pattern), lexicon[key]))
    return rules


def apply_lexicon(text: str, rules: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in rules:
        text = pattern.sub(lambda _m, r=replacement: r, text)
    return text


# --- Dělení dlouhých textů ----------------------------------------------------

_SENTENCE_END_RE = re.compile(r"([.!?…]+[\"'“”„‟»«’‘)\]]*)\s+")
_CLAUSE_END_RE = re.compile(r"([,;:—–])\s+")


def _first_letter(s: str) -> str | None:
    for ch in s:
        if ch.isalpha():
            return ch
        if ch.isdigit():
            return ch
    return None


def split_sentences(text: str) -> list[str]:
    """Rozdělí text na věty. Nedělí, když další slovo začíná malým písmenem
    (typicky zkratky: „např. tady“, „sv. Václav“ se ale rozdělit může)."""
    parts: list[str] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        nxt = _first_letter(text[m.end() : m.end() + 4])
        if nxt is not None and nxt.isalpha() and nxt.islower():
            continue
        parts.append(text[start : m.end(1)])
        start = m.end()
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Rozdělí příliš dlouhou větu po čárkách, a když to nestačí, po slovech."""
    pieces: list[str] = []
    start = 0
    for m in _CLAUSE_END_RE.finditer(text):
        pieces.append(text[start : m.end(1)])
        start = m.end()
    pieces.append(text[start:])
    out: list[str] = []
    for piece in _pack([p.strip() for p in pieces if p.strip()], max_chars):
        if len(piece) <= max_chars:
            out.append(piece)
            continue
        words = piece.split(" ")
        out.extend(_pack(words, max_chars))
    return out


def _pack(units: list[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}" if current else unit
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Rozdělí text na části ≤ max_chars, přednostně na hranicích vět."""
    if len(text) <= max_chars:
        return [text]
    sentences: list[str] = []
    for sentence in split_sentences(text):
        if len(sentence) > max_chars:
            sentences.extend(_hard_split(sentence, max_chars))
        else:
            sentences.append(sentence)
    return _pack(sentences, max_chars)
