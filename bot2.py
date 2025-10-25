import os
import re
import time
import html as html_lib
import aiohttp
import asyncio
import random
from urllib.parse import quote_plus

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

# Pozwalamy nadpisać bazowy URL z ENV (np. gdy użyjesz własnego proxy)
BIBLIA_INFO_BASE = os.getenv("BIBLIA_INFO_BASE", "https://www.biblia.info.pl/api")

# ---- PRZEKŁADY ----
BIBLIA_INFO_CODES = {
    "bw": "bw",      # Biblia Warszawska
    "bg": "bg",      # Biblia Gdańska
    "ubg": "ubg",    # Uwspółcześniona Biblia Gdańska
    "bt": "bt",      # Biblia Tysiąclecia
    "bp": "bp",      # Biblia Poznańska
    "bz": "bz",      # Biblia Zaremby
    "np": "np",      # Nowa Przymierza (Ewangeliczna)
    "pd": "pd",      # Przekład Dosłowny
    "npw": "npw",    # Nowy Przekład Warszawski
    "eib": "eib",    # Ewangeliczna Instytutu Biblijnego
    "snp": "snp",    # Słowo Nowego Przymierza
    "tor": "tor",    # Biblia Toruńska
    "wb": "wb",      # Biblia Warszawsko-Brytyjska
    "nb": "ubg",     # alias: Nowa Biblia Gdańska = UBG
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
    "lk": "luk", "łk": "luk", "łuk": "luk", "luk": "luk",
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
    # dopisuj kolejne jeśli trafisz na nietypowy slug w API
}

REF_RE = re.compile(r'^\s*([^\d]+)\s+(\d+):(\d+(?:-\d+)?)\s*$', re.IGNORECASE)

# Prosty cache odpowiedzi (na 5 min)
_cache = {}
CACHE_TTL = 300

def cache_get(k: str):
    v = _cache.get(k)
    if not v:
        return None
    if time.time() - v["t"] > CACHE_TTL:
        _cache.pop(k, None)
        return None
    return v["d"]

def cache_set(k: str, d): _cache[k] = {"t": time.time(), "d": d}

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
    s = re.sub(r'(?is)<style.*?>.*?</style>', '', html)
    s = re.sub(r'(?is)<script.*?>.*?</script>', '', s)
    s = s.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
    s = re.sub(r'(?is)<[^>]+>', '', s)
    s = re.sub(r'\r?\n[ \t]*\r?\n+', '\n', s)   # puste linie
    s = re.sub(r'[ \t]+', ' ', s)               # wielokrotne spacje
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
    """
    Pobiera werset/y z biblia.info.pl. Obsługuje warianty slugów księgi (fallback na 404)
    i zwraca czysty tekst (bez HTML/CSS).
    """
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

# ---------- SZUKAJ FRAZY (jak na biblia.info.pl) ----------
def _cache_key_search(trans: str, phrase: str, limit: int) -> str:
    return f"search|{trans}|{phrase.strip().lower()}|{limit}"

# do wyłapania referencji w tekście html
REF_INLINE_RE = re.compile(r'([A-Za-zŁŚŻŹĆŃłśżźćń\. ]{1,12}\s*\d{1,3}:\d{1,3}(?:[-–]\d{1,3})?)')

