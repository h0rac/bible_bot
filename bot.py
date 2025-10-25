import os
import re
import time
import html as html_lib
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

# Pozwalamy nadpisać bazowy URL z ENV (np. gdy użyjesz własnego proxy/CF Worker)
BIBLIA_INFO_BASE = os.getenv("BIBLIA_INFO_BASE", "https://www.biblia.info.pl/api")
# Domena bez /api – do budowania linków do strony wyników
BIBLIA_ORIGIN = re.sub(r"/api/?$", "", BIBLIA_INFO_BASE)

# ---- PRZEKŁADY ----
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

# ---- SKRÓTY KSIĄG (ST + NT) -> slug w API ----
PRIMARY_BOOK_SLUG = {
    # NT
    "Mt": "mat", "Mk": "mar", "Łk": "luk", "Lk": "luk", "Łuk": "luk",
    "J": "jan", "Jan": "jan",
    "Dz": "dz",
    "Rz": "rz",
    "1Kor": "1kor", "I Kor": "1kor", "1 Kor": "1kor",
    "2Kor": "2kor", "II Kor": "2kor", "2 Kor": "2kor",
    "Gal": "gal", "Ef": "ef", "Flp": "flp", "Kol": "kol",
    "1Tes": "1tes", "2Tes": "2tes",
    "1Tm": "1tm", "2Tm": "2tm",
    "Tt": "tt", "Flm": "flm",
    "Hbr": "hbr", "Jak": "jak",
    "1P": "1p", "2P": "2p",
    "1J": "1j", "2J": "2j", "3J": "3j",
    "Jud": "jud",
    "Obj": "obj", "Ap": "obj",

    # ST
    "Rdz": "rdz", "Wj": "wj", "Kpł": "kpl", "Kpl": "kpl", "Lb": "lb", "Pwt": "pwt",
    "Joz": "joz", "Sdz": "sdz", "Rut": "rut",
    "1Sm": "1sm", "2Sm": "2sm",
    "1Krl": "1krl", "2Krl": "2krl",
    "1Krn": "1krn", "2Krn": "2krn",
    "Ezd": "ezd", "Neh": "neh", "Est": "est",
    "Hi": "hi", "Jb": "hi",
    "Ps": "ps", "Prz": "prz", "Koh": "koh", "Pnp": "pnp",
    "Iz": "iz", "Jer": "jer", "Lm": "lm", "Ez": "ez", "Dn": "dn",
    "Oz": "oz", "Jl": "jl", "Am": "am", "Ab": "ab", "Jon": "jon",
    "Mi": "mi", "Na": "na", "Ha": "ha", "So": "so",
    "Ag": "ag", "Za": "za", "Ml": "ml",
}

# Case-insensitive aliasy nazw ksiąg
BOOK_ALIASES = {k.lower(): v for k, v in PRIMARY_BOOK_SLUG.items()}
BOOK_ALIASES.update({
    "mt": "mat", "mateusz": "mat",
    "mk": "mar", "marka": "mar",
    "lk": "luk", "łk": "luk", "łuk": "luk",
    "j": "jan", "jan": "jan",
    "ap": "obj", "apo": "obj", "apokalipsa": "obj",
})

# Warianty slugów (jeśli podstawowy da 404 – spróbujemy po kolei)
BOOK_SLUG_VARIANTS = {
    "jan": ["jan", "ewjan", "joan"],
    "mat": ["mat", "ewmat"],
    "mar": ["mar", "ewmar"],
    "luk": ["luk", "ewluk"],
    "obj": ["obj", "apokal", "ap"],
}

REF_RE = re.compile(r"^\s*([^\d]+)\s+(\d+):(\d+(?:-\d+)?)\s*$", re.IGNORECASE)

# Prosty cache odpowiedzi (na 5 min)
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

