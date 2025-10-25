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
# Na lokalnym dev wczyta .env, w chmurze (Railway/Render) zmienne są już w środowisku.
if os.path.exists(".env"):
    from dotenv import load_dotenv
    load_dotenv()

BOT_PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True   # kluczowe, by bot widział treść komend
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

# ---------- WYSZUKIWANIE (oficjalne API: /api/search, fallback /api/szukaj) ----------
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
async def biblia_info_search_phrase_api(trans: str, phrase: str, limit: int = 5, page: int = 1):
    """
    Wyszukiwanie frazy przez oficjalne API biblia.info.pl (i fallback /szukaj).
    Zwraca: (results, search_page_url)
    results = [{ "ref": "J 3:16", "snippet": "Tak bowiem Bóg..." }, ...]
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

    candidates = [
        f"{API_BASE}/search/{code}/{q_path}?page={page}&limit={limit}",
        f"{API_BASE}/szukaj/{code}/{q_path}?page={page}&limit={limit}",
    ]

    last_status, last_body = None, ""
    for url in candidates:
        status, body = await http_get_text(url, timeout=20)
        last_status, last_body = status, (body or "")[:200].replace("\n", " ")
        if status != 200 or not body:
            continue

        try:
            import json
            data = json.loads(body)

            # 1) „Oficjalny” format: {"type":"Wyniki wyszukiwania", ... "results":[ {...} ]}
            if isinstance(data, dict) and data.get("type", "").lower().startswith("wyniki"):
                seq = data.get("results") or []
                out = []
                for r in seq:
                    if not isinstance(r, dict):
                        continue

                    # Księga: krótsza forma (abbr/short) lub pełna nazwa
                    book_block = r.get("book") or {}
                    b_short = (book_block.get("abbr") or book_block.get("short") or
                               book_block.get("short_name") or "").strip()
                    b_name = (book_block.get("name") or book_block.get("nazwa") or "").strip()
                    b_disp = b_short or b_name or ""

                    # Rozdział / wers(y)
                    chapter = str(r.get("chapter") or r.get("rozdzial") or "").strip()
                    verse = (r.get("verse") or r.get("werset") or
                             r.get("verses") or r.get("wersety") or "")
                    verse = str(verse).strip()

                    # Tekst / fragment
                    txt = (
                        r.get("snippet") or r.get("text") or r.get("content") or
                        r.get("fragment") or r.get("tekst") or r.get("tresc") or ""
                    )
                    txt = _strip_tags(str(txt))

                    # Złóż referencję, np. "J 3:16" albo "Rdz 1:1-3"
                    ref = ""
                    if b_disp and chapter and verse:
                        # Zamień ewentualny separator przecinek->dwukropek
                        verse_clean = verse.replace(",", ":")
                        ref = f"{b_disp} {chapter}:{verse_clean}"

                    if ref and txt:
                        out.append({"ref": ref, "snippet": txt})

                if out:
                    # Podświetlenie frazy
                    for h in out:
                        h["snippet"] = _highlight(h["snippet"], phrase)
                    cache_set(ck, (out, search_page_url))
                    return out, search_page_url

            # 2) Ogólne fallbacki: inne możliwe klucze „hits” / „data” / „results”
            seq = []
            if isinstance(data, dict):
                for key in ("hits", "data", "results", "items"):
                    if isinstance(data.get(key), list):
                        seq = data[key]
                        break
            elif isinstance(data, list):
                seq = data

            out = []
            for h in seq:
                if not isinstance(h, dict):
                    continue

                # ref z gotowego pola lub złożony z elementów
                ref = (h.get("ref") or h.get("reference") or h.get("miejsce") or h.get("title") or "").strip()
                if not ref:
                    bname = (h.get("book_short") or h.get("skrot") or h.get("book") or h.get("ksiega") or "").strip()
                    chapter = str(h.get("chapter") or h.get("rozdzial") or "").strip()
                    verse = str(h.get("verse") or h.get("werset") or h.get("verses") or h.get("wersety") or "").strip()
                    if bname and chapter and verse:
                        ref = f"{bname} {chapter}:{verse.replace(',', ':')}"

                txt = (
                    h.get("snippet") or h.get("text") or h.get("content") or
                    h.get("fragment") or h.get("tekst") or h.get("tresc") or ""
                )
                txt = _strip_tags(str(txt))

                if ref and txt:
                    out.append({"ref": ref, "snippet": txt})

            if out:
                for h in out:
                    h["snippet"] = _highlight(h["snippet"], phrase)
                cache_set(ck, (out, search_page_url))
                return out, search_page_url

        except Exception:
            # spróbuj następną ścieżkę z candidates
            continue

    raise RuntimeError(f"Brak wyników lub błąd wyszukiwania (status {last_status}). Odpowiedź: {last_body!r}")


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
        hits, search_url = await biblia_info_search_phrase_api(trans, phrase, limit=5, page=page)
    except Exception as e:
        await ctx.reply(f"❌ Błąd wyszukiwania: {e}")
        return

    if not hits:
        await ctx.reply("Brak wyników.")
        return

    lines = []
    for h in hits:
        ref = h.get("ref", "—")
        snip = _highlight((h.get("snippet") or "").strip(), phrase)
        if len(snip) > 200:
            snip = snip[:197] + "…"
        lines.append(f"**{ref}** — {snip}")

    embed = discord.Embed(
        title=f"Wyniki („{phrase}”) — {trans.upper()} — strona {page}",
        description="\n\n".join(lines)[:4000]
    )
    embed.url = search_url
    embed.set_footer(text="Źródło: biblia.info.pl (API search)")
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

