import os
import re
import time
import html as html_lib
import ast
import aiohttp
import asyncio
import random
from urllib.parse import quote_plus, quote
import ephem
import datetime
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

# === api.bible (WLC) ===
API_BIBLE_BASE = os.getenv("API_BIBLE_BASE", "https://api.scripture.api.bible/v1")
API_BIBLE_TOKEN = os.getenv("API_BIBLE_TOKEN")  # <-- wymagany
WLC_BIBLE_ID = "0b262f1ed7f084a6-01"           # The Hebrew Bible, Westminster Leningrad Codex

# === biblia.info.pl (PL przekłady) ===
BIBLIA_INFO_BASE = os.getenv("BIBLIA_INFO_BASE", "https://www.biblia.info.pl/api")
BIBLIA_ORIGIN = re.sub(r"/api/?$", "", BIBLIA_INFO_BASE)

# ---- PRZEKŁADY (PL) ----
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

# ---- USFM → polskie skróty (ST + NT) ----
USFM_TO_PL = {
    # ST
    "GEN": "Rdz","EXO": "Wj","LEV": "Kpł","NUM": "Lb","DEU": "Pwt",
    "JOS": "Joz","JDG": "Sdz","RUT": "Rut",
    "1SA":"1Sm","2SA":"2Sm","1KI":"1Krl","2KI":"2Krl",
    "1CH":"1Krn","2CH":"2Krn","EZR":"Ezd","NEH":"Neh","EST":"Est",
    "JOB":"Hi","PSA":"Ps","PRO":"Prz","ECC":"Koh","SNG":"Pnp",
    "ISA":"Iz","JER":"Jer","LAM":"Lm","EZK":"Ez","DAN":"Dn",
    "HOS":"Oz","JOL":"Jl","AMO":"Am","OBA":"Ab","JON":"Jon",
    "MIC":"Mi","NAM":"Na","HAB":"Ha","ZEP":"So","HAG":"Ag","ZEC":"Za","MAL":"Ml",
}
# NT (uzupełnienie)
USFM_TO_PL.update({
    "MAT": "Mt","MRK": "Mk","LUK": "Łk","JHN": "J",
    "ACT": "Dz",
    "ROM": "Rz","1CO": "1Kor","2CO": "2Kor","GAL": "Gal","EPH": "Ef",
    "PHP": "Flp","COL": "Kol","1TH": "1Tes","2TH": "2Tes",
    "1TI": "1Tm","2TI": "2Tm","TIT": "Tt","PHM": "Flm",
    "HEB": "Hbr","JAS": "Jk","1PE": "1P","2PE": "2P",
    "1JN": "1J","2JN": "2J","3JN": "3J","JUD": "Jud",
    "REV": "Ap",
})

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

