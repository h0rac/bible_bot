import os
import re
import time
import html as html_lib
import ast
import aiohttp
import asyncio
import random
from urllib.parse import quote_plus, quote

import discord
from discord.ext import commands

# --------------- konfiguracja ---------------
if os.path.exists(".env"):
    from dotenv import load_dotenv
    load_dotenv()

BOT_PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=INTENTS)

# === API.BIBLE ===
API_BIBLE_BASE = os.getenv("API_BIBLE_BASE", "https://api.scripture.api.bible/v1")
API_BIBLE_TOKEN = os.getenv("API_BIBLE_TOKEN")  # <-- WYMAGANY
WLC_BIBLE_ID = "0b262f1ed7f084a6-01"  # The Hebrew Bible, Westminster Leningrad Codex

# === biblia.info.pl (PL przekłady) ===
BIBLIA_INFO_BASE = os.getenv("BIBLIA_INFO_BASE", "https://www.biblia.info.pl/api")
BIBLIA_ORIGIN = re.sub(r"/api/?$", "", BIBLIA_INFO_BASE)

# ---- PRZEKŁADY biblia.info.pl ----
BIBLIA_INFO_CODES = {
    "bw": "bw",
    "bg": "bg",
    "ubg": "ubg",
    "bt": "bt",
    "bp": "bp",
    "bz": "bz",
    "np": "np",
    "pd": "pd",
    "npw": "npw",
    "eib": "eib",
    "snp": "snp",
    "tor": "tor",
    "wb": "wb",
    "nb": "ubg",  # alias
}

TRANSLATION_NAMES = {
    "bw":  "Biblia Warszawska",
    "bg":  "Biblia Gdańska",
    "ubg": "Uwspółcześniona Biblia Gdańska",
    "bt":  "Biblia Tysiąclecia",
    "bp":  "Biblia Poznańska",
    "bz":  "Biblia Zaremby",
    "np":  "Nowy Przekład",
    "pd":  "Biblia Paulistów",
    "npw": "Nowy Przekład Współczesny",
    "eib": "EIB",
    "snp": "Przekład Literacki (SNP)",
    "tor": "Torah (PL)",
    "wb":  "Warszawsko-Praska",
    "nb":  "Uwspółcześniona Biblia Gdańska",
}

# ---- Mapowanie USFM → polskie skróty (dla nagłówka) ----
USFM_TO_PL = {
    # Pięcioksiąg
    "GEN": "Rdz", "EXO": "Wj", "LEV": "Kpł", "NUM": "Lb", "DEU": "Pwt",
    # Historyczne
    "JOS": "Joz", "JDG": "Sdz", "RUT": "Rut",
    "1SA": "1Sm", "2SA": "2Sm",
    "1KI": "1Krl", "2KI": "2Krl",
    "1CH": "1Krn", "2CH": "2Krn",
    "EZR": "Ezd", "NEH": "Neh", "EST": "Est",
    # Mądrościowe
    "JOB": "Hi", "PSA": "Ps", "PRO": "Prz", "ECC": "Koh", "SNG": "Pnp",
    # Prorocy więksi
    "ISA": "Iz", "JER": "Jer", "LAM": "Lm", "EZK": "Ez", "DAN": "Dn",
    # Prorocy mniejsi
    "HOS": "Oz", "JOL": "Jl", "AMO": "Am", "OBA": "Ab", "JON": "Jon",
    "MIC": "Mi", "NAM": "Na", "HAB": "Ha", "ZEP": "So",
    "HAG": "Ag", "ZEC": "Za", "MAL": "Ml",
}

# ---------- cache ----------
_cache: dict[str, dict] = {}
CACHE_TTL = 300

def cache_get(k: str):
    v = _cache.get(k)
    if not v:
        return None
    if time.time() - v["t"] > CACHE_TTL:
        _cache.pop(k, None)
        return None
    return v["d"]

def cache_set(k: str, d):
    _cache[k] = {"t": time.time(), "d": d}

# ---------- narzędzia HTML ----------
def _strip_tags(html: str) -> str:
    s = re.sub(r"(?is)<style.*?>.*?</style>", "", html)
    s = re.sub(r"(?is)<script.*?>.*?</script>", "", s)
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = re.sub(r"(?is)<[^>]+>", "", s)
    s = re.sub(r"\r?\n[ \t]*\r?\n+", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    return html_lib.unescape(s).strip()

# ---------- HTTP ----------
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
]
BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def _api_bible_headers():
    if not API_BIBLE_TOKEN:
        raise SystemExit("Brak API_BIBLE_TOKEN w środowisku")
    h = dict(BASE_HEADERS)
    h["User-Agent"] = random.choice(_UAS)
    h["api-key"] = API_BIBLE_TOKEN
    return h

