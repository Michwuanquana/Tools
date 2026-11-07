"""XTTS v2 bez závislosti na torch: jazyky, limity textu a výběr hlasu.

Samotná syntéza je v `engines/xtts.py`; tady je jen to, co potřebuje
kontrola a plán (`resolve`) a co jde testovat bez modelu.
"""

from __future__ import annotations

from .model import Recordings, XttsVoice

# Jazyky XTTS v2 (config.languages modelu).
XTTS_LANGUAGES: frozenset[str] = frozenset(
    {"en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"}
)

# Doporučený limit znaků na jedno volání (TTS.tts.layers.xtts.tokenizer.VoiceBpeTokenizer.char_limits).
# Delší text model sice přijme, ale kvalita klesá a může ho useknout.
XTTS_CHAR_LIMITS: dict[str, int] = {
    "en": 250,
    "de": 253,
    "fr": 273,
    "es": 239,
    "it": 213,
    "pt": 203,
    "pl": 224,
    "zh-cn": 82,
    "ar": 166,
    "cs": 186,
    "ru": 182,
    "nl": 251,
    "tr": 226,
    "ja": 71,
    "hu": 224,
    "ko": 95,
    "hi": 150,
}
DEFAULT_CHAR_LIMIT = 200
SAMPLE_RATE = 24000  # výstup XTTS v2

# Vestavěné hlasy XTTS v2 (speakers_xtts.pth) – pro nabídku v GUI; engine bere seznam z modelu.
XTTS_SPEAKERS: tuple[str, ...] = (
    "Claribel Dervla",
    "Daisy Studious",
    "Gracie Wise",
    "Tammie Ema",
    "Alison Dietlinde",
    "Ana Florence",
    "Annmarie Nele",
    "Asya Anara",
    "Brenda Stern",
    "Gitta Nikolina",
    "Henriette Usha",
    "Sofia Hellen",
    "Tammy Grit",
    "Tanja Adelina",
    "Vjollca Johnnie",
    "Andrew Chipper",
    "Badr Odhiambo",
    "Dionisio Schuyler",
    "Royston Min",
    "Viktor Eka",
    "Abrahan Mack",
    "Adde Michal",
    "Baldur Sanjin",
    "Craig Gutsy",
    "Damien Black",
    "Gilberto Mathias",
    "Ilkin Urbano",
    "Kazuhiko Atallah",
    "Ludvig Milivoj",
    "Suad Qasim",
    "Torcull Diarmuid",
    "Viktor Menelaos",
    "Zacharie Aimilios",
    "Nova Hogarth",
    "Maja Ruoho",
    "Uta Obando",
    "Lidiya Szekeres",
    "Chandra MacFarland",
    "Szofi Granger",
    "Camilla Holmström",
    "Lilya Stainthorpe",
    "Zofija Kendrick",
    "Narelle Moon",
    "Barbora MacLean",
    "Alexandra Hisakawa",
    "Alma María",
    "Rosemary Okafor",
    "Ige Behringer",
    "Filip Traverse",
    "Damjan Chapman",
    "Wulf Carlevaro",
    "Aaron Dreschner",
    "Kumar Dahl",
    "Eugenio Mataracı",
    "Ferran Simen",
    "Xavier Hayasaka",
    "Luis Moray",
    "Marcos Rudaski",
)

WAV_PREFIX = "wav:"
SPEAKER_PREFIX = "speaker:"


def xtts_language(language: str) -> str | None:
    """BCP 47 → kód jazyka XTTS ('cs-CZ' → 'cs', 'zh-Hans-CN' → 'zh-cn'); None = nepodporovaný."""
    primary = language.split("-")[0].lower()
    code = "zh-cn" if primary == "zh" else primary
    return code if code in XTTS_LANGUAGES else None


def char_limit(language: str) -> int:
    code = xtts_language(language)
    return XTTS_CHAR_LIMITS.get(code, DEFAULT_CHAR_LIMIT) if code else DEFAULT_CHAR_LIMIT


def _as_list(recordings: Recordings) -> list[str]:
    return [recordings] if isinstance(recordings, str) else list(recordings)


def wav_spec(recordings: Recordings) -> str:
    """Hlas z nahrávek jako text do `Job.voice`: 'wav:a.wav|b.wav'."""
    return WAV_PREFIX + "|".join(_as_list(recordings))


def speaker_spec(name: str) -> str:
    return SPEAKER_PREFIX + name


def parse_voice(voice: str) -> tuple[str, list[str] | str]:
    """'wav:a|b' → ('wav', ['a', 'b']), 'speaker:X' → ('speaker', 'X')."""
    if voice.startswith(WAV_PREFIX):
        return "wav", voice[len(WAV_PREFIX) :].split("|")
    if voice.startswith(SPEAKER_PREFIX):
        return "speaker", voice[len(SPEAKER_PREFIX) :]
    raise ValueError(f"neplatný hlas XTTS {voice!r} (čekám 'wav:…' nebo 'speaker:…')")


def matching_style(styles: dict[str, Recordings], direction: str | None) -> str | None:
    """Klíč ze `styles`, který se ve složené režii vyskytuje nejvíc vpravo (bez ohledu na velikost písmen).

    Režie se skládá od nejobecnější úrovně po nejkonkrétnější, takže pozdější výskyt
    vyhrává. Při shodě pozice vyhraje delší klíč ('velmi tiše' před 'tiše').
    """
    if not direction or not styles:
        return None
    haystack = direction.lower()
    best: tuple[int, int] | None = None
    best_key = None
    for key in styles:
        pos = haystack.rfind(key.lower())
        if pos < 0:
            continue
        rank = (pos + len(key), len(key))
        if best is None or rank > best:
            best, best_key = rank, key
    return best_key


def voice_for(voice: XttsVoice, direction: str | None) -> str:
    """Hlas pro `Job.voice` podle režie: nahrávka ze `styles`, jinak `reference` / `speaker`."""
    key = matching_style(voice.styles, direction)
    if key is not None:
        return wav_spec(voice.styles[key])
    if voice.reference is not None:
        return wav_spec(voice.reference)
    assert voice.speaker is not None
    return speaker_spec(voice.speaker)


def references(voice: XttsVoice) -> list[str]:
    """Všechny nahrávky postavy (výchozí i ze `styles`)."""
    out = _as_list(voice.reference) if voice.reference is not None else []
    for recordings in voice.styles.values():
        out.extend(_as_list(recordings))
    return out