def parse_ref(ref: str):
    """'J 3:16' -> ('jan', '3', '16') – case-insensitive, z aliasami."""
    m = REF_RE.match(ref)
    if not m:
        return None
    book_raw, ch, vs = m.groups()
    key = book_raw.strip().lower()
    primary = BOOK_ALIASES.get(key, key).lower()
    return primary, ch, vs

# ---------- ekstrakcja czystego tekstu z HTML biblia.info.pl ----------
DIV_VERSE_RE = re.compile(r'(?is)<div[^>]*class="verse-text"[^>]*>(.*?)</div>')
SPAN_NUM_RE = re.compile(r'(?is)<span[^>]*class="verse-number"[^>]*>(\d+)</span>')

def _strip_tags(html: str) -> str:
    s = re.sub(r"(?is)<style.*?>.*?</style>", "", html)
    s = re.sub(r"(?is)<script.*?>.*?</script>", "", s)
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = re.sub(r"(?is)<[^>]+>", "", s)
    s = re.sub(r"\r?\n[ \t]*\r?\n+", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    return html_lib.unescape(s).strip()

def biblia_html_to_text(full_html: str) -> str:
    """Z <div class="verse-text">...> wyciąga tekst + numer wersu; fallback: globalny strip."""
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

# ---------- HTTP (z nagłówkami i retry – pomaga na Cloudflare) ----------
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
]

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.7,en;q=0.6",
    "Referer": "https://www.biblia.info.pl/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

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

# ---------- POBRANIE WERSETU ----------
async def biblia_info_get_passage(trans: str, ref: str) -> str:
    """Pobiera werset/y z biblia.info.pl, zwraca czysty tekst."""
    if trans not in BIBLIA_INFO_CODES:
        raise ValueError(f"Nieznany przekład: {trans}")

    parsed = parse_ref(ref)
    if not parsed:
        raise ValueError("Nieprawidłowa referencja (użyj np. 'J 3:16' lub 'Obj 21:3-5').")

    primary_slug, ch, vs = parsed
    candidates = BOOK_SLUG_VARIANTS.get(primary_slug, [primary_slug])

    cache_key = f"biblia_info|{trans}|{primary_slug}|{ch}|{vs}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    last_status = None
    last_snippet = ""
    for slug in candidates:
        url = f"{BIBLIA_INFO_BASE}/werset/{BIBLIA_INFO_CODES[trans]}/{slug}/{ch}/{vs}"
        status, html = await http_get_text(url)
        last_status, last_snippet = status, (html or "")[:120].replace("\n", " ")
        if status == 200 and html.strip():
            text = biblia_html_to_text(html)
            if text:
                cache_set(cache_key, text)
                return text

    raise RuntimeError(f"Błąd API ({last_status}). Odpowiedź: {last_snippet!r}")

# ---------- WYSZUKIWANIE ----------
def _cache_key_search_api(trans: str, phrase: str, limit: int, page: int) -> str:
    return f"searchapi|{trans}|{phrase.strip().lower()}|{limit}|{page}"

def _highlight(hay: str, needle: str) -> str:
    if not hay or not needle:
        return hay
    words = [w for w in re.split(r"\s+", needle.strip()) if w]
    def repl(m): return f"**{m.group(0)}**"
    for w in sorted(words, key=len, reverse=True):
        try:
            hay = re.sub(re.escape(w), repl, hay, flags=re.IGNORECASE)
        except re.error:
            pass
    return hay

# --- ekstra: rozpakowanie „pseudo-JSON” z tekstami wersetów ---
_TEXT_KEY_RE = re.compile(r"""(?is)(["']text["']\s*:\s*["'])(.+?)(["'])""")

