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

# ---- USFM → polskie skróty (nagłówek) ----
USFM_TO_PL = {
    "GEN": "Rdz","EXO": "Wj","LEV": "Kpł","NUM": "Lb","DEU": "Pwt",
    "JOS": "Joz","JDG": "Sdz","RUT": "Rut",
    "1SA":"1Sm","2SA":"2Sm","1KI":"1Krl","2KI":"2Krl",
    "1CH":"1Krn","2CH":"2Krn","EZR":"Ezd","NEH":"Neh","EST":"Est",
    "JOB":"Hi","PSA":"Ps","PRO":"Prz","ECC":"Koh","SNG":"Pnp",
    "ISA":"Iz","JER":"Jer","LAM":"Lm","EZK":"Ez","DAN":"Dn",
    "HOS":"Oz","JOL":"Jl","AMO":"Am","OBA":"Ab","JON":"Jon",
    "MIC":"Mi","NAM":"Na","HAB":"Ha","ZEP":"So","HAG":"Ag","ZEC":"Za","MAL":"Ml",
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
REF_RE = re.compile(r"^\s*([^\d]+)\s+(\d+):(\d+(?:-\d+)?)\s*$", re.IGNORECASE)

def parse_ref(ref: str):
    m = REF_RE.match(ref)
    if not m:
        return None
    book_pl, ch, vs = m.groups()
    return book_pl.strip(), ch, vs

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

def clean_pl_verse_text(t: str) -> str:
    """
    Czyści z nadmiarowych nagłówków/mety (Księga..., (6), 46:2, 3,4, lata 1998–2023, IB2000, ©, autor).
    Zostawia sam tekst wersetu.
    """
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
    out = re.sub(r"(?m)^\s*\d+[.)]\s*", "", out)  # wiodący "6. "
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out

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
    slug_try = [
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
    """
    Zwraca (hits, meta); hits = [{id, reference}]
    Uwaga: 'offset' = numer strony (0-based) w api.bible.
    """
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

    # retry: jeśli 0 wyników i brak niqqud – podpowiedz
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
    """
    Tekst HE wersetu; mesora=True => HTML (zachowane wszystkie znaczniki),
    mesora=False => plain text (Unicode) – nadal z niqqud/ta’amim.
    """
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

# ---------- KOMENDY: !werset / !fraza ----------
@bot.command(name="werset")
async def werset(ctx, *, arg: str):
    parts = arg.rsplit(" ", 1)
    if len(parts) != 2:
        await ctx.reply("Użycie: `!werset <KSIĘGA> <ROZDZIAŁ:WERS[-WERS]> <PRZEKŁAD>`\nnp. `!werset J 3:16 bw`")
    else:
        ref, trans = parts[0].strip(), parts[1].strip().lower()
        try:
            txt = await biblia_info_get_passage(trans, ref)
            embed = discord.Embed(title=f"{ref} — {trans.upper()}", description=txt[:4000])
            embed.set_footer(text="Źródło: biblia.info.pl")
            await ctx.reply(embed=embed)
        except Exception as e:
            await ctx.reply(f"❌ {e}")

@bot.command(name="fraza")
async def fraza(ctx, *, arg: str):
    if not arg or not arg.strip():
        await ctx.reply("Użycie: `!fraza <FRAZA> [PRZEKŁAD] [STRONA|all]`")
        return

    PAGE_SIZE = 25
    parts = arg.strip().split()
    page = 1
    trans = "bw"
    fetch_all = False

    if parts[-1].lower() in ("all","wsz","wszystko"):
        fetch_all = True
        parts = parts[:-1]

    if parts and parts[-1].isdigit():
        page = max(1, int(parts[-1])); parts = parts[:-1]

    if parts and parts[-1].lower() in BIBLIA_INFO_CODES:
        trans = parts[-1].lower(); parts = parts[:-1]

    phrase = " ".join(parts).strip()
    if not phrase:
        await ctx.reply("Podaj frazę do wyszukania, np. `!fraza tak bowiem Bóg umiłował świat`")
        return

    hits_all = []
    meta_last = None
    search_url = None

    try:
        if fetch_all:
            cur = page
            for _ in range(10):
                hits, search_url, meta = await biblia_info_search_phrase_api(
                    trans, phrase, limit=PAGE_SIZE, page=cur
                )
                if not hits:
                    break
                hits_all.extend(hits)
                meta_last = meta
                if meta.get("end") and meta.get("total") and meta["end"] >= meta["total"]:
                    break
                cur += 1
        else:
            hits_all, search_url, meta_last = await biblia_info_search_phrase_api(
                trans, phrase, limit=PAGE_SIZE, page=page
            )
    except Exception as e:
        await ctx.reply("Brak wyników albo problem z wyszukiwarką. Spróbuj inne parametry lub za chwilę.")
        print(f"[fraza] error: {type(e).__name__}: {e}", flush=True)
        return

    if not hits_all:
        await ctx.reply("Brak wyników.")
        return

    trans_name = TRANSLATION_NAMES.get(trans, trans.upper())
    total = meta_last.get("total") if meta_last else len(hits_all)

    if fetch_all:
        start, end = 1, len(hits_all)
        title = f"Wyniki („{phrase}”) — {trans.upper()} — wszystkie ({end} z {total})"
    else:
        start = meta_last.get("start") or 1
        end = meta_last.get("end") or (start + len(hits_all) - 1)
        title = f"Wyniki („{phrase}”) — {trans.upper()} — strona {page}"

    summary_line = (
        f"Znaleziono {total} wystąpień frazy «{phrase}» "
        f"w tłumaczeniu {trans_name}. Wyświetlono wyniki {start}–{end}."
    )

    lines = [summary_line, ""]
    for h in hits_all:
        ref = h.get("ref", "—")
        snip = (h.get("snippet") or "").strip()
        lines.append(f"**{ref}** — {snip}")

    footer = "Źródło: biblia.info.pl (API search)"
    chunks = _split_for_embeds(title, footer, lines, limit=4000)

    first = True
    for ch in chunks:
        embed = discord.Embed(title=ch["title"], description=ch["description"])
        if first and search_url:
            embed.url = search_url
            first = False
        embed.set_footer(text=ch["footer"])
        await ctx.reply(embed=embed)

# ---------- PAGINACJA VIEW dla !fh ----------
class FHResultsView(discord.ui.View):
    def __init__(self, ctx_author_id: int, blocks: list[str], title: str, footer: str, per_page: int = 3):
        super().__init__(timeout=180)
        self.ctx_author_id = ctx_author_id
        self.blocks = blocks
        self.per_page = max(1, per_page)
        self.page = 0
        self.footer = footer
        self.title = title

        # Czy blokować na autora? 0/1 z ENV (domyślnie: NIE blokuj).
        self.locked_to_author = os.getenv("FH_LOCKED_TO_AUTHOR", "0") in ("1", "true", "yes")
        # Anty-spam: cooldown (sekundy) per user
        self.cooldown = 1.5
        self._last_click_per_user: dict[int, float] = {}

    @property
    def total_pages(self):
        from math import ceil
        return max(1, (len(self.blocks) + self.per_page - 1) // self.per_page)

    def _page_slice(self):
        a = self.page * self.per_page
        b = min(len(self.blocks), a + self.per_page)
        return self.blocks[a:b]

    def make_embed(self):
        chunk = "\n\n".join(self._page_slice()).strip()
        header = f"{self.title} — strona {self.page+1}/{self.total_pages}"
        embed = discord.Embed(title=header, description=chunk[:4000])
        embed.set_footer(text=self.footer)
        return embed

    async def _can_interact(self, interaction: discord.Interaction) -> bool:
        # 1) opcjonalna blokada do autora (włączana ENV)
        if self.locked_to_author and interaction.user.id != self.ctx_author_id:
            await interaction.response.send_message(
                "Tę paginację może obsługiwać tylko autor komendy (ustawienie FH_LOCKED_TO_AUTHOR).",
                ephemeral=True
            )
            return False
        # 2) prosty cooldown per user
        import time as _t
        now = _t.time()
        last = self._last_click_per_user.get(interaction.user.id, 0.0)
        if now - last < self.cooldown:
            await interaction.response.send_message("Daj mi sekundkę… (cooldown)", ephemeral=True)
            return False
        self._last_click_per_user[interaction.user.id] = now
        return True

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
    - czysty układ: Rdz 1:6 → HE (bold) → (pusta linia) → BT → BW
    - czyszczenie metadanych BT/BW (nagłówki, (6), 46:2, lata/copyright)
    - paginacja na przyciskach (domyślnie 3 wyniki/stronę)
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
    MAX_ALL = 150  # bezpieczeństwo

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

    # forma do bold w HE
    hl_query = raw_query
    if not has_niqqud(raw_query):
        hinted = add_niqqud_hints_if_missing(raw_query)
        if hinted != raw_query:
            hl_query = hinted

    # zbuduj bloki wyników
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

        lines = [f"**{header_pl}**", he_for_embed]
        lines.append("")  # odstęp między HE a tłumaczeniami
        if bt_txt:
            lines.append(f"*BT:* {bt_txt}")
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
    head = []
    head.append(f"Znaleziono {total} wystąpień.")
    if fetch_all:
        head.append(f"Pobrano do {len(blocks)} wyników (limit bezpieczeństwa {MAX_ALL}).")
    else:
        head.append(f"Strona API {cur_page_api}/{pages_api}, {PER_PAGE_API} na stronę.")
    head.append("")  # pusty wiersz

    footer = "Źródła: api.bible (WLC) + biblia.info.pl (BT, BW)"
    RESULTS_PER_PAGE = 3

    view = FHResultsView(ctx.author.id, blocks=blocks, title=title, footer=footer, per_page=RESULTS_PER_PAGE)
    first_embed = discord.Embed(
        title=f"{title} — strona 1/{view.total_pages}",
        description="\n".join(head + blocks[:RESULTS_PER_PAGE])[:4000]
    )
    first_embed.set_footer(text=footer)
    await ctx.reply(embed=first_embed, view=view)

# ---------- utilities ----------
@bot.command()
async def ping(ctx):
    await ctx.reply("pong")

@bot.command()
async def diag(ctx):
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