async def http_get_json(url: str, headers: dict | None = None, timeout: int = 25):
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(headers=headers or BASE_HEADERS) as s:
                async with s.get(url, timeout=timeout) as r:
                    txt = await r.text()
                    if r.status == 200:
                        try:
                            import json
                            return r.status, json.loads(txt)
                        except Exception:
                            return r.status, None
                    if r.status in (429, 500, 502, 503, 504):
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    return r.status, None
        except Exception:
            await asyncio.sleep(0.6 * (attempt + 1))
    return 503, None

async def http_get_text(url: str, timeout: int = 20):
    for attempt in range(3):
        headers = dict(BASE_HEADERS)
        headers["User-Agent"] = random.choice(_UAS)
        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(url, timeout=timeout) as r:
                    text = await r.text()
                    if r.status == 200:
                        return r.status, text
                    if r.status in (403, 503):
                        await asyncio.sleep(0.7 * (attempt + 1))
                        continue
                    return r.status, text
        except Exception:
            await asyncio.sleep(0.7 * (attempt + 1))
    return 403, "<blocked>"

# ---------- biblia.info.pl – pojedynczy werset ----------
REF_RE = re.compile(r"^\s*([^\d]+)\s+(\d+):(\d+(?:-\d+)?)\s*$", re.IGNORECASE)

def parse_ref(ref: str):
    m = REF_RE.match(ref)
    if not m:
        return None
    book_raw, ch, vs = m.groups()
    return book_raw.strip(), ch, vs

DIV_VERSE_RE = re.compile(r'(?is)<div[^>]*class="verse-text"[^>]*>(.*?)</div>')
SPAN_NUM_RE = re.compile(r'(?is)<span[^>]*class="verse-number"[^>]*>(\d+)</span>')

def biblia_html_to_text(full_html: str) -> str:
    blocks = DIV_VERSE_RE.findall(full_html)
    lines = []
    if blocks:
        for b in blocks:
            num = SPAN_NUM_RE.search(b)
            prefix = f"{num.group(1)}. " if num else ""
            txt = _strip_tags(b).strip()
            if txt:
                if prefix and not txt.startswith(prefix):
                    txt = prefix + txt
                lines.append(txt)
        return "\n".join(lines).strip()
    return _strip_tags(full_html)

async def biblia_info_get_passage(trans: str, ref: str) -> str:
    if trans not in BIBLIA_INFO_CODES:
        raise ValueError(f"Nieznany przekład: {trans}")
    parsed = parse_ref(ref)
    if not parsed:
        raise ValueError("Nieprawidłowa referencja (np. 'Rdz 1:1').")
    book_pl, ch, vs = parsed
    cache_key = f"biblia_info|{trans}|{book_pl}|{ch}|{vs}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    # Spróbuj kilku wariantów slugu PL (część nazw ma znaki PL)
    slug_try = [
        # typowe polskie skróty już są prawidłowe jako slug w biblia.info.pl
        book_pl.lower().replace("ł", "l").replace("ś", "s").replace("ż", "z").replace("ź","z"),
        book_pl.lower()
    ]
    last_status, last_snippet = None, ""
    for slug in slug_try:
        url = f"{BIBLIA_INFO_BASE}/werset/{BIBLIA_INFO_CODES[trans]}/{slug}/{ch}/{vs}"
        status, html = await http_get_text(url)
        last_status, last_snippet = status, (html or "")[:120].replace("\n", " ")
        if status == 200 and html.strip():
            text = biblia_html_to_text(html)
            if text:
                cache_set(cache_key, text)
                return text
    raise RuntimeError(f"Błąd API PL ({last_status}). Odpowiedź: {last_snippet!r}")

# ---------- api.bible – wyszukiwanie i pobranie wersetów (HE) ----------
def _split_for_embeds(title: str, footer: str, lines: list[str], limit: int = 4000):
    chunks = []
    buf = ""
    for line in lines:
        add = (line.strip() + "\n\n")
        if len(buf) + len(add) > limit and buf:
            chunks.append({"title": title, "description": buf.rstrip(), "footer": footer})
            buf = add
        else:
            buf += add
    if buf:
        chunks.append({"title": title, "description": buf.rstrip(), "footer": footer})
    return chunks

def _parse_verse_id(verse_id: str):
    """
    Rozbija ID typu GEN.1.1 -> (GEN, 1, 1)
    """
    # czasem mogą być zakresy; tu bierzemy pierwszy
    base = verse_id.split("-")[0]
    parts = base.split(".")
    if len(parts) >= 3:
        book, ch, vs = parts[0], parts[1], parts[2]
        return book, ch, vs
    return None, None, None