def _coerce_text_block(raw):
    """
    Zwraca czysty tekst:
    - list/dict -> zlepione "nr. tekst"
    - string będący reprezentacją listy/dict -> wyciągnięte wszystkie 'text'
    - zwykły string -> jak jest
    """
    if isinstance(raw, list):
        parts = []
        for it in raw:
            if isinstance(it, dict):
                vno = str(it.get("verse") or "")
                vtx = str(it.get("text") or "")
                if vtx:
                    parts.append(f"{vno}. {vtx}" if vno else vtx)
            elif isinstance(it, str):
                parts.append(it)
        return " ".join(parts)

    if isinstance(raw, dict):
        vno = str(raw.get("verse") or "")
        vtx = str(raw.get("text") or "")
        return (f"{vno}. {vtx}" if vno and vtx else vtx).strip()

    if isinstance(raw, str) and "[" in raw and "text" in raw and ("{" in raw or "}" in raw):
        texts = [m.group(2) for m in _TEXT_KEY_RE.finditer(raw)]
        if texts:
            return " ".join(texts)

    return "" if raw is None else str(raw)

async def biblia_info_search_phrase_api(trans: str, phrase: str, limit: int = 5, page: int = 1):
    """
    Zwraca: (results, search_page_url)
    results = [{ "ref": "J 3:16", "snippet": "<PEŁNY TEKST>" }, ...]
    """
    if trans not in BIBLIA_INFO_CODES:
        raise ValueError(f"Nieznany przekład: {trans}")

    phrase = phrase.strip()
    if not phrase:
        raise ValueError("Podaj frazę do wyszukania.")

    page = max(1, int(page))
    limit = max(1, min(10, int(limit)))

    ck = _cache_key_search_api(trans, phrase, limit, page)
    cached = cache_get(ck)
    if cached:
        return cached

    code = BIBLIA_INFO_CODES[trans]
    q_path = quote(phrase, safe="")
    API_BASE = BIBLIA_INFO_BASE
    ORIGIN = BIBLIA_ORIGIN
    search_page_url = f"{ORIGIN}/szukaj.php?st={quote_plus(phrase)}&tl={code}&p={page}"

    urls = [
        f"{API_BASE}/search/{code}/{q_path}?page={page}&limit={limit}",
        f"{API_BASE}/szukaj/{code}/{q_path}?page={page}&limit={limit}",
    ]

    last_status, last_body = None, ""
    import json

    def _longest_string_record(rec: dict) -> str:
        ban = {"book", "chapter", "rozdzial", "verse", "verses", "werset", "wersety", "range"}
        cand = [str(v) for k, v in rec.items() if k not in ban and isinstance(v, str)]
        return max(cand, key=len).strip() if cand else ""

    for url in urls:
        status, body = await http_get_text(url, timeout=20)
        last_status, last_body = status, (body or "")[:800].replace("\n", " ")
        if status != 200 or not body:
            continue

        try:
            data = json.loads(body)
        except Exception:
            continue

        out = []

        # --- GŁÓWNY FORMAT: dict z kluczem "results" ---
        seq = []
        if isinstance(data, dict):
            if isinstance(data.get("results"), list):
                seq = data["results"]
            elif isinstance(data.get("hits"), list):
                seq = data["hits"]
            elif isinstance(data.get("data"), list):
                seq = data["data"]
            elif isinstance(data.get("items"), list):
                seq = data["items"]
        elif isinstance(data, list):
            seq = data

        for r in seq:
            if not isinstance(r, dict):
                continue

            book = r.get("book") or {}
            b_disp = (
                book.get("abbreviation") or book.get("abbr") or book.get("short")
                or book.get("short_name") or book.get("name") or ""
            ).strip()

            chapter = str(r.get("chapter") or r.get("rozdzial") or "").strip()
            verse = (
                r.get("verse") or r.get("verses") or r.get("werset")
                or r.get("wersety") or r.get("range") or ""
            )
            verse = str(verse).strip().replace(",", ":")

            raw_text = (
                r.get("text") or r.get("content") or r.get("snippet") or
                r.get("fragment") or r.get("tekst") or r.get("tresc") or r.get("html") or ""
            )

            txt = _coerce_text_block(raw_text)
            if not txt:
                txt = _longest_string_record(r)

            txt = _strip_tags(txt).strip()
            ref = f"{(b_disp or '').upper()} {chapter}:{verse}" if b_disp and chapter and verse else ""

            if ref and txt:
                out.append({"ref": ref, "snippet": txt})

        if out:
            for h in out:
                h["snippet"] = _highlight(h["snippet"], phrase)
            cache_set(ck, (out, search_page_url))
            return out, search_page_url

    raise RuntimeError(f"Brak wyników lub nierozpoznany format API (status {last_status}). Body (800B): {last_body}")

