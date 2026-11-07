"""Definice formátu souboru knihy (*.book.yaml).

Tyto modely jsou jediný zdroj pravdy: z nich se generuje JSON Schema
(`tts-book schema`) a podle nich se soubor validuje při načtení.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

FORMAT_VERSION = 1

# --- Pomocné typy -----------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*$")
_DURATION_PATTERN = r"^\s*\d+(\.\d+)?\s*(ms|s)?\s*$"


def parse_duration(value: Any) -> float:
    """Převede 0.8 / "800ms" / "1.5s" na sekundy (float)."""
    if isinstance(value, bool):
        raise ValueError("délka musí být číslo nebo text jako '800ms' / '1.5s'")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("délka nesmí být záporná")
        return float(value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if not m:
            raise ValueError(f"neplatná délka {value!r} – použij např. 0.8, '800ms' nebo '1.5s'")
        number = float(m.group(1))
        return number / 1000 if m.group(2) == "ms" else number
    raise ValueError("délka musí být číslo nebo text jako '800ms' / '1.5s'")


Duration = Annotated[
    float,
    BeforeValidator(parse_duration),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number", "minimum": 0},
                {"type": "string", "pattern": _DURATION_PATTERN},
            ],
            "description": "Délka v sekundách: číslo (0.8) nebo text ('800ms', '1.5s').",
        }
    ),
]

Id = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=64),
]

Language = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"),
]

NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class Strict(BaseModel):
    """Základ: neznámé klíče jsou chyba (odhalí překlepy v YAML)."""

    model_config = ConfigDict(extra="forbid")


# --- Hlavička, engine, výstup ------------------------------------------------


class Meta(Strict):
    title: NonEmptyText = Field(description="Název knihy (album v ID3 tazích).")
    author: str | None = Field(None, description="Autor (interpret v ID3 tazích).")
    language: Language = Field(
        description=(
            "Výchozí jazyk textu jako BCP 47 kód, např. 'cs-CZ', 'en-US', 'de'. "
            "Gemini jazyk detekuje sám; kód slouží dělení na věty, importu a "
            "dalším enginům. Lze přepsat u kapitoly, odstavce i segmentu."
        )
    )


EngineName = Literal["gemini", "xtts", "mock"]

DEFAULT_MODELS: dict[str, str] = {
    "gemini": "gemini-3.8-flash-tts",
    "xtts": "tts_models/multilingual/multi-dataset/xtts_v2",
    "mock": "mock",
}


class Engine(Strict):
    name: EngineName = Field(
        "gemini",
        description=(
            "TTS backend. 'gemini' = Gemini API (API klíč), 'xtts' = lokální XTTS v2 (coqui-tts, GPU), "
            "'mock' = offline testovací tóny bez volání API."
        ),
    )
    model: str | None = Field(
        None,
        description=(
            "Model enginu. Když chybí, použije se výchozí: gemini → 'gemini-3.8-flash-tts', "
            "xtts → 'tts_models/multilingual/multi-dataset/xtts_v2' (u xtts jde zadat i složku "
            "se staženým modelem)."
        ),
    )
    sample_rate: Literal[24000, 16000] = Field(24000, description="Vzorkovací frekvence generovaného audia v Hz.")
    max_chars: int = Field(
        2000,
        ge=200,
        le=8000,
        description=(
            "Nejvyšší počet znaků v jednom požadavku. Delší text se rozdělí po "
            "větách. Model vrátí max. ~655 s audia na požadavek."
        ),
    )
    api_key_env: str = Field(
        "GEMINI_API_KEY",
        description="Název proměnné prostředí s API klíčem.",
    )

    @model_validator(mode="after")
    def _default_model(self) -> Engine:
        if not self.model:
            self.model = DEFAULT_MODELS[self.name]
        return self


def model_for(book: Book, engine: str | None = None, override: str | None = None) -> str:
    """Model pro render: `--model` > model z knihy (jen pro engine z knihy) > výchozí model enginu."""
    if override:
        return override
    engine = engine or book.engine.name
    if engine == book.engine.name and book.engine.model:
        return book.engine.model
    return DEFAULT_MODELS.get(engine, engine)


class Pauses(Strict):
    paragraph: Duration = Field(0.8, description="Ticho mezi odstavci.")
    segment: Duration = Field(0.2, description="Ticho mezi segmenty uvnitř odstavce.")
    chunk: Duration = Field(0.3, description="Ticho mezi částmi příliš dlouhého textu (dělení po větách).")
    title: Duration = Field(1.5, description="Ticho po přečteném názvu kapitoly.")
    chapter_start: Duration = Field(0.5, description="Ticho na začátku souboru kapitoly.")
    chapter_end: Duration = Field(2.0, description="Ticho na konci souboru kapitoly.")


class Output(Strict):
    dir: str | None = Field(
        None,
        description=(
            "Výstupní složka (relativně k souboru knihy). Výchozí je "
            "build/<název souboru>. Je v ní i cache vygenerovaných odstavců."
        ),
    )
    format: Literal["mp3", "wav"] = Field("mp3", description="Formát souborů kapitol.")
    bitrate: Annotated[str, StringConstraints(pattern=r"^\d{2,3}k$")] = Field(
        "128k", description="Datový tok MP3, např. '96k', '128k', '192k'."
    )
    read_titles: bool = Field(True, description="Přečíst název kapitoly na jejím začátku (hlasem `defaults.speaker`).")
    pauses: Pauses = Field(default_factory=Pauses)


# --- Obsazení ----------------------------------------------------------------


Recording = Annotated[str, StringConstraints(min_length=1)]
Recordings = Recording | Annotated[list[Recording], Field(min_length=1)]


class XttsVoice(Strict):
    model_config = ConfigDict(json_schema_extra={"oneOf": [{"required": ["reference"]}, {"required": ["speaker"]}]})

    reference: Recordings | None = Field(
        None,
        description=(
            "Referenční nahrávka hlasu (WAV, 6–30 s čisté řeči bez hudby a šumu) nebo seznam nahrávek. "
            "Cesta relativně k souboru knihy."
        ),
    )
    speaker: str | None = Field(
        None,
        description="Vestavěný hlas XTTS (např. 'Claribel Dervla', 'Damien Black'). Buď `reference`, nebo `speaker`.",
    )
    styles: dict[NonEmptyText, Recordings] = Field(
        default_factory=dict,
        description=(
            "Nahrávky pro různé polohy hlasu: {klíčové slovo: nahrávka}. Když se klíčové slovo objeví "
            "ve složené režii (např. 'šeptem'), použije se jeho nahrávka; při více shodách vyhrává ta "
            "nejvíc vpravo (nejkonkrétnější úroveň). Jinak platí `reference` / `speaker`."
        ),
    )

    @model_validator(mode="after")
    def _reference_xor_speaker(self) -> XttsVoice:
        if (self.reference is None) == (self.speaker is None):
            raise ValueError("`xtts` potřebuje buď `reference`, nebo `speaker` (ne obojí)")
        return self


class CastMember(Strict):
    model_config = ConfigDict(
        json_schema_extra={"anyOf": [{"required": ["voice"]}, {"required": ["design"]}, {"required": ["xtts"]}]}
    )

    name: str | None = Field(None, description="Zobrazované jméno postavy (jen pro přehled).")
    voice: str | None = Field(
        None,
        description=(
            "Hlas: předpřipravený (např. 'Kore', 'Puck', 'Charon'), hlas z knihovny "
            "nebo ID vlastního hlasu 'voice_…' vytvořeného ve Voice design."
        ),
    )
    design: str | None = Field(
        None,
        description=(
            "Popis hlasu pro Voice design (např. 'starší laskavá žena, lehce chraplavá'). "
            "Automatické vytvoření hlasu zatím není hotové – vytvoř hlas v AI Studiu "
            "a jeho ID vlož do `voice`. Popis si tu nech jako dokumentaci."
        ),
    )
    style: str | None = Field(
        None,
        description=(
            "Krátký výchozí způsob přednesu postavy, např. 'klidně, pomaleji'. "
            "Identitu nese hlas – dlouhé popisy zhoršují stálost hlasu."
        ),
    )
    xtts: XttsVoice | None = Field(
        None,
        description="Hlas pro engine xtts: referenční nahrávka nebo vestavěný hlas, volitelně nahrávky podle režie.",
    )

    @model_validator(mode="after")
    def _voice_or_design(self) -> CastMember:
        if not self.voice and not self.design and not self.xtts:
            raise ValueError("postava potřebuje `voice`, `design` nebo `xtts`")
        return self


class Defaults(Strict):
    speaker: Id = Field("narrator", description="Výchozí mluvčí (klíč z `cast`).")
    direction: str | None = Field(
        None,
        description="Režijní poznámka platná pro celou knihu (připojuje se ke všem ostatním).",
    )


# --- Obsah -------------------------------------------------------------------

_SPEAKER = "Mluvčí (klíč z `cast`). Když chybí, dědí se z nadřazené úrovně."
_DIRECTION = (
    "Režijní poznámka k přednesu (např. 'šeptem, ustaraně'). Poznámky se "
    "skládají za sebe: kniha → postava → kapitola → odstavec → segment."
)
_LANGUAGE = "Jazyk této části, pokud se liší od nadřazené úrovně (BCP 47)."


class Segment(Strict):
    text: NonEmptyText = Field(description="Doslovný text. Režijní poznámky sem nepatří – jen do `direction`.")
    speaker: Id | None = Field(None, description=_SPEAKER)
    direction: str | None = Field(None, description=_DIRECTION)
    language: Language | None = Field(None, description=_LANGUAGE)


class Paragraph(Strict):
    model_config = ConfigDict(json_schema_extra={"oneOf": [{"required": ["text"]}, {"required": ["segments"]}]})

    id: Id | None = Field(
        None,
        description="Stabilní ID odstavce. Když chybí, vygeneruje se z pořadí (např. 'ch01.p003').",
    )
    text: NonEmptyText | None = Field(
        None,
        description="Doslovný text odstavce. Buď `text`, nebo `segments`.",
    )
    segments: list[Segment] | None = Field(
        None,
        min_length=1,
        description="Odstavec složený z více částí (různí mluvčí / různá režie).",
    )
    speaker: Id | None = Field(None, description=_SPEAKER)
    direction: str | None = Field(None, description=_DIRECTION)
    language: Language | None = Field(None, description=_LANGUAGE)
    pause_after: Duration | None = Field(None, description="Ticho po odstavci (přepíše `output.pauses.paragraph`).")
    skip: bool = Field(False, description="Odstavec vynechat z renderu.")

    @model_validator(mode="after")
    def _text_xor_segments(self) -> Paragraph:
        if (self.text is None) == (self.segments is None):
            raise ValueError("odstavec musí mít buď `text`, nebo `segments` (ne obojí)")
        return self


class Chapter(Strict):
    id: Id | None = Field(None, description="ID kapitoly. Když chybí, vygeneruje se z pořadí ('ch01', 'ch02', …).")
    title: str | None = Field(None, description="Název kapitoly (čte se, pokud `read_titles`).")
    speaker: Id | None = Field(None, description="Výchozí mluvčí kapitoly (např. kapitola vyprávěná jinou postavou).")
    direction: str | None = Field(None, description=_DIRECTION)
    language: Language | None = Field(None, description=_LANGUAGE)
    read_title: bool | None = Field(None, description="Přepíše `output.read_titles` pro tuto kapitolu.")
    skip: bool = Field(False, description="Kapitolu vynechat z renderu.")
    paragraphs: list[str | Paragraph] = Field(
        min_length=1,
        description="Odstavce. Prostý řetězec je zkratka pro odstavec jen s textem.",
    )


class Book(Strict):
    """Soubor knihy pro tts-book."""

    model_config = ConfigDict(extra="forbid", title="tts-book")

    version: Literal[1] = Field(description="Verze formátu souboru.")
    meta: Meta
    engine: Engine = Field(default_factory=Engine)
    output: Output = Field(default_factory=Output)
    defaults: Defaults = Field(default_factory=Defaults)
    cast: dict[Id, CastMember] = Field(
        min_length=1, description="Obsazení: klíč je ID mluvčího, hodnota jeho hlas a styl."
    )
    lexicon: dict[NonEmptyText, str] = Field(
        default_factory=dict,
        description=(
            "Textové náhrady před syntézou (zkratky, výslovnost jmen). Celá slova, "
            "rozlišuje velikost písmen. Např. {'sv.': 'svatý'}."
        ),
    )
    chapters: list[Chapter] = Field(min_length=1)