def _pl_ref_from_usfm(verse_id: str) -> tuple[str, str]:
    """
    Zwraca (ref_pl, naglowek_pl): np. ("Rdz 1:1", "Rodzaju 1:1") – tu nagłówek użyje skrótu.
    """
    book, ch, vs = _parse_verse_id(verse_id)
    if not (book and ch and vs):
        return "", ""
    pl_abbr = USFM_TO_PL.get(book, book)
    ref = f"{pl_abbr} {ch}:{vs}"
    header = f"{pl_abbr} {ch}:{vs}"
    return ref, header

async def api_bible_search_hebrew(query: str, page: int = 1, per_page: int = 10):
    """
    Zwraca (hits, meta) gdzie hits = [{verseId, bookId, reference, snippet?}]
    """
    page = max(1, int(page))
    per_page = max(1, min(25, int(per_page)))
    offset = (page - 1) * per_page
    ck = f"api_bible_search|{query}|{page}|{per_page}"
    cached = cache_get(ck)
    if cached:
        return cached
    q = quote_plus(query)
    url = (f"{API_BIBLE_BASE}/bibles/{WLC_BIBLE_ID}/search?"
           f"query={q}&offset={offset}&limit={per_page}&sort=relevance")
    status, data = await http_get_json(url, headers=_api_bible_headers(), timeout=25)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"api.bible search fail: {status}")
    d = data.get("data") or {}
    total = int(d.get("total", 0))
    lim = int(d.get("limit", per_page))
    off = int(d.get("offset", offset))
    verses = d.get("verses") or []
    hits = []
    for v in verses:
        verse_id = v.get("id") or ""
        ref_txt = str(v.get("reference") or "")
        hits.append({
            "id": verse_id,
            "reference": ref_txt,
        })
    meta = {
        "page": page,
        "limit": lim,
        "offset": off,
        "total": total,
        "pages": (total + lim - 1) // lim if lim else 1
    }
    cache_set(ck, (hits, meta))
    return hits, meta

async def api_bible_get_he_text(verse_id: str) -> str:
    """
    Pobiera czysty tekst hebrajski pojedynczego wersetu (bez numerów).
    """
    ck = f"api_bible_verse|{verse_id}"
    cached = cache_get(ck)
    if cached:
        return cached
    # content-type=text usuwa HTML; include-verse-numbers=false -> sam tekst
    url = (f"{API_BIBLE_BASE}/bibles/{WLC_BIBLE_ID}/verses/{verse_id}"
           f"?content-type=text&include-verse-numbers=false&include-titles=false"
           f"&include-notes=false&include-chapter-numbers=false")
    status, data = await http_get_json(url, headers=_api_bible_headers(), timeout=25)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"api.bible verse fail: {status}")
    text = (data.get("data") or {}).get("content") or ""
    text = _strip_tags(text).strip()
    cache_set(ck, text)
    return text