def _split_for_embeds(title: str, footer: str, lines: list[str], limit: int = 4000):
    """Dzieli listę linii na porcje <= limit dla Discord embed.description."""
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

# --------------- komendy ---------------

@bot.command(name="werset")
async def werset(ctx, *, arg: str):
    """
    Użycie:
      !werset J 3:16 bw
      !werset Rdz 1:1 bg
      !werset Obj 21:3-5 bt
    """
    parts = arg.rsplit(" ", 1)
    if len(parts) != 2:
        await ctx.reply("Użycie: `!werset <KSIĘGA> <ROZDZIAŁ:WERS[-WERS]> <PRZEKŁAD>`\nnp. `!werset J 3:16 bw`")
        return

    ref, trans = parts[0].strip(), parts[1].strip().lower()
    try:
        txt = await biblia_info_get_passage(trans, ref)
    except Exception as e:
        await ctx.reply(f"❌ {e}")
        return

    embed = discord.Embed(title=f"{ref} — {trans.upper()}", description=txt[:4000])
    embed.set_footer(text="Źródło: biblia.info.pl")
    await ctx.reply(embed=embed)

@bot.command(name="fraza")
async def fraza(ctx, *, arg: str):
    """
    Wyszukaj frazę przez oficjalne API.
    Użycie:
      !fraza <fraza>
      !fraza <fraza> <kod_przekładu>
      !fraza <fraza> <kod_przekładu> <strona>
    """
    if not arg or not arg.strip():
        await ctx.reply("Użycie: `!fraza <FRAZA> [PRZEKŁAD] [STRONA]`")
        return

    parts = arg.strip().split()
    page = 1
    trans = "bw"

    if parts[-1].isdigit():
        page = max(1, int(parts[-1]))
        parts = parts[:-1]

    if parts and parts[-1].lower() in BIBLIA_INFO_CODES:
        trans = parts[-1].lower()
        parts = parts[:-1]

    phrase = " ".join(parts).strip()
    if not phrase:
        await ctx.reply("Podaj frazę do wyszukania, np. `!fraza tak bowiem Bóg umiłował świat`")
        return

    try:
        hits, search_url = await biblia_info_search_phrase_api(trans, phrase, limit=10, page=page)
    except Exception as e:
        await ctx.reply("Brak wyników albo problem z wyszukiwarką. Spróbuj inne parametry lub za chwilę.")
        print(f"[fraza] error: {type(e).__name__}: {e}", flush=True)
        return

    if not hits:
        await ctx.reply("Brak wyników.")
        return

    # Buduj PEŁNE linie (bez skracania); Discord limitujemy przez dzielenie embedów
    lines = []
    for h in hits:
        ref = h.get("ref", "—")
        snip = (h.get("snippet") or "").strip()
        lines.append(f"**{ref}** — {snip}")

    title = f"Wyniki („{phrase}”) — {trans.upper()} — strona {page}"
    footer = "Źródło: biblia.info.pl (API search)"
    chunks = _split_for_embeds(title, footer, lines, limit=4000)

    first = True
    for ch in chunks:
        embed = discord.Embed(title=ch["title"], description=ch["description"])
        if first:
            embed.url = search_url
            first = False
        embed.set_footer(text=ch["footer"])
        await ctx.reply(embed=embed)

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

# --------------- eventy / logi ---------------
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

# --------------- start ---------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Brak DISCORD_BOT_TOKEN w środowisku")
bot.run(TOKEN)

