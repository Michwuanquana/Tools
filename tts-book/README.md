# tts-book

Audioknihy z textového scénáře. Kniha je soubor YAML s kapitolami a odstavci;
každý odstavec může mít vlastního mluvčího a režijní poznámku k přednesu
(„šeptem“, „naléhavě“). Skript to pošle do Gemini TTS (nebo lokálně do XTTS v2
na GPU) a složí z výsledku kapitoly v MP3.

- **Formát souboru:** [docs/FORMAT.md](docs/FORMAT.md), strojově
  [schema/tts-book.schema.json](schema/tts-book.schema.json)
- **Ukázka:** [examples/majak.book.yaml](examples/majak.book.yaml)

## Instalace

Potřebuješ Python 3.11+ a [ffmpeg](https://ffmpeg.org/) (pro MP3; bez něj jde
výstup jen do WAV).

```sh
cd tts-book
uv sync                 # nebo: python -m venv .venv && .venv/bin/pip install -e .
uv run tts-book --help  # s pip: .venv/bin/tts-book --help
```

API klíč vytvoříš v [Google AI Studiu](https://aistudio.google.com/apikey):

```sh
export GEMINI_API_KEY="…"
```

### Lokální XTTS v2 (volitelné)

Engine `xtts` běží offline na vlastní grafice přes
[coqui-tts](https://github.com/idiap/coqui-ai-TTS). Instaluje se jako extra:

```sh
uv sync --extra xtts    # torch 2.8 (na Windows CUDA 12.6 build z indexu PyTorch), coqui-tts
uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

- **Licence:** model XTTS v2 má licenci [Coqui CPML](https://coqui.ai/cpml) – **jen
  nekomerční použití**. Při prvním stažení se coqui zeptá na souhlas; bez terminálu
  (nebo předem) souhlas dáš proměnnou `COQUI_TOS_AGREED=1`.
- **Model:** výchozí `tts_models/multilingual/multi-dataset/xtts_v2` se stáhne
  (~1,9 GB) do `%LOCALAPPDATA%\tts` / `~/.local/share/tts`. Už stažený model
  (složka s `config.json`, `model.pth`, `speakers_xtts.pth`, `vocab.json`) použiješ
  přes `--model C:\cesta\XTTS-v2` nebo `engine.model` v knize.
- **GPU:** model zabere ~2 GB VRAM (špička při syntéze ~2,2 GB). Na RTX 3060
  Laptop (6 GB) vychází syntéza na RTF ~0,6–0,7 (1 min audia ≈ 40 s), načtení
  modelu ~30 s. Na CPU to jde taky, ale řádově pomaleji – `render` na to upozorní.
- **Hlasy:** `cast.<id>.xtts` – vlastní referenční nahrávka (klonování hlasu,
  6–30 s čisté řeči) nebo jeden z 58 vestavěných hlasů. Režie (`direction`) se
  modelu neposílá; místo ní jde podle klíčových slov přepínat nahrávky
  (`xtts.styles`). Viz [docs/FORMAT.md](docs/FORMAT.md#casted-xtts).
- **Jazyky:** en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn, ja, hu, ko, hi
  (slovenština ne). Text se dělí po větách na kratší úseky podle jazyka (čeština 186 znaků).

## Použití

```sh
# 1. kontrola souboru (schéma, postavy, ID, značky)
tts-book validate examples/majak.book.yaml

# 2. zkušební běh bez API – tóny místo řeči, ověří strukturu a pauzy
tts-book render examples/majak.book.yaml --engine mock

# 3. co se bude generovat a kolik to zhruba bude stát
tts-book plan examples/majak.book.yaml

# 4. ostrý render → examples/build/majak/01 - Kapitola první – Bouřka.mp3 …
tts-book render examples/majak.book.yaml
```

Další možnosti:

```sh
tts-book render kniha.book.yaml -c ch03            # jen vybrané kapitoly
tts-book render kniha.book.yaml --only ch03.p012    # přegenerovat jeden odstavec → previews/
tts-book render kniha.book.yaml --force             # ignorovat cache
tts-book say --voice Kore --style "šeptem" "Zkušební věta [sigh]."   # vyzkoušet hlas
tts-book render kniha.book.yaml --engine xtts       # lokálně přes XTTS v2
tts-book say --engine xtts --voice "Damien Black" "Zkušební věta."       # vestavěný hlas XTTS
tts-book say --engine xtts --voice hlasy/marta.wav -l cs-CZ "Zkušební věta."  # klon z nahrávky
tts-book schema -o schema/tts-book.schema.json      # znovu vygenerovat schéma
```

### Cache a přegenerování

Každý úsek audia se uloží do `build/<kniha>/cache/` pod hashem textu, hlasu,
režie a modelu. Opakovaný `render` vygeneruje jen to, co se změnilo – oprava
překlepu v jednom odstavci stojí jeden požadavek.

Model je generativní, takže každé vygenerování zní trochu jinak. Když se ti
odstavec nelíbí, `--only <id>` ho vygeneruje znovu, uloží ukázku do `previews/`
a nová verze nahradí tu v cache. Další `render` už ji použije.

### Cena

Gemini 3.8 Flash TTS účtuje audio po tokenech (25 tokenů za sekundu). Podle
ceníku k 11/2026 to je zhruba **$0.81 za hodinu audia** (od 1. 1. 2027 dvojnásobek),
Flash-Lite je o třetinu levnější. Text na vstupu je zanedbatelný. `tts-book plan`
spočítá odhad pro konkrétní knihu; ceník si ověř na
<https://aistudio.google.com/docs/pricing>.

## Jak to funguje

```
kniha.book.yaml
  → načtení a kontrola          loader.py, model.py, resolve.check
  → plochý plán požadavků        resolve.build_plan (dědičnost, režie, lexikon, značky, dělení)
  → syntéza s cache              render.synthesize + engines/gemini.py
  → skládání kapitol             render.assemble_chapter + audio.py (ffmpeg)
```

Napojení na Gemini API (`v1beta/interactions`) je celé v
`src/tts_book/engines/gemini.py` – API je v betě, případné změny se opraví tam.
XTTS je v `src/tts_book/engines/xtts.py` (torch se importuje až při vytvoření
enginu), výběr hlasu, jazyky a limity textu v `src/tts_book/xtts.py`.

## Vývoj

```sh
uv run pytest
uvx ruff check src tests && uvx ruff format --check src tests
```

## Stav a plán

- [x] **Fáze 1:** formát a schéma, `validate`, `plan`, `render` s cache, segmenty,
  dělení dlouhých textů, MP3/WAV po kapitolách, `say`, offline engine `mock`
- [x] Lokální engine XTTS v2 (vestavěné hlasy, klonování z nahrávky, nahrávky podle režie)
- [ ] **Fáze 2:** normalizace hlasitosti, M4B s kapitolami, paralelní požadavky, Batch API (½ ceny)
- [ ] **Fáze 3:** import z txt/md (později epub), automatické vytvoření hlasů z `design`,
  výpis knihovny hlasů
- [ ] **Fáze 4:** automatická režie přes LLM, dialogový režim (2 mluvčí v jednom
  požadavku), webové rozhraní pro poslech a ladění odstavců
