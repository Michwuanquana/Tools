# Formát souboru knihy (`*.book.yaml`)

Kniha je jeden soubor YAML. Popisuje **co** se čte (kapitoly a odstavce),
**kdo** to čte (obsazení) a **jak** (režijní poznámky).

Strojová definice formátu je v [`schema/tts-book.schema.json`](../schema/tts-book.schema.json)
(JSON Schema, generuje se z `src/tts_book/model.py` příkazem `tts-book schema`).
Když na první řádek souboru dáš

```yaml
# yaml-language-server: $schema=../schema/tts-book.schema.json
```

dostaneš ve VS Code (rozšíření *YAML* od Red Hatu) našeptávání klíčů, popisky
a kontrolu chyb přímo při psaní. Cestu uprav podle toho, kde soubor leží.

Úplná ukázka: [`examples/majak.book.yaml`](../examples/majak.book.yaml).

## Struktura

```yaml
version: 1          # verze formátu (povinné)
meta: …             # název, autor, jazyk (povinné)
engine: …           # TTS backend a model
output: …           # formát výstupu a pauzy
defaults: …         # výchozí mluvčí a režie pro celou knihu
cast: …             # obsazení – hlasy postav (povinné)
lexicon: …          # textové náhrady (zkratky, výslovnost)
chapters: …         # kapitoly s odstavci (povinné)
```

Neznámé klíče jsou chyba – překlep jako `speakr:` se tak neztratí.

## `meta`