async def biblia_info_search_phrase(trans: str, phrase: str, limit: int = 5):
    """
    Szuka frazy w danym przekładzie. Zwraca listę:
      [{ "ref": "J 3:16", "snippet": "Tak bowiem Bóg..." }, ...]
    Próbuje kilku możliwych endpointów API oraz fallback do HTML.
    """
    if trans not in BIBLIA_INFO_CODES:
        raise ValueError(f"Nieznany przekład: {trans}")

    phrase = phrase.strip()
    if not phrase:
        raise ValueError("Podaj frazę do wyszukania.")

    ck = _cache_key_search(trans, phrase, limit)
    cached = cache_get(ck)
    if cached:
        return cached

    q = quote_plus(phrase)
    code = BIBLIA_INFO_CODES[trans]
    candidates = [
        (f"{BIBLIA_INFO_BASE}/szukaj/{code}?q={q}", "json"),
        (f"{BIBLIA_INFO_BASE}/search/{code}?q={q}", "json"),
        (f"{BIBLIA_INFO_BASE}/szukaj?q={q}&tlum={code}", "json"),
        (f"https://www.biblia.info.pl/szukaj.php?st={q}&tl={code}", "html"),
        (f"https://www.biblia.info.pl/szukaj.php?st={q}", "html"),
    ]

    results = []
    last_status = None
    last_snippet = ""

    for url, mode in candidates:
        status, body = await http_get_text(url, timeout=20)
        last_status, last_snippet = status, (body or "")[:160].replace("\n", " ")
        if status != 200 or not body:
            continue

        try:
            if mode == "json":
                import json
                data = json.loads(body)
                items = []

                if isinstance(data, dict) and "hits" in data and isinstance(data["hits"], list):
                    for h in data["hits"]:
                        ref = (h.get("ref") or h.get("reference") or "").strip()
                        txt = (h.get("text") or h.get("snippet") or h.get("content") or "").strip()
                        if ref and txt:
                            items.append({"ref": ref, "snippet": _strip_tags(txt)})

                elif isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    for h in data["data"]:
                        ref = (h.get("ref") or h.get("reference") or "").strip()
                        txt = (h.get("text") or h.get("snippet") or h.get("content") or "").strip()
                        if ref and txt:
                            items.append({"ref": ref, "snippet": _strip_tags(txt)})

                elif isinstance(data, list):
                    for h in data:
                        if isinstance(h, dict):
                            ref = (h.get("ref") or h.get("reference") or "").strip()
                            txt = (h.get("text") or h.get("snippet") or h.get("content") or "").strip()
                            if ref and txt:
                                items.append({"ref": ref, "snippet": _strip_tags(txt)})

                if items:
                    results = items[:limit]
                    break

            else:
                # mode == "html": spróbuj parsować HTML wyników
                text_all = html_lib.unescape(_strip_tags(body))
                # rozbij na potencjalne linie wyników i szukaj ref + okolicy frazy
                chunks = re.split(r'\n{2,}|—|-{3,}|•|\u2022', text_all)
                acc = []
                for ch in chunks:
                    mref = REF_INLINE_RE.search(ch)
                    if not mref:
                        continue
                    ref = mref.group(1).strip()
                    t = re.sub(r'\s+', ' ', ch).strip()
                    idx = t.lower().find(phrase.lower())
                    if idx != -1:
                        start = max(0, idx - 60)
                        end = min(len(t), idx + len(phrase) + 60)
                        snippet = t[start:end]
                    else:
                        snippet = t[:160]
                    snippet = snippet.strip(" .…\u2026")
                    acc.append({"ref": ref, "snippet": snippet})
                    if len(acc) >= limit:
                        break
                if acc:
                    results = acc
                    break
        except Exception:
            # spróbujemy kolejny wariant
            continue

    if not results:
        raise RuntimeError(f"Brak wyników lub błąd wyszukiwania (status {last_status}). Odpowiedź: {last_snippet!r}")

    cache_set(ck, results)
    return results

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
    Szuka frazy w danym przekładzie (domyślnie BW).
    Użycie:
      !fraza <fraza>
      !fraza <fraza> <kod_przekładu>
    Przykłady:
      !fraza tak bowiem Bóg umiłował świat
      !fraza łaska i pokój ubg
    """
    if not arg or not arg.strip():
        await ctx.reply("Użycie: `!fraza <FRAZA> [PRZEKŁAD]` np. `!fraza tak bowiem Bóg umiłował świat ubg`")
        return

    parts = arg.strip().split()
    last = parts[-1].lower()
    if last in BIBLIA_INFO_CODES:
        trans = last
        phrase = " ".join(parts[:-1]).strip()
    else:
        trans = "bw"
        phrase = " ".join(parts).strip()

    if not phrase:
        await ctx.reply("Podaj frazę do wyszukania, np. `!fraza tak bowiem Bóg umiłował świat`")
        return

    try:
        hits = await biblia_info_search_phrase(trans, phrase, limit=5)
    except Exception as e:
        await ctx.reply(f"❌ Błąd wyszukiwania: {e}")
        return

    if not hits:
        await ctx.reply("Brak wyników.")
        return

    lines = []
    for h in hits:
        ref = h.get("ref", "—")
        snip = (h.get("snippet") or "").strip()
        if len(snip) > 180:
            snip = snip[:177] + "…"
        lines.append(f"**{ref}** — {snip}")

    desc = "\n\n".join(lines)
    embed = discord.Embed(
        title=f"Wyniki dla: „{phrase}” — {trans.upper()}",
        description=desc[:4000]
    )
    embed.set_footer(text="Źródło: biblia.info.pl (wyszukiwarka)")
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

# --------------- eventy / logi ---------------
@bot.event
async def on_message(message: discord.Message):
    # podgląd w logach (opcjonalnie)
    # print(f"[MSG] g={getattr(message.guild,'name',None)} ch={getattr(message.channel,'name',None)} by={message.author} content={message.content!r}", flush=True)
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