# ---------- HTML / tekst utils ----------
def _strip_tags(html: str) -> str:
    s = re.sub(r"(?is)<style.*?>.*?</style>", "", html)
    s = re.sub(r"(?is)<script.*?>.*?</script>", "", s)
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = re.sub(r"(?is)<[^>]+>", "", s)
    s = re.sub(r"\r?\n[ \t]*\r?\n+", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    return html_lib.unescape(s).strip()

def _compact_blank_lines(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    out = []
    last_blank = False
    for ln in lines:
        blank = (ln.strip() == "")
        if blank and last_blank:
            continue
        out.append(ln)
        last_blank = blank
    return "\n".join(out).strip()

# ---------- Hebrew niqqud / highlight ----------
_HE_DIA = re.compile(r"[\u0591-\u05BD\u05BF-\u05C7]")   # ta’amim + niqqud

def has_hebrew_letters(s: str) -> bool:
    return bool(re.search(r"[\u0590-\u05FF]", s or ""))

def has_niqqud(s: str) -> bool:
    return bool(_HE_DIA.search(s or ""))

def strip_hebrew_diacritics(s: str) -> str:
    return _HE_DIA.sub("", s or "")

def _build_strip_map(hay: str):
    stripped_chars = []
    idx_map = []
    for i, ch in enumerate(hay):
        if not _HE_DIA.match(ch):
            stripped_chars.append(ch)
            idx_map.append(i)
    return "".join(stripped_chars), idx_map

def highlight_hebrew(hay: str, needle: str) -> str:
    if not hay or not needle:
        return hay
    Hs, map_idx = _build_strip_map(hay)
    Ns = strip_hebrew_diacritics(needle)
    if not Ns:
        return hay
    matches = []
    start = 0
    while True:
        i = Hs.find(Ns, start)
        if i == -1:
            break
        j = i + len(Ns) - 1
        orig_start = map_idx[i]
        orig_end = map_idx[j] + 1
        matches.append((orig_start, orig_end))
        start = j + 1
    if not matches:
        return hay
    out = []
    prev = 0
    for a, b in matches:
        if a < prev:
            continue
        out.append(hay[prev:a])
        out.append("**")
        out.append(hay[a:b])
        out.append("**")
        prev = b
    out.append(hay[prev:])
    return "".join(out)

# Prosta mapa „PL bold” (rozszerzaj wg potrzeb)
PL_HIGHLIGHT_HINTS = {
    "אלהים": ["Bóg", "Boga", "Bogu", "Bogiem"],
    "יהוה": ["PAN", "Pan"],
    "אדני": ["Pan", "Pana", "Panu", "Panem"],
    "ישראל": ["Izrael", "Izraela"],
    "ירושלים": ["Jerozolima", "Jerozolimy"],
}

def highlight_polish_like(hay: str, he_query: str) -> str:
    if not hay or not he_query:
        return hay
    tokens = he_query.split()
    pl_words = set()
    for t in tokens:
        key = strip_hebrew_diacritics(t)
        pl_words.update(PL_HIGHLIGHT_HINTS.get(key, []))
        pl_words.update(PL_HIGHLIGHT_HINTS.get(t, []))
    if not pl_words:
        return hay
    def repl(m): return f"**{m.group(0)}**"
    out = hay
    for w in sorted(pl_words, key=len, reverse=True):
        try:
            out = re.sub(rf"\b{re.escape(w)}\b", repl, out)
        except re.error:
            pass
    return out

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

# ---------- biblia.info.pl – pojedynczy werset (PL) ----------
# ✅ Poprawiony regex – obsługuje: 1Kor, 2 Tm, 3J, Ew. Jana, Mt 5:3 itd.
REF_RE = re.compile(
    r"^\s*([1-3]?\s*[A-Za-zżźćńółęąśŻŹĆĄŚĘŁÓŃ.\- ]+?)\s+(\d+)\s*:\s*(\d+(?:-\d+)?)\s*$",
    re.IGNORECASE
)

def parse_ref(ref: str):
    m = REF_RE.match(ref)
    if not m:
        return None
    book_pl, ch, vs = m.groups()
    return book_pl.strip(), ch, vs

def _strip_pl_diacritics(s: str) -> str:
    return (s or "").translate(str.maketrans("ąćęłńóśżź", "acelnoszz"))

def _slug_candidates(book_pl: str) -> list[str]:
    """
    Buduje listę sensownych wariantów sluga dla biblia.info.pl:
    - lower, usunięte ogonki,
    - warianty: ze spacją, bez spacji, z myślnikiem,
    - usuwa kropki, redukuje wielokrotne spacje,
    - zdejmuje prefiksy 'Ew.', 'Ew ', 'List do '.
    """
    base = (book_pl or "").strip().lower()
    base_nodiac = _strip_pl_diacritics(base)

    # usuń kropki i zredukuj spacje
    base_clean = re.sub(r"[.]+", "", base_nodiac)
    base_clean = re.sub(r"\s+", " ", base_clean).strip()

    variants = set()
    # bazowe
    variants.add(base_clean)
    variants.add(base_clean.replace(" ", ""))
    variants.add(base_clean.replace(" ", "-"))

    # uproszczenia popularnych nazw
    simplified = base_clean.replace("list do ", "").replace("ew ", "").replace("ew.", "").strip()
    variants.add(simplified)
    variants.add(simplified.replace(" ", ""))
    variants.add(simplified.replace(" ", "-"))

    # Psalmy – dodatkowe pewniaki
    if simplified.startswith("ps"):
        variants.update(["ps", "psalm", "psalmy"])

    # deduplikacja z zachowaniem kolejności
    return list(dict.fromkeys(v for v in variants if v))

def biblia_html_to_text(full_html: str) -> str:
    DIV_VERSE_RE = re.compile(r'(?is)<div[^>]*class="verse-text"[^>]*>(.*?)</div>')
    SPAN_NUM_RE = re.compile(r'(?is)<span[^>]*class="verse-number"[^>]*>(\d+)</span>')
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

def clean_pl_verse_text(t: str) -> str:
    t = (t or "").replace("\xa0", " ")
    lines = [ln.strip() for ln in t.splitlines()]
    DROP_PATTERNS = [
        r"^Księga\s+\w+.*$",
        r"^\(?\d+\)?[.,]?$",
        r"^\d+\s*[:.,]\s*\d+\s*,?$",
        r"^Biblia\s+(Tysiąclecia|Warszawska|Gdańska|Poznańska|Zaremby|Paulistów|EIB|SNP).*$",
        r"^Internetowa\s+Biblia\s+2000.*$",
        r"^(BT|BW|BG|UBG|BP|BZ|NP|PD|NPW|EIB|SNP|TOR|WB)\s*:.*$",
        r"^by\s+Digital\s+Gospel.*$",
        r"^©.*$",
        r"^\d{4}(?:\s*[–\-]\s*\d{4})?$",
        r"^[,.;·]+$"
    ]
    drops = [re.compile(p, re.IGNORECASE) for p in DROP_PATTERNS]
    kept = []
    for ln in lines:
        if not ln:
            continue
        if any(p.match(ln) for p in drops):
            continue
        kept.append(ln)
    out = "\n".join(kept)
    out = re.sub(r"(?m)^\s*\d+[.)]\s*", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out

async def biblia_info_get_passage(trans: str, ref: str) -> str:
    if trans not in BIBLIA_INFO_CODES:
        raise ValueError(f"Nieznany przekład: {trans}")
    parsed = parse_ref(ref)
    if not parsed:
        raise ValueError("Nieprawidłowa referencja (np. 'Rdz 1:1' lub '1 Kor 13:4').")
    book_pl, ch, vs = parsed
    cache_key = f"biblia_info|{trans}|{book_pl}|{ch}|{vs}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    last_status, last_snippet = None, ""
    for slug in _slug_candidates(book_pl):
        slug_enc = quote(slug, safe="")
        url = f"{BIBLIA_INFO_BASE}/werset/{BIBLIA_INFO_CODES[trans]}/{slug_enc}/{ch}/{vs}"
        status, html = await http_get_text(url)
        last_status, last_snippet = status, (html or "")[:120].replace("\n", " ")
        if status == 200 and (html or "").strip():
            text = biblia_html_to_text(html)
            if text:
                text = clean_pl_verse_text(text)
                cache_set(cache_key, text)
                return text
    raise RuntimeError(f"Błąd API PL ({last_status}). Odpowiedź: {last_snippet!r}")

# ---------- api.bible – search + verse (HE) ----------
def _parse_verse_id(verse_id: str):
    base = verse_id.split("-")[0]
    parts = base.split(".")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None, None, None

def _pl_ref_from_usfm(verse_id: str) -> tuple[str, str]:
    book, ch, vs = _parse_verse_id(verse_id)
    if not (book and ch and vs):
        return "", ""
    pl = USFM_TO_PL.get(book, book)
    ref = f"{pl} {ch}:{vs}"
    return ref, ref

def add_niqqud_hints_if_missing(query: str) -> str:
    NIQQUD_HINTS = {
        "אלהים": "אֱלֹהִים",
        "ויאמר": "וַיֹּאמֶר",
        "ויאמרו": "וַיֹּאמְרוּ",
        "בראשית": "בְּרֵאשִׁית",
    }
    if not has_hebrew_letters(query) or has_niqqud(query):
        return query
    parts = query.split()
    out = []
    for p in parts:
        key = strip_hebrew_diacritics(p)
        out.append(NIQQUD_HINTS.get(key, p))
    return " ".join(out)

async def api_bible_search_hebrew(query: str, page: int = 1, per_page: int = 10):
    page = max(1, int(page))
    per_page = max(1, min(25, int(per_page)))
    api_offset = page - 1

    async def _call(q: str):
        q_enc = quote_plus(q)
        url = (f"{API_BIBLE_BASE}/bibles/{WLC_BIBLE_ID}/search"
               f"?query={q_enc}&offset={api_offset}&limit={per_page}&sort=relevance")
        status, data = await http_get_json(url, headers=_api_bible_headers(), timeout=25)
        return status, data

    status, data = await _call(query)

    def _extract(data):
        d = data.get("data") or {}
        verses = d.get("verses") or []
        hits = [{"id": v.get("id") or "", "reference": str(v.get("reference") or "")} for v in verses if v]
        lim = int(d.get("limit", per_page)) or per_page
        off = int(d.get("offset", api_offset))
        total = int(d.get("total", 0))
        pages = (total + lim - 1) // lim if lim else 1
        meta = {"page": off + 1, "limit": lim, "offset": off, "total": total, "pages": max(pages, 1)}
        return hits, meta

    hits, meta = [], None
    if status == 200 and isinstance(data, dict):
        hits, meta = _extract(data)

    if (not hits) and has_hebrew_letters(query) and not has_niqqud(query):
        hinted = add_niqqud_hints_if_missing(query)
        if hinted != query:
            status2, data2 = await _call(hinted)
            if status2 == 200 and isinstance(data2, dict):
                hits, meta = _extract(data2)

    if meta is None:
        raise RuntimeError(f"api.bible search fail: {status}")
    return hits, meta

async def api_bible_get_he_text(verse_id: str, mesora: bool = False) -> str:
    key = f"api_bible_verse|{verse_id}|{'mes' if mesora else 'txt'}"
    cached = cache_get(key)
    if cached:
        return cached

    if mesora:
        url = (f"{API_BIBLE_BASE}/bibles/{WLC_BIBLE_ID}/verses/{verse_id}"
               f"?content-type=html&include-verse-numbers=false"
               f"&include-titles=false&include-notes=false&include-chapter-numbers=false")
        status, data = await http_get_json(url, headers=_api_bible_headers(), timeout=25)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"api.bible verse fail: {status}")
        html = (data.get("data") or {}).get("content") or ""
        out = (html or "").strip()
        cache_set(key, out)
        return out
    else:
        url = (f"{API_BIBLE_BASE}/bibles/{WLC_BIBLE_ID}/verses/{verse_id}"
               f"?content-type=text&include-verse-numbers=false"
               f"&include-titles=false&include-notes=false&include-chapter-numbers=false")
        status, data = await http_get_json(url, headers=_api_bible_headers(), timeout=25)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"api.bible verse fail: {status}")
        text = (data.get("data") or {}).get("content") or ""
        out = text.strip()
        cache_set(key, out)
        return out

# ---------- helper: split embeds ----------
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

# ---------- TWOJE: biblia.info.pl – wyszukiwarka (PL) ----------
_TEXT_KEY_RE = re.compile(r'(?is)(["\'“”]text["\'“”]\s*:\s*["\'“”])(.*?)(["\'“”])')

def _coerce_text_block(raw) -> str:
    if isinstance(raw, list):
        parts = []
        for it in raw:
            if isinstance(it, dict):
                vtx = str(it.get("text") or "")
                if vtx:
                    parts.append(vtx)
            elif isinstance(it, str):
                parts.append(it)
        return " ".join(parts)
    if isinstance(raw, dict):
        return str(raw.get("text") or "")
    if isinstance(raw, str) and "text" in raw and ("[" in raw or "{" in raw):
        try:
            parsed = ast.literal_eval(raw)
            return _coerce_text_block(parsed)
        except Exception:
            texts = [m.group(2) for m in _TEXT_KEY_RE.finditer(raw)]
            if texts:
                return " ".join(texts)
    return "" if raw is None else str(raw)

def _is_texty(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    return len(s) >= 5 and re.search(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]", s) is not None

def _extract_all_texts_from_any(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    parts = [m.group(2) for m in _TEXT_KEY_RE.finditer(raw)]
    return " ".join([p for p in parts if p])

def _highlight_case_insensitive(hay: str, needle: str) -> str:
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

def _cache_key_search_api(trans: str, phrase: str, limit: int, page: int) -> str:
    return f"searchapi|{trans}|{phrase.strip().lower()}|{limit}|{page}"

async def biblia_info_search_phrase_api(trans: str, phrase: str, limit: int = 5, page: int = 1):
    if trans not in BIBLIA_INFO_CODES:
        raise ValueError(f"Nieznany przekład: {trans}")

    phrase = phrase.strip()
    if not phrase:
        raise ValueError("Podaj frazę do wyszukania.")

    page = max(1, int(page))
    limit = max(1, min(25, int(limit)))

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

    import json
    last_status, last_body = None, ""
    def _longest_string_record(rec: dict) -> str:
        ban = {"book", "chapter", "rozdzial", "verse", "verses", "werset", "wersety", "range"}
        cand = [str(v) for k, v in rec.items() if k not in ban and isinstance(v, str)]
        return max(cand, key=len).strip() if cand else ""

    def _to_int(x):
        try:
            return int(str(x).strip())
        except Exception:
            return None

    out = []
    total_all = None
    range_start = None
    range_end = None

    for url in urls:
        status, body = await http_get_text(url, timeout=20)
        last_status, last_body = status, (body or "")[:1000].replace("\n", " ")
        if status != 200 or not body:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue

        if isinstance(data, dict):
            for k in ("all_results","total_results","total","hits_total","count"):
                if k in data and total_all is None:
                    total_all = _to_int(data.get(k))
            rstr = (data.get("results_range") or data.get("range") or "").strip()
            m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(rstr))
            if m:
                range_start, range_end = int(m.group(1)), int(m.group(2))

        seq = []
        if isinstance(data, dict):
            for key in ("results","hits","data","items"):
                if isinstance(data.get(key), list):
                    seq = data[key]
                    break
        elif isinstance(data, list):
            seq = data

        for r in seq:
            if not isinstance(r, dict):
                continue
            book = r.get("book") or {}
            b_disp = (
                (book.get("abbreviation") or book.get("abbr") or book.get("short")
                 or book.get("short_name") or book.get("name") or "")
            ).strip().upper()
            chapter = str(r.get("chapter") or r.get("rozdzial") or "").strip()
            verse_raw = (r.get("verse") or r.get("verses") or r.get("werset")
                         or r.get("wersety") or r.get("range") or "")
            verse = str(verse_raw).strip().replace(",", ":")
            if "[" in verse or "{" in verse:
                m = re.search(r"\b(\d+)\b", verse)
                verse = m.group(1) if m else ""
            raw_text = (r.get("text") or r.get("content") or r.get("snippet") or
                        r.get("fragment") or r.get("tekst") or r.get("tresc") or r.get("html") or "")
            txt = _coerce_text_block(raw_text)
            if not _is_texty(txt):
                txt = _extract_all_texts_from_any(str(r))
            if not _is_texty(txt):
                candidate = _longest_string_record(r)
                txt = candidate if _is_texty(candidate) else ""
            if txt:
                txt = html_lib.unescape(txt)
                txt = re.sub(r"(?is)</?strong[^>]*>", "", txt)
                txt = _strip_tags(txt).strip()
            if verse and txt:
                txt = re.sub(rf"^\s*{re.escape(verse)}[.)]\s*", "", txt)
            if not (b_disp and chapter and verse and _is_texty(txt)):
                continue
            ref = f"{b_disp} {chapter}:{verse}"
            out.append({"ref": ref, "snippet": txt})

        if out:
            if range_start is None or range_end is None:
                range_start = (page - 1) * limit + 1
                range_end = range_start + len(out) - 1
                if total_all and range_end > total_all:
                    range_end = total_all
            meta = {
                "page": page,
                "limit": limit,
                "total": total_all if total_all is not None else len(out),
                "start": range_start,
                "end": range_end,
            }
            for h in out:
                h["snippet"] = _highlight_case_insensitive(h["snippet"], phrase)
            cache_set(ck, (out, search_page_url, meta))
            return out, search_page_url, meta

    raise RuntimeError(f"Brak wyników lub nierozpoznany format API (status {last_status}). Body: {last_body[:300]}")

# ---------- KOMENDY: !w / !fp ----------
@bot.command(name="w")
async def werset(ctx, *, arg: str):
    parts = arg.rsplit(" ", 1)
    if len(parts) != 2:
        await ctx.reply("Użycie: `!w <KSIĘGA> <ROZDZIAŁ:WERS[-WERS]> <PRZEKŁAD>`\nnp. `!w 1 Kor 13:4 bw`")
    else:
        ref, trans = parts[0].strip(), parts[1].strip().lower()
        try:
            txt = await biblia_info_get_passage(trans, ref)
            embed = discord.Embed(title=f"{ref} — {trans.upper()}", description=txt[:4000])
            embed.set_footer(text="Źródło: biblia.info.pl")
            await ctx.reply(embed=embed)
        except Exception as e:
            await ctx.reply(f"❌ {e}")

@bot.command(name="fp")
async def fraza(ctx, *, arg: str):
    """
    !fp <fraza> [przekład] [all]
    Paginacja po stronie bota: pobieramy wszystkie wyniki (do limitu bezpieczeństwa),
    składamy w bloki i paginujemy przyciskami tak jak w !fh.
    """
    if not arg or not arg.strip():
        await ctx.reply("Użycie: `!fp <FRAZA> [PRZEKŁAD] [all]`")
        return

    parts = arg.strip().split()
    trans = "bw"
    fetch_all = False

    if parts[-1].lower() in ("all", "wsz", "wszystko"):
        fetch_all = True
        parts = parts[:-1]

    if parts and parts[-1].lower() in BIBLIA_INFO_CODES:
        trans = parts[-1].lower()
        parts = parts[:-1]

    phrase = " ".join(parts).strip()
    if not phrase:
        await ctx.reply("Podaj frazę do wyszukania, np. `!fp tak bowiem Bóg umiłował świat`")
        return

    # Ustawienia paginacji „po stronie bota”
    API_PAGE_SIZE = 25          # ile prosimy z API na krok (gdy API wspiera)
    MAX_ALL = 600               # twardy limit bezpieczeństwa łącznej liczby rekordów
    RESULTS_PER_PAGE = 10       # ile rekordów na stronę w embedzie

    hits_all = []
    meta_last = None
    search_url = None

    try:
        cur = 1
        while True:
            hits, search_url, meta = await biblia_info_search_phrase_api(
                trans, phrase, limit=API_PAGE_SIZE, page=cur
            )
            if not hits:
                break
            hits_all.extend(hits)
            meta_last = meta
            if (meta.get("end") and meta.get("total") and meta["end"] >= meta["total"]) \
               or len(hits_all) >= MAX_ALL \
               or not fetch_all:
                break
            cur += 1

    except Exception as e:
        await ctx.reply("Brak wyników albo problem z wyszukiwarką. Spróbuj inne parametry lub za chwilę.")
        print(f"[fp] error: {type(e).__name__}: {e}", flush=True)
        return

    if not hits_all:
        await ctx.reply("Brak wyników.")
        return

    total = (meta_last or {}).get("total") or len(hits_all)
    shown = min(len(hits_all), MAX_ALL)
    trans_name = TRANSLATION_NAMES.get(trans, trans.upper())

    blocks = [f"**{h.get('ref', '—')}** — { (h.get('snippet') or '').strip() }" for h in hits_all[:MAX_ALL]]

    head = [
        f"Znaleziono {total} wystąpień frazy «{phrase}» w tłumaczeniu {trans_name}.",
        f"Wyświetlam po {RESULTS_PER_PAGE} na stronę.",
        "" if shown < total else "",
    ]
    if shown < total:
        head.append(f"Pobrano do {shown} wyników (limit bezpieczeństwa {MAX_ALL}).")
        head.append("")

    title = f"Wyniki («{phrase}») — {trans.upper()}"
    footer = "Źródło: biblia.info.pl (API search)"

    view = FHResultsView(
        ctx_author_id=ctx.author.id,
        blocks=blocks,
        title=title,
        footer=footer,
        per_page=RESULTS_PER_PAGE,
        head_lines=head
    )

    msg = await ctx.reply(embed=view.make_embed(), view=view)
    view.message = msg

# ---------- PAGINACJA VIEW dla !fh ----------
class FHResultsView(discord.ui.View):
    def __init__(self, ctx_author_id: int, blocks: list[str], title: str, footer: str, per_page: int = 3, head_lines: list[str] | None = None):
        super().__init__(timeout=900)  # do 15 min
        self.ctx_author_id = ctx_author_id
        self.blocks = blocks
        self.per_page = max(1, per_page)
        self.page = 0
        self.footer = footer
        self.title = title
        self.head_lines = head_lines or []
        self.message: discord.Message | None = None
        self.locked_to_author = os.getenv("FH_LOCKED_TO_AUTHOR", "0") in ("1", "true", "yes")
        self.cooldown = 1.5
        self._last_click_per_user: dict[int, float] = {}

    @property
    def total_pages(self):
        return max(1, (len(self.blocks) + self.per_page - 1) // self.per_page)

    def _page_slice(self):
        a = self.page * self.per_page
        b = min(len(self.blocks), a + self.per_page)
        return self.blocks[a:b]

    def make_embed(self):
        parts = []
        if self.page == 0 and self.head_lines:
            parts.append("\n".join(self.head_lines).strip())
        parts.append("\n\n".join(self._page_slice()).strip())
        desc = "\n\n".join([p for p in parts if p]).strip()

        header = f"{self.title} — strona {self.page+1}/{self.total_pages}"
        embed = discord.Embed(title=header, description=desc[:4000])
        embed.set_footer(text=self.footer)
        return embed

    async def _can_interact(self, interaction: discord.Interaction) -> bool:
        if self.locked_to_author and interaction.user.id != self.ctx_author_id:
            await interaction.response.send_message("Tę paginację może obsługiwać tylko autor (FH_LOCKED_TO_AUTHOR).", ephemeral=True)
            return False
        now = time.time()
        last = self._last_click_per_user.get(interaction.user.id, 0.0)
        if now - last < self.cooldown:
            await interaction.response.send_message("Daj sekundkę… (cooldown)", ephemeral=True)
            return False
        self._last_click_per_user[interaction.user.id] = now
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="⏮︎", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._can_interact(interaction): return
        self.page = 0
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="◀︎", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._can_interact(interaction): return
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="▶︎", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._can_interact(interaction): return
        if self.page < self.total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="⏭︎", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._can_interact(interaction): return
        self.page = self.total_pages - 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

# ---------- KOMENDA: !fh (hebrajski, WLC, czyste PL, paginacja) ----------
@bot.command(name="fh")
async def find_hebrew(ctx, *, arg: str):
    """
    !fh <hebrajski> [strona|all] [mesora]
    - Rdz 1:6 → HE (bold) → (pusta linia) → BT → (pusta) → BW
    - PL czyszczone z nagłówków/note/©
    - paginacja przyciskami (3/stronę)
    """
    if not arg or not arg.strip():
        await ctx.reply("Użycie: `!fh <FRAZA_HEBRAJSKA> [STRONA|all] [mesora]`")
        return

    parts = arg.strip().split()
    page = 1
    fetch_all = False
    mesora_mode = False

    normalized = [p.lower() for p in parts]
    for kw in ("mesora","mesorah","taamim","cantillation"):
        if kw in normalized:
            mesora_mode = True
            parts = [p for p in parts if p.lower() != kw]
            break

    if parts and parts[-1].lower() in ("all","wsz","wszystko"):
        fetch_all = True
        parts = parts[:-1]
    elif parts and parts[-1].isdigit():
        page = max(1, int(parts[-1]))
        parts = parts[:-1]

    raw_query = " ".join(parts).strip()
    if not raw_query:
        await ctx.reply("Podaj frazę, np. `!fh ויאמר אלהים`")
        return

    PER_PAGE_API = 10
    MAX_ALL = 1000

    try:
        if fetch_all:
            cur = 1
            all_hits = []
            meta_final = None
            while True:
                hs, meta = await api_bible_search_hebrew(raw_query, page=cur, per_page=PER_PAGE_API)
                all_hits.extend(hs)
                meta_final = meta
                if not hs or cur >= meta.get("pages", 1) or len(all_hits) >= MAX_ALL:
                    break
                cur += 1
            hits = all_hits[:MAX_ALL]
            meta = meta_final or {"total": len(hits), "page": 1, "pages": 1}
        else:
            hits, meta = await api_bible_search_hebrew(raw_query, page=page, per_page=PER_PAGE_API)
    except Exception as e:
        await ctx.reply(f"❌ Problem z wyszukiwaniem: {e}")
        return

    if not hits:
        await ctx.reply("Brak wyników.")
        return

    total = meta.get("total", len(hits))
    pages_api = meta.get("pages", 1)
    cur_page_api = meta.get("page", page)

    hl_query = raw_query
    if not has_niqqud(raw_query):
        hinted = add_niqqud_hints_if_missing(raw_query)
        if hinted != raw_query:
            hl_query = hinted

    async def build_block(v):
        verse_id = v["id"]
        he_text = await api_bible_get_he_text(verse_id, mesora=mesora_mode)
        ref_pl, header_pl = _pl_ref_from_usfm(verse_id)
        if not header_pl:
            header_pl = _strip_tags(v.get("reference") or verse_id)
        he_for_embed = he_text if mesora_mode else highlight_hebrew(he_text, hl_query)

        bt_txt = ""
        bw_txt = ""
        if ref_pl:
            try:
                bt_txt = await biblia_info_get_passage("bt", ref_pl)
            except Exception:
                bt_txt = ""
            try:
                bw_txt = await biblia_info_get_passage("bw", ref_pl)
            except Exception:
                bw_txt = ""

        if bt_txt:
            bt_txt = highlight_polish_like(bt_txt, raw_query)
        if bw_txt:
            bw_txt = highlight_polish_like(bw_txt, raw_query)

        lines = [f"**{header_pl}**", he_for_embed, ""]
        if bt_txt:
            lines.append(f"*BT:* {bt_txt}")
        if bt_txt and bw_txt:
            lines.append("")
        if bw_txt:
            lines.append(f"*BW:* {bw_txt}")
        return "\n".join(lines).strip()

    BATCH = 10
    blocks = []
    for i in range(0, len(hits), BATCH):
        chunk = hits[i:i+BATCH]
        blocks.extend(await asyncio.gather(*(build_block(v) for v in chunk)))

    title = f"Wyszukiwanie (HE): «{raw_query}» — WLC"
    head = [
        f"Znaleziono {total} wystąpień.",
        f"Strona API {cur_page_api}/{pages_api}, {PER_PAGE_API} na stronę.",
        ""
    ]
    if fetch_all:
        head = [f"Znaleziono {total} wystąpień.", f"Pobrano do {len(blocks)} wyników (limit {MAX_ALL}).", ""]

    footer = "Źródła: api.bible (WLC) + biblia.info.pl (BT, BW)"
    RESULTS_PER_PAGE = 3

    view = FHResultsView(ctx.author.id, blocks=blocks, title=title, footer=footer, per_page=RESULTS_PER_PAGE, head_lines=head)
    msg = await ctx.reply(embed=view.make_embed(), view=view)
    view.message = msg

# ---------- PSALMY: liczba wersetów ----------
PSALM_VERSES = {
    1:6, 2:12, 3:9, 4:9, 5:13, 6:11, 7:18, 8:10, 9:21, 10:18,
    11:7, 12:9, 13:6, 14:7, 15:5, 16:11, 17:15, 18:51, 19:15, 20:10,
    21:14, 22:32, 23:6, 24:10, 25:22, 26:12, 27:14, 28:9, 29:11, 30:13,
    31:25, 32:11, 33:22, 34:23, 35:28, 36:13, 37:40, 38:23, 39:14, 40:18,
    41:14, 42:12, 43:5, 44:27, 45:18, 46:12, 47:10, 48:15, 49:21, 50:23,
    51:21, 52:11, 53:7, 54:9, 55:24, 56:14, 57:12, 58:12, 59:18, 60:14,
    61:9, 62:13, 63:12, 64:11, 65:14, 66:20, 67:8, 68:36, 69:37, 70:6,
    71:24, 72:20, 73:28, 74:23, 75:11, 76:13, 77:21, 78:72, 79:13, 80:20,
    81:17, 82:8, 83:19, 84:13, 85:14, 86:17, 87:7, 88:19, 89:53, 90:17,
    91:16, 92:16, 93:5, 94:23, 95:11, 96:13, 97:12, 98:9, 99:9, 100:5,
    101:8, 102:29, 103:22, 104:35, 105:45, 106:48, 107:43, 108:14, 109:31, 110:7,
    111:10, 112:10, 113:9, 114:8, 115:18, 116:19, 117:2, 118:29, 119:176, 120:7,
    121:8, 122:9, 123:4, 124:8, 125:5, 126:6, 127:5, 128:6, 129:8, 130:8,
    131:3, 132:18, 133:3, 134:3, 135:21, 136:26, 137:9, 138:8, 139:24, 140:14,
    141:10, 142:8, 143:12, 144:15, 145:21, 146:10, 147:20, 148:14, 149:9, 150:6
}

# ---------- KOMENDA: !psalm (jak !w, ale tylko Psalmy; losuje gdy bez argumentów) ----------
@bot.command(name="psalm")
async def psalm_cmd(ctx, *, arg: str | None = None):
    """
    Użycie:
      !psalm                  -> losowy Psalm (BW)
      !psalm 23               -> cały Ps 23 (BW)
      !psalm 23 ubg           -> cały Ps 23 (UBG)
      !psalm 23:1-9           -> Ps 23:1-9 (BW)
      !psalm 23 1-9 bt        -> Ps 23:1-9 (BT)
    """
    trans = "bw"
    num = None
    vrange = None

    if arg and arg.strip():
        parts = arg.strip().split()

        # Ostatni token = kod przekładu?
        if parts and parts[-1].lower() in BIBLIA_INFO_CODES:
            trans = parts[-1].lower()
            parts = parts[:-1]

        # Złap formy: "23", "23:1-9", "23 1-9"
        rest = " ".join(parts)
        m = re.match(r"^\s*(\d{1,3})(?::\s*([\d\-]+))?\s*$", rest)
        if not m and parts:
            # spróbuj wariantu "23 1-9"
            m = re.match(r"^\s*(\d{1,3})\s+([\d\-]+)\s*$", rest)
        if m:
            num = int(m.group(1))
            vrange = m.group(2) if m.lastindex and m.group(2) else None

    # Losowo, jeśli nie podano numeru
    if num is None:
        num = random.randint(1, 150)

    # Jeśli nie podano zakresu, bierz cały psalm (korzystamy z PSALM_VERSES jeśli masz; inaczej 1-200)
    end = PSALM_VERSES.get(num, 200) if 'PSALM_VERSES' in globals() else 200
    ref = f"Ps {num}:{vrange if vrange else f'1-{end}'}"

    try:
        txt = await biblia_info_get_passage(trans, ref)
        if not txt:
            raise RuntimeError("Pusty wynik.")
        embed = discord.Embed(title=f"{ref} — {trans.upper()}", description=txt[:4000])
        embed.set_footer(text="Źródło: biblia.info.pl")
        await ctx.reply(embed=embed)
    except Exception as e:
        await ctx.reply(f"❌ Nie udało się pobrać {ref} ({trans.upper()}): {e}")


# ---------- utilities ----------

@bot.command(name="pascha")
async def pascha(ctx, arg: str | None = None):
    """
    Oblicza biblijną Paschę (14 Nisan) tak, jak chcesz:
    1) bierzemy równonoc = 20 marca danego roku
    2) bierzemy PIERWSZY nów PO tej dacie
    3) 14 Nisan = nów + 13 dni
    Użycie:
      !pascha          → bieżący rok
      !pascha 2027     → konkretny rok
      !pascha all      → lista np. 2024–2030
    """

    def oblicz_pasche_dla_roku(rok: int) -> tuple[datetime.date, datetime.date, datetime.date]:
        # 1. równonoc — przyjmujemy zawsze 20 marca danego roku (twoja zasada)
        rownonoc = datetime.datetime(rok, 3, 20, 12, 0, 0)  # godzina tu nie ma aż takiego znaczenia

        # 2. pierwszy nów PO równonocy
        # ephem potrzebuje stringa lub datetime
        # bierzemy next_new_moon OD 20 marca → to da pierwszy po równonocy, nie wcześniejszy
        pierwszy_now = ephem.next_new_moon(rownonoc)
        pierwszy_now = ephem.localtime(pierwszy_now)

        # 3. 14 Nisan = nów + 13 dni
        pascha_dt = pierwszy_now + datetime.timedelta(days=13)

        return rownonoc, pierwszy_now, pascha_dt

    # --- tryb lista ---
    if arg and arg.lower() == "all":
        start = 2024
        end = 2030
        lines: list[str] = []
        for rok in range(start, end + 1):
            _, now_dt, pascha_dt = oblicz_pasche_dla_roku(rok)
            lines.append(
                f"**{rok}** — nów: {now_dt.strftime('%d %b %Y')} → Pascha 🌕 {pascha_dt.strftime('%d %b %Y')}"
            )

        # ewentualny podział na kilka embedów, gdyby było za długo
        buf = ""
        chunks: list[str] = []
        for ln in lines:
            add = ln + "\n"
            if len(buf) + len(add) > 3800:
                chunks.append(buf)
                buf = add
            else:
                buf += add
        if buf:
            chunks.append(buf)

        for i, chunk in enumerate(chunks, 1):
            embed = discord.Embed(
                title=f"📅 Biblijna Pascha — {start}–{end} (część {i}/{len(chunks)})",
                description=chunk,
                color=0xFFD700,
            )
            embed.set_footer(text="Zasada: pierwszy nów po 20 marca + 13 dni = 14 Nisan")
            await ctx.reply(embed=embed)
        return

    # --- tryb pojedynczy rok ---
    if arg and arg.isdigit():
        rok = int(arg)
    else:
        rok = datetime.datetime.utcnow().year

    rownonoc, now_dt, pascha_dt = oblicz_pasche_dla_roku(rok)

    embed = discord.Embed(
        title=f"📆 Biblijna Pascha — {rok}",
        description=(
            f"🌸 **Równonoc przyjęta:** 20 marca {rok}\n"
            f"🌑 **Pierwszy nów po równonocy:** {now_dt.strftime('%d %B %Y')} "
            f"(astron.)\n\n"
            f"🌕 **14 Nisan (Pascha):** {pascha_dt.strftime('%d %B %Y')}"
        ),
        color=0xFFD700,
    )
    embed.set_footer(text="Obliczanie wg: pierwszy nów po 20 marca → +13 dni")
    await ctx.reply(embed=embed)

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

