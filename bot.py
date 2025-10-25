import os
import re
import time
import html as html_lib
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# --------------- konfiguracja ---------------
load_dotenv()
BOT_PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True   # kluczowe, by bot widział treść komend
INTENTS.guilds = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=INTENTS)

BIBLIA_INFO_BASE = "https://www.biblia.info.pl/api"

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
    "Hi": "hi", "Jb": "hi",   # alternatywa dla Hioba
    "Ps": "ps", "Prz": "prz", "Koh": "koh", "Pnp": "pnp",
    "Iz": "iz", "Jer": "jer", "Lm": "lm", "Ez": "ez", "Dn": "dn",
    "Oz": "oz", "Jl": "jl", "Am": "am", "Ab": "ab", "Jon": "jon",
    "Mi": "mi", "Na": "na", "Ha": "ha", "So": "so",
    "Ag": "ag", "Za": "za", "Ml": "ml",
}

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
_cache: dict[str, dict] = {}
CACHE_TTL = 300

def cache_get(k: str):
    v = _cache.get(k)
    if not v: return None
    if time.time() - v["t"] > CACHE_TTL:
        _cache.pop(k, None); return None
    return v["d"]

def cache_set(k: str, d): _cache[k] = {"t": time.time(), "d": d}

def parse_ref(ref: str):
    """'J 3:16' -> ('jan', '3', '16') na bazie skrótów; None gdy format błędny."""
    m = REF_RE.match(ref)
    if not m: return None
    book_raw, ch, vs = m.groups()
    key = book_raw.strip()
    primary = PRIMARY_BOOK_SLUG.get(key, key).lower()
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

# ---------- HTTP ----------
async def http_get_text(url: str, timeout: int = 15):
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=timeout) as r:
            return r.status, await r.text()

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
    if cached: return cached

    last_status = None
    last_snippet = ""
    for slug in candidates:
        url = f"{BIBLIA_INFO_BASE}/werset/{BIBLIA_INFO_CODES[trans]}/{slug}/{ch}/{vs}"
        print(f"[REQ] {url}")
        status, html = await http_get_text(url)
        last_status, last_snippet = status, html[:120].replace("\n", " ")
        if status == 200 and html.strip():
            text = biblia_html_to_text(html)
            if text:
                cache_set(cache_key, text)
                return text

    raise RuntimeError(f"Błąd API ({last_status}). Odpowiedź: {last_snippet!r}")

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
        print(f"[CMD] werset ref={ref!r} trans={trans!r}")
        txt = await biblia_info_get_passage(trans, ref)
    except Exception as e:
        print("❗ werset ERROR:", repr(e))
        await ctx.reply(f"❌ {e}")
        return

    embed = discord.Embed(title=f"{ref} — {trans.upper()}", description=txt[:4000])
    embed.set_footer(text="Źródło: biblia.info.pl")
    await ctx.reply(embed=embed)

@bot.command()
async def ping(ctx):
    print("ping: dotarła komenda z kanału", ctx.channel.id)
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
    print("diag perms:\n" + report)
    await ctx.reply(f"```{report}```")

# --------------- eventy / logi ---------------
@bot.event
async def on_message(message: discord.Message):
    print(f"[MSG] g={getattr(message.guild,'name',None)} ch={getattr(message.channel,'name',None)} by={message.author} content={message.content!r}")
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    print("❗ on_command_error:", repr(error))
    try:
        await ctx.reply(f"❌ {error}")
    except Exception:
        pass

@bot.event
async def on_ready():
    print(f"✅ Bot zalogowany jako {bot.user} (id={bot.user.id})")
    print("➡️ Serwery:", [g.name for g in bot.guilds])

# --------------- start ---------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Brak DISCORD_BOT_TOKEN w .env")
bot.run(TOKEN)