| klíč | povinné | popis |
|---|---|---|
| `title` | ano | Název knihy (v MP3 jako album). |
| `author` | | Autor (v MP3 jako interpret). |
| `language` | ano | Výchozí jazyk textu jako [BCP 47](https://en.wikipedia.org/wiki/IETF_language_tag): `cs-CZ`, `en-US`, `de`, … |

Gemini jazyk pozná sám (umí přes 130 jazyků), takže `language` se do API
neposílá. Používá se pro dělení textu na věty, budoucí import a další enginy.
Jazyk jde přepsat u kapitoly, odstavce i segmentu – třeba u cizojazyčného
citátu.

## `engine`

| klíč | výchozí | popis |
|---|---|---|
| `name` | `gemini` | `gemini` = Gemini API přes API klíč, `mock` = offline testovací tóny. |
| `model` | `gemini-3.8-flash-tts` | Např. `gemini-3.8-flash-lite-tts` (levnější). |
| `sample_rate` | `24000` | Vzorkovací frekvence v Hz (`24000` nebo `16000`). |
| `max_chars` | `2000` | Max. znaků na jeden požadavek; delší text se rozdělí po větách. |
| `api_key_env` | `GEMINI_API_KEY` | Proměnná prostředí s API klíčem. |

Model vrátí nejvýš ~655 s audia na požadavek; 2000 znaků jsou zhruba 2–3 minuty
řeči, takže je rezerva.

## `output`

| klíč | výchozí | popis |
|---|---|---|
| `dir` | `build/<název souboru>` | Výstupní složka (relativně k souboru knihy). Je v ní i `cache/`. |
| `format` | `mp3` | `mp3` (přes ffmpeg) nebo `wav`. |
| `bitrate` | `128k` | Datový tok MP3. |
| `read_titles` | `true` | Přečíst název kapitoly na jejím začátku. |
| `pauses` | | Pauzy – viz níže. |

### `output.pauses`

Ticho mezi úseky vkládá skript (přesně), ne model. Délka je číslo v sekundách
(`0.8`) nebo text (`"800ms"`, `"1.5s"`).

| klíč | výchozí | kde |
|---|---|---|
| `paragraph` | `0.8` | mezi odstavci (u odstavce jde přepsat přes `pause_after`) |
| `segment` | `0.2` | mezi segmenty uvnitř odstavce |
| `chunk` | `0.3` | mezi částmi dlouhého odstavce rozděleného po větách |
| `title` | `1.5` | po přečteném názvu kapitoly |
| `chapter_start` | `0.5` | na začátku souboru kapitoly |
| `chapter_end` | `2.0` | na konci souboru kapitoly |

## `defaults`

| klíč | výchozí | popis |
|---|---|---|
| `speaker` | `narrator` | Výchozí mluvčí – musí existovat v `cast`. Čte i názvy kapitol. |
| `direction` | | Režie platná pro celou knihu, např. `přednes audioknihy, bez spěchu`. |

## `cast` – obsazení

Klíč je ID mluvčího (písmena, číslice, `_ . -`), hodnota jeho hlas:

```yaml
cast:
  narrator:
    name: Vypravěč              # jen pro přehled
    voice: Charon               # předpřipravený hlas
    style: klidný, vřelý        # krátký výchozí přednes
  marta:
    voice: voice_abc123…        # vlastní hlas z Voice design
    design: starší žena od moře, hluboký zastřený hlas
```

| klíč | popis |
|---|---|
| `voice` | Předpřipravený hlas (viz tabulka), hlas z knihovny nebo ID vlastního hlasu `voice_…`. |
| `design` | Popis hlasu pro Voice design. Automatické vytvoření zatím není hotové – vytvoř hlas v [AI Studiu](https://aistudio.google.com/generate-speech), jeho ID vlož do `voice` a popis si tu nech jako dokumentaci. |
| `style` | Krátký výchozí způsob přednesu postavy. |
| `name` | Zobrazované jméno. |

Postava musí mít `voice` nebo `design`; k renderu je potřeba `voice`.

**Tip:** Kdo mluví, určuje hlas, ne `style`. Dlouhé popisy postavy ve stylu
(„Audio profile…“, odrážky s režijními pokyny) podle Googlu nejčastěji způsobují,
že se hlas během knihy mění. Pro stálou postavu navrhni hlas ve Voice design
a `style` nech na pár slov.

### Předpřipravené hlasy

| | | |
|---|---|---|
| Zephyr – jasný | Puck – energický | Charon – informativní |
| Kore – pevný | Fenrir – vzrušený | Leda – mladistvý |
| Orus – pevný | Aoede – svěží | Callirrhoe – pohodový |
| Autonoe – jasný | Enceladus – dýchavý | Iapetus – zřetelný |
| Umbriel – pohodový | Algieba – hladký | Despina – hladký |
| Erinome – zřetelný | Algenib – chraplavý | Rasalgethi – informativní |
| Laomedeia – energický | Achernar – měkký | Alnilam – pevný |
| Schedar – vyrovnaný | Gacrux – zralý | Pulcherrima – přímý |
| Achird – přátelský | Zubenelgenubi – ležérní | Vindemiatrix – jemný |
| Sadachbia – živý | Sadaltager – znalý | Sulafat – vřelý |

Hlasy nejsou vázané na jazyk – jazyk model pozná z textu. Vyzkoušíš je příkazem
`tts-book say --voice Kore "Zkušební věta."`.

## `lexicon` – textové náhrady

```yaml
lexicon:
  "km": "kilometrů"
  "sv.": "svatý"
  "Nguyen": "Nguyen"   # přepis výslovnosti, např. "Ngujen"
```

Nahrazují se jen celá slova a rozlišuje se velikost písmen. Delší klíče mají
přednost. Náhrady se aplikují před syntézou, text v souboru zůstává beze změny.

## `chapters` – kapitoly

```yaml
chapters:
  - id: bourka                  # volitelné; jinak ch01, ch02, …
    title: Kapitola první       # přečte se, pokud read_titles
    speaker: marta              # výchozí mluvčí kapitoly (jiný vypravěč)
    direction: napjatá nálada   # režie pro celou kapitolu
    language: cs-CZ
    read_title: false           # přepíše output.read_titles
    skip: true                  # kapitolu vynechat
    paragraphs: [ … ]
```

Jen `paragraphs` je povinné.

## Odstavce

Nejkratší zápis odstavce je prostý řetězec – čte ho výchozí mluvčí:

```yaml
paragraphs:
  - Krátký odstavec na jednom řádku.
  - >
    Delší odstavec se dá zalomit na víc řádků. Zalomení i vícenásobné
    mezery se sloučí do jedné mezery.
```

Plný zápis:

```yaml
  - id: bourka.marta-okno       # stabilní ID (jinak ch01.p002 podle pořadí)
    speaker: marta
    direction: tiše, skoro pro sebe
    text: "[sigh] Zase se to žene od severu."
    pause_after: 1.5s           # ticho po odstavci
    language: cs-CZ
    skip: false
```

Odstavec má **buď `text`, nebo `segments`**. Segmenty slouží pro odstavec, ve
kterém se střídají mluvčí nebo režie – typicky přímá řeč s uvozovací větou:

```yaml
  - segments:
      - speaker: tomas
        direction: vzrušeně
        text: "„Babi, můžu dneska nahoru se světlem?“"
      - text: zeptal se Tomáš a už stál u schodů.   # mluvčí se dědí
```

Segment má `text` (povinný), `speaker`, `direction` a `language`.

**`text` je doslovný přepis.** Model ho přečte celý, takže režijní poznámky
(„šeptem“, „(smutně)“) patří do `direction`, nikdy do `text`.

### ID odstavců

ID slouží k adresování (`tts-book render kniha.yaml --only ch01.p005`) a v
chybových hlášeních. Bez explicitního `id` se generuje `<id kapitoly>.p<pořadí>`,
takže vložení odstavce posune čísla těch za ním. U odstavců, ke kterým se budeš
vracet, dej vlastní `id`. ID musí být v celé knize unikátní.

## Dědičnost a skládání režie

| vlastnost | odkud se bere (první nalezená) |
|---|---|
| mluvčí | segment → odstavec → kapitola → `defaults.speaker` |
| jazyk | segment → odstavec → kapitola → `meta.language` |

Režie se **nepřepisuje, ale skládá** v tomto pořadí a spojí se středníkem:

```
defaults.direction ; cast.<mluvčí>.style ; kapitola.direction ; odstavec.direction ; segment.direction
```

Příklad z ukázky – Tomášova replika v první kapitole dostane styl:

```
přednes audioknihy, přirozeně, bez spěchu; kluk kolem deseti let, zvědavý, rychlejší; napjatá, ale tichá nálada; vzrušeně, nedočkavě
```

`tts-book validate` upozorní, když složená režie přesáhne 300 znaků.

## Značky v textu

Okamžité zvuky a pauzy se píšou přímo do textu v hranatých závorkách.
Skript je převede na formát Gemini (`<sigh>`):

```yaml
text: "Hmm… [short pause] Ne. [sigh] Tak dobře."
```

| druh | značky |
|---|---|
| pauzy | `[short pause]`, `[long pause]` |
| dech | `[breath]`, `[heavy breath]`, `[exhales]`, `[sigh]`, `[sighs]`, `[pant]`, `[yawn]` |
| smích | `[laugh]`, `[laughter]`, `[chuckle]`, `[chuckles]`, `[giggle]`, `[snicker]`, `[cackle]` |
| emoce | `[cry]`, `[sob]`, `[whimper]`, `[gasp]`, `[groan]`, `[moan]`, `[scream]`, `[shriek]`, `[shout]`, `[cheer]` |
| ostatní | `[cough]`, `[sneeze]`, `[snort]`, `[throat-clearing]`, `[tsk]`, `[hiss]`, `[growl]`, `[grunt]`, `[grr]`, `[argh]`, `[pff]`, `[phew]`, `[whispers]`, `[whispering]` |

Značky piš anglicky i v českém textu (Google to tak doporučuje). Hranaté
závorky, které nejsou značkou (`[pozn. red.]`), zůstanou jako text;
`validate` upozorní na slovo v závorkách, které vypadá jako překlep ve značce.

Další způsoby řízení přednesu:

- **tempo a nálada celé repliky** → `direction` (`mluví rychle`, `monotónně`, `vysoký hlas, nadšeně`),
- **důraz** → VELKÁ PÍSMENA u slova,
- **váhání** → interpunkce: `…`, `--`, čárky.

## Jak se kniha renderuje

1. Načte se YAML a ověří proti schématu, pak se zkontrolují odkazy (mluvčí, ID, značky).
2. Kniha se rozbalí na plochý seznam požadavků: dědičnost, režie, lexikon, značky,
   dělení dlouhých textů po větách.
3. Každý požadavek má hash z textu, hlasu, režie, modelu a vzorkovací frekvence.
   Hotové audio se ukládá do `cache/` pod tímto hashem – po úpravě jednoho
   odstavce se přegeneruje jen ten.
4. Kapitola se složí z řeči a ticha a zakóduje do MP3 (s ID3 tagy) nebo WAV.