# ---------- Komenda: !fh (find Hebrew) ----------
@bot.command(name="fh")
async def find_hebrew(ctx, *, arg: str):
    """
    Użycie:
      !fh <fraza_hebrajska> [strona|all]
      np. !fh ויאמר אלהים
      np. !fh ויאמר אלהים 3
      np. !fh ויאמר אלהים all
    """
    if not arg or not arg.strip():
        await ctx.reply("Użycie: `!fh <FRAZA_HEBRAJSKA> [STRONA|all]`")
        return

    parts = arg.strip().split()
    page = 1
    fetch_all = False

    if parts and parts[-1].lower() in ("all", "wsz", "wszystko"):
        fetch_all = True
        parts = parts[:-1]
    elif parts and parts[-1].isdigit():
        page = max(1, int(parts[-1]))
        parts = parts[:-1]

    phrase = " ".join(parts).strip()
    if not phrase:
        await ctx.reply("Podaj frazę do wyszukania, np. `!fh ויאמר אלהים`")
        return

    PER_PAGE = 10         # wyniki na stronę (bezpiecznie, bo dla każdego ściągamy 3 wersje tekstu)
    MAX_ALL = 200         # twardy limit dla "all", by nie floodować kanału

    try:
        if fetch_all:
            cur = 1
            all_hits = []
            meta_final = None
            while True:
                hits, meta = await api_bible_search_hebrew(phrase, page=cur, per_page=PER_PAGE)
                all_hits.extend(hits)
                meta_final = meta
                if not hits or (len(all_hits) >= MAX_ALL) or cur >= meta.get("pages", 1):
                    break
                cur += 1
            hits = all_hits[:MAX_ALL]
            meta = meta_final or {"total": len(hits), "page": 1, "pages": 1, "limit": PER_PAGE}
        else:
            hits, meta = await api_bible_search_hebrew(phrase, page=page, per_page=PER_PAGE)
    except Exception as e:
        await ctx.reply(f"❌ Problem z wyszukiwaniem: {e}")
        return

    if not hits:
        await ctx.reply("Brak wyników.")
        return

    total = meta.get("total", len(hits))
    pages = meta.get("pages", 1)
    cur_page = meta.get("page", page)

    # Zbieramy linie do embeda – dla każdego wersetu: nagłówek PL, tekst HE, BT i BW
    lines = []
    lines.append(f"Znaleziono {total} wystąpień frazy «{phrase}» w WLC (hebr.).")
    if fetch_all:
        shown = min(len(hits), MAX_ALL)
        lines.append(f"Wyświetlono {shown} wyników (pełne «all», limit bezpieczeństwa {MAX_ALL}).")
    else:
        lines.append(f"Strona {cur_page}/{pages}, {PER_PAGE} na stronę.")
    lines.append("")

    # Pobieramy treści równolegle (hebrajski + 2xPL)
    async def build_block(v):
        verse_id = v["id"]
        he_text = await api_bible_get_he_text(verse_id)
        ref_pl, header_pl = _pl_ref_from_usfm(verse_id)
        # jeśli nie udało się zmapować – użyj referencji z api.bible
        if not header_pl:
            header_pl = _strip_tags(v.get("reference") or verse_id)

        bt_txt = ""
        bw_txt = ""
        if ref_pl:
            try:
                bt_txt = await biblia_info_get_passage("bt", ref_pl)
            except Exception:
                bt_txt = "(brak odpowiedzi BT)"
            try:
                bw_txt = await biblia_info_get_passage("bw", ref_pl)
            except Exception:
                bw_txt = "(brak odpowiedzi BW)"

        block = []
        block.append(f"**{header_pl}**")           # nagłówek PL (księga/rozdział/wers)
        block.append(he_text if he_text else "(brak tekstu HE)")
        if bt_txt:
            block.append(f"*BT:* {bt_txt}")
        if bw_txt:
            block.append(f"*BW:* {bw_txt}")
        return "\n".join(block).strip()

    # jeżeli to "all" – nie rób 200*3 requestów naraz; batching
    results = []
    BATCH = 10
    for i in range(0, len(hits), BATCH):
        chunk = hits[i:i+BATCH]
        blocks = await asyncio.gather(*(build_block(v) for v in chunk))
        results.extend(blocks)

    lines.extend(results)

    title = f"Wyszukiwanie (HE): «{phrase}» — WLC"
    footer = "Źródła: api.bible (WLC) + biblia.info.pl (BT, BW)"
    chunks = _split_for_embeds(title, footer, lines, limit=4000)

    first = True
    for ch in chunks:
        embed = discord.Embed(title=ch["title"], description=ch["description"])
        if first:
            # link informacyjny (do api.bible docs)
            embed.url = "https://docs.api.bible/guides/bibles"
            first = False
        embed.set_footer(text=ch["footer"])
        await ctx.reply(embed=embed)

# ---------- proste util-komendy ----------
@bot.command()
async def ping(ctx):
    await ctx.reply("pong")

@bot.command()
async def diag(ctx):
    """Pokaż uprawnienia bota na bieżącym kanale."""
    me = ctx.guild.me
    perms = ctx.channel.permissions_for(me)
    report = (
        f"view_channel={perms.view_channel}\n"
        f"send_messages={perms.send_messages}\n"
        f"embed_links={perms.embed_links}\n"
        f"read_message_history={perms.read_message_history}"
    )
    await ctx.reply(f"```{report}```")

@bot.command(name="komendy")
async def komendy(ctx):
    names = [c.name for c in bot.commands]
    await ctx.reply(", ".join(sorted(names)))

# ---------- eventy ----------
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    try:
        await ctx.reply(f"❌ {error}")
    except Exception:
        pass

@bot.event
async def on_ready():
    print(f"✅ Bot zalogowany jako {bot.user} (id={bot.user.id})", flush=True)
    print("➡️ Serwery:", [g.name for g in bot.guilds], flush=True)

# ---------- start ----------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Brak DISCORD_BOT_TOKEN w środowisku")
if not API_BIBLE_TOKEN:
    raise SystemExit("Brak API_BIBLE_TOKEN w środowisku (api.bible)")
bot.run(TOKEN)

