"""
Sammelt offene Lehrerstellen aus mehreren Schweizer Quellen und schreibt
docs/jobs.json fuer die Webseite.

Quellen:
  1. Stellenportal Volksschule Aargau (schulen-aargau.ch/stellen)
  2. Stellenmarkt Kanton Aargau (Kantonsschulen, Berufsfachschulen)
  3. VSA-Stellenboerse Kanton Zuerich (Volksschule, Dauerstellen + Stellvertretungen)
  4. job-room.ch (Stellenboerse des Bundes, viele Kantone, Privatschulen usw.)

Nur Python-Standardbibliothek, damit es ohne Installation auf GitHub Actions laeuft.
Aufruf:  python scraper/fetch_jobs.py
"""

from __future__ import annotations

import html
import json
import math
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "docs" / "jobs.json"
PLZ_OUT_FILE = ROOT / "docs" / "plz.json"
CACHE_FILE = ROOT / "scraper" / "cache" / "details.json"
PLZ_FILE = ROOT / "scraper" / "plz_ch.tsv"

UA = "Mozilla/5.0 (compatible; lehrerjobs-privat/1.0; persoenliche Stellensuche)"
NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()

# Kantone, die bei job-room.ch abgefragt werden (Umgebung Neuenhof/Spreitenbach)
JOBROOM_CANTONS = ["AG", "ZH", "LU", "ZG", "SO", "BL", "BS", "SZ"]
JOBROOM_KEYWORDS = [
    "Lehrperson", "Lehrer", "Lehrerin", "Lehrkraft", "Mittelschullehrperson",
    "Fachlehrperson", "Berufsfachschule", "Lehrauftrag", "Lehrbeauftragte",
    "Bildnerisches Gestalten", "Englisch", "Stellvertretung Lehrperson",
    "Kantonsschule", "Gymnasium",
]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http(url: str, data: bytes | None = None, headers: dict | None = None,
         tries: int = 3, timeout: int = 40) -> tuple[bytes, dict]:
    h = {"User-Agent": UA, "Accept-Language": "de-CH,de;q=0.9"}
    if headers:
        h.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h,
                                         method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), dict(r.headers)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def get_text(url: str) -> str:
    body, _ = http(url)
    return body.decode("utf-8", errors="replace")


def get_json(url: str):
    body, _ = http(url, headers={"Accept": "application/json"})
    return json.loads(body)


# --------------------------------------------------------------------------- #
# Text-Helfer
# --------------------------------------------------------------------------- #

def strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|div|h\d|tr|dd|dt)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", s)
    s = re.sub(r"\n\s*", "\n", s)
    return s.strip()


def clean(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"</?em>", "", s)
    s = re.sub(r"\\([&*_#\[\]()-])", r"\1", s)    # Markdown-Escapes von job-room
    s = re.sub(r"\*\*|__", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def snippet(s: str, n: int = 420) -> str:
    s = clean(s)
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut + " …"


def parse_date_ch(s: str | None) -> str | None:
    """'30.11.2026' -> '2026-11-30'"""
    if not s:
        return None
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        d, mth, y = map(int, m.groups())
        try:
            return datetime(y, mth, d).date().isoformat()
        except ValueError:
            return None
    months = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
              "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}
    m = re.search(r"(\d{1,2})\.?\s+([A-Za-zäÄ]+)\s+(\d{4})", s)
    if m and m.group(2).lower() in months:
        try:
            return datetime(int(m.group(3)), months[m.group(2).lower()], int(m.group(1))).date().isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    return None


def parse_pensum(text: str) -> tuple[int | None, int | None]:
    """Sucht '50-80 %', '50 – 80%', '60%' im Text."""
    t = text.replace("–", "-").replace("—", "-")
    m = re.search(r"(\d{1,3})\s*%?\s*(?:-|bis)\s*(\d{1,3})\s*%", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b <= 300:
            return a, b
    m = re.search(r"(\d{1,3})\s*%", t)
    if m:
        a = int(m.group(1))
        if 0 < a <= 300:
            return a, a
    return None, None


def parse_lektionen(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*(?:-\s*\d{1,2}\s*)?(?:WL\b|Wochenlektionen|Lektionen|Lekt\.?\b|L\b)", text)
    if m:
        v = int(m.group(1))
        if 0 < v <= 35:
            return v
    return None


# --------------------------------------------------------------------------- #
# Klassifizierung: Fach, Schulstufe, Rolle
# --------------------------------------------------------------------------- #

SUBJECTS: list[tuple[str, str]] = [
    ("Bildnerisches Gestalten", r"bildnerisch|\bBG\b|visuelle[nr]? gestaltung|gestaltung und kunst|\bkunst\b|kunsterziehung"),
    ("Englisch", r"englisch|\benglish\b"),
    ("Deutsch", r"\bdeutsch\b(?! als zweit)"),
    ("DaZ", r"\bDaZ\b|deutsch als zweitsprache"),
    ("Französisch", r"französisch|franzoesisch|\bfranz\b|\bfrz\b"),
    ("Italienisch", r"italienisch"),
    ("Spanisch", r"spanisch"),
    ("Latein / Griechisch", r"latein|griechisch"),
    ("Mathematik", r"mathematik|\bmathe\b"),
    ("Physik", r"\bphysik"),
    ("Chemie", r"\bchemie"),
    ("Biologie", r"biologie"),
    ("Natur & Technik / NMG", r"\bNT\b|natur und technik|naturwissenschaft|\bNMG\b|natur, mensch"),
    ("Geografie", r"geogra(?:f|ph)ie|\bRZG\b|räume,? zeiten"),
    ("Geschichte", r"\bgeschichte\b|\bRZG\b|räume,? zeiten"),
    ("Berufliche Orientierung", r"berufliche orientierung|berufswahlunterricht|\bBO\b"),
    ("Projektunterricht", r"projektunterricht|projektarbeit|\bPU\b"),
    ("Wirtschaft & Recht", r"wirtschaft und recht|wirtschaft & recht|\bW&R\b|\bWR\b"),
    ("WAH / Hauswirtschaft", r"\bWAH\b|hauswirtschaft|wirtschaft, arbeit, haushalt"),
    ("Informatik / Medien", r"informatik|\bMI\b"),
    ("Musik", r"\bmusik|schulmusik|klavier|gesang|saxophon|gitarre|violin|instrumental"),
    ("Sport", r"\bsport\b|bewegung und sport|\bturnen\b|schwimmen"),
    ("TTG / Werken", r"\bTTG\b|textiles?\b|technisches gestalten|\bwerken\b"),
    ("Religion / Ethik", r"\bERG\b|\bRKE\b|religion|\bethik"),
    ("Philosophie", r"philosophie"),
    ("Psychologie / Pädagogik", r"\bpsychologie|pädagogik und psychologie|pädagogik/psychologie"),
    ("ABU / Allgemeinbildung", r"\bABU\b|allgemeinbildend|allgemeinbildung"),
    ("Berufskunde", r"berufskunde|fachkunde"),
]

LEVELS: list[tuple[str, str]] = [
    ("Gymnasium / Mittelschule", r"kantonsschul|gymnas|mittelschul|maturit(?:ä|ae)tsschul|\bkanti\b|\bFMS\b|\bWMS\b|sekundarstufe ii|sek ii"),
    ("Berufsfachschule", r"berufsfachschul|berufsschul|berufsmaturit|\bBMS\b|\bABU\b|allgemeinbildend|berufskunde|überbetriebliche|kaufmännische|wirtschaftsschule|berufsbildungszentrum|\bBBZ\b|gewerblich|berufsbildungsschule|schule für berufsbildung|\bKV\b"),
    ("Sekundarstufe I", r"sekundar(?!stufe ii)|bezirksschul|realschul|oberstufe|\bWAH\b|\bsek\b(?! ?ii)|zyklus 3|berufswahljahr|werkjahr|\bsek\s?[abc]\b"),
    ("Primarschule", r"primar|zyklus 2|mittelstufe|unterstufe|einführungsklasse|\b[1-6]\.(?:\s*[/-]\s*[1-6]\.)?\s*klasse"),
    ("Kindergarten", r"kindergarten|\bkiga\b|zyklus 1|\bKG\b(?!\s*&)"),
    ("Heilpädagogik / Sonderschule", r"heilpäd|\bSHP\b|sonderschul|förderklass|sprachheil|logopäd|\bISF\b|\bIF\b|tagessonderschul"),
    ("Hochschule", r"hochschule|\bPHZH\b|\bFHNW\b|universität|\bZHAW\b|dozent"),
    ("Musikschule", r"musikschul|instrumentalunterricht"),
    ("Erwachsenenbildung / Kurse", r"klubschule|sprachschule|nachhilfe|erwachsenenbildung|weiterbildung|strafvollzug|\bDaF\b"),
]

TEACH_RE = re.compile(
    r"lehrperson|lehrer|lehrkraft|lehrbeauftragt|lehrauftrag|dozent|teacher|enseignant|"
    r"unterricht|vikariat|heilpädagog|\bSHP\b|kindergärtner", re.I)
NOT_TEACH_RE = re.compile(
    r"lehrstelle|\blernende|lehrling|schnupper|praktik|doktorand|assistenzperson|"
    r"klassenassistenz|schulsozial|hauswart|sekretär|sachbearbeit|lehrbetrieb|"
    r"berufsbildner|ausbildner|fahrlehrer|kursleiter.*fitness", re.I)
LEAD_RE = re.compile(r"schulleit|standortleit|stufenleit|schulhausleit|prorektor|rektor|co-leitung|gesamtleit", re.I)
ASSIST_RE = re.compile(r"assistenz|schulsozial|betreuung|hort|tagesstruktur|logopäd|psychomotorik|therapeut", re.I)
SUBST_RE = re.compile(r"stellvertretung|vikariat|\bstv\b\.?|mutterschaftsvertretung|vertretung\b", re.I)


def mentioned(table: list[tuple[str, str]], text: str, exclude: list[str]) -> list[str]:
    """Faecher, die nur im Beschreibungstext vorkommen (schwaecherer Treffer)."""
    return [n for n, rx in table if n not in exclude and re.search(rx, text or "", re.I)]


def classify(table: list[tuple[str, str]], primary: str, fallback: str = "") -> list[str]:
    found = [name for name, rx in table if re.search(rx, primary, re.I)]
    if not found and fallback:
        found = [name for name, rx in table if re.search(rx, fallback, re.I)]
    return found


def role_of(title: str, extra: str = "") -> str:
    t = f"{title} {extra}"
    if re.search(r"assistent|assistenz|sekretar|sekretär|sachbearbeit", title, re.I) and not re.search(r"lehrperson|lehrer", title, re.I):
        return "Assistenz / Fachperson"
    if LEAD_RE.search(title):
        return "Schulleitung"
    if NOT_TEACH_RE.search(title) and not re.search(r"lehrperson|lehrer", title, re.I):
        return "Andere"
    if TEACH_RE.search(t):
        return "Lehrperson"
    if ASSIST_RE.search(title):
        return "Assistenz / Fachperson"
    return "Andere"


# --------------------------------------------------------------------------- #
# Orte / Koordinaten
# --------------------------------------------------------------------------- #

class Geo:
    def __init__(self, path: Path):
        self.by_plz: dict[str, tuple[float, float, str]] = {}
        self.by_name: dict[str, list[tuple[float, float, str]]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            plz, place, canton, lat, lon, commune = parts
            rec = (float(lat), float(lon), canton)
            self.by_plz.setdefault(plz, rec)
            for n in {self.norm(place), self.norm(commune)}:
                if n:
                    self.by_name.setdefault(n, []).append(rec)

    @staticmethod
    def norm(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"\ba\.\s*a\.?", "am albis", s)
        s = re.sub(r"\ba\.\s*s\.?", "am see", s)
        s = re.sub(r"\ba\.\s*d\.\s*", "an der ", s)
        s = re.sub(r"^(stadt|gemeinde|schule|schulgemeinde|primarschulgemeinde|sekundarschulgemeinde|kreisschule)\s+", "", s)
        s = re.sub(r"\(.*?\)", "", s)
        s = re.sub(r"\b(ag|zh|lu|zg|so|bl|bs|sz|be|sg|tg)\b", "", s)
        s = re.sub(r"\s+\d+$", "", s)
        s = re.sub(r"[^a-zäöüéèàç-]+", " ", s)
        return s.strip()

    def lookup(self, plz: str | None = None, name: str | None = None,
               canton: str | None = None) -> tuple[float, float, str] | None:
        if plz and plz in self.by_plz:
            return self.by_plz[plz]
        if name:
            cands = self.by_name.get(self.norm(name))
            if not cands:
                # 'Illnau-Effretikon' -> 'Illnau', 'Schinznach und Veltheim' -> 'Schinznach'
                first = re.split(r"[-/,]| und |\s", name.strip())[0]
                cands = self.by_name.get(self.norm(first))
            if cands:
                if canton:
                    for c in cands:
                        if c[2] == canton:
                            return c
                return cands[0]
        return None


def find_plz(text: str) -> tuple[str | None, str | None]:
    m = re.search(r"\b([1-9]\d{3})\s+([A-ZÄÖÜ][\w .äöüÄÖÜéè-]+)", text or "")
    if m:
        return m.group(1), m.group(2).strip()
    return None, None


# --------------------------------------------------------------------------- #
# Quelle 1: Volksschule Aargau (Talentsoft-Portal)
# --------------------------------------------------------------------------- #

AGVS = "https://kantonaargau-career.talent-soft.com"


def agvs_list() -> list[dict]:
    items: dict[str, dict] = {}
    for page in range(1, 80):
        url = f"{AGVS}/offre-de-emploi/liste-offres.aspx?page={page}&LCID=1031"
        s = get_text(url)
        new = 0
        for li in re.findall(r'<li class="ts-offer-list-item.*?</ul>\s*</li>', s, re.S):
            m = re.search(r'href="(/stelle/[^"]*_(\d+)\.aspx)"[^>]*>\s*(.*?)\s*</a>', li, re.S)
            if not m:
                continue
            path, oid, title = m.groups()
            if oid in items:
                continue
            desc = re.findall(r"<li[^>]*>(.*?)</li>", li.split("ts-offer-list-item__description", 1)[-1], re.S)
            desc = [clean(strip_tags(d)) for d in desc]
            items[oid] = {
                "oid": oid,
                "url": AGVS + path,
                "title": clean(title),
                "published": parse_date_ch(desc[1] if len(desc) > 1 else ""),
                "employer": desc[2] if len(desc) > 2 else "",
            }
            new += 1
        total = re.search(r"\((\d+) Stellenangebote", s)
        if new == 0 or (total and len(items) >= int(total.group(1))):
            break
        time.sleep(0.4)
    return list(items.values())


AGVS_LABELS = [
    "Kennziffer", "Einsatzort", "Beschreibung der Stelle", "Funktion", "Schultyp", "Anstellung ab",
    "Befristung", "Anstellung bis", "Stellenprozent Minimum", "Stellenprozent Maximum",
    "Unterrichtspensum in Wochenlektionen (nur Lehrpersonen)", "Aufgaben", "Anforderungen",
    "Angebot", "Kontakt", "Einsatzort der Stelle", "Schulstandort", "Arbeitsort",
    "Diese Stellenangebote könnten Sie interessieren.",
]


def agvs_detail(url: str) -> dict:
    s = get_text(url)
    start = s.find("Allgemeine Informationen")
    text = strip_tags(s[start:] if start > 0 else s)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    fields: dict[str, str] = {}
    cur = None
    for l in lines:
        if l in AGVS_LABELS:
            cur = l
            fields.setdefault(cur, "")
            continue
        if cur:
            fields[cur] = (fields[cur] + "\n" + l).strip()
    return {
        "funktion": fields.get("Funktion", ""),
        "schultyp": fields.get("Schultyp", ""),
        "start": fields.get("Anstellung ab", ""),
        "end": fields.get("Anstellung bis", ""),
        "befristung": fields.get("Befristung", ""),
        "pmin": fields.get("Stellenprozent Minimum", ""),
        "pmax": fields.get("Stellenprozent Maximum", ""),
        "wl": fields.get("Unterrichtspensum in Wochenlektionen (nur Lehrpersonen)", ""),
        "aufgaben": fields.get("Aufgaben", "")[:2500],
        "standort": fields.get("Schulstandort", "") or fields.get("Einsatzort", ""),
        "arbeitsort": fields.get("Arbeitsort", "") or fields.get("Einsatzort", ""),
    }


def to_int(s) -> int | None:
    try:
        m = re.search(r"\d+", str(s))
        return int(m.group(0)) if m else None
    except Exception:  # noqa: BLE001
        return None


def source_agvs(cache: dict, geo: Geo) -> list[dict]:
    items = agvs_list()
    todo = [it for it in items if f"agvs-{it['oid']}" not in cache]
    with ThreadPoolExecutor(4) as ex:
        for it, det in zip(todo, ex.map(lambda i: safe(agvs_detail, i["url"]), todo)):
            if det:
                cache[f"agvs-{it['oid']}"] = det
    jobs = []
    for it in items:
        d = cache.get(f"agvs-{it['oid']}", {})
        title = it["title"]
        pmin, pmax = to_int(d.get("pmin")), to_int(d.get("pmax"))
        if pmax and not pmin:
            pmin = pmax
        if not pmax:
            pmin, pmax = parse_pensum(title)
        wl = to_int(d.get("wl")) or parse_lektionen(title)
        plz, place = find_plz(d.get("standort", "") + "\n" + d.get("arbeitsort", ""))
        if place:
            place = re.split(r"[,|]", place)[0].replace(", Schweiz", "").strip()
        place = place or d.get("arbeitsort", "").split("\n")[0]
        g = geo.lookup(plz, place, "AG")
        primary = f"{title} {d.get('funktion','')} {d.get('schultyp','')}"
        befr = d.get("befristung", "")
        jobs.append(make_job(
            id=f"agvs-{it['oid']}", source="Volksschule Aargau", url=it["url"], title=title,
            employer=it["employer"], place=place, plz=plz, canton="AG", geo=g,
            pmin=pmin, pmax=pmax, wl=wl,
            start=d.get("start"), end=d.get("end"),
            temporary=(befr.lower().startswith("befristet") if befr else None),
            substitute=bool(SUBST_RE.search(f"{title} {d.get('funktion','')}")),
            levels=classify(LEVELS, primary, d.get("aufgaben", "")),
            subjects=classify(SUBJECTS, title + " " + d.get("funktion", "")),
            role=role_of(title, d.get("funktion", "")),
            published=it["published"], text=d.get("aufgaben", ""),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Quelle 2: Kanton Aargau Stellenmarkt (Kantonsschulen / Berufsfachschulen)
# --------------------------------------------------------------------------- #

AGK = "https://www.ag.ch/io/jobs-proxy"
SCHOOL_NAME_RE = re.compile(
    r"((?:Neue |Alte )?Kantonsschule(?:\s+(?:am\s+)?[A-ZÄÖÜ][\wäöü-]+){1,2}"
    r"|Berufs(?:fach|bildungs)?schule\s+[A-ZÄÖÜ][\wäöü-]+"
    r"|[A-ZÄÖÜ][\wäöü-]*[Bb]erufs(?:fach)?schule\b(?:\s+[A-ZÄÖÜ][\wäöü-]+)?"
    r"|Berufsbildungszentrum\s+[A-ZÄÖÜ][\wäöü-]+|BBZ\s+[A-ZÄÖÜ][\wäöü-]+"
    r"|Berufs- und Weiterbildung\s+[A-ZÄÖÜ][\wäöü-]+|Wirtschaftsschule\s+[A-ZÄÖÜ][\wäöü-]+"
    r"|Kantonale Schule für Berufsbildung|Musikschule\s+[A-ZÄÖÜ][\wäöü-]+)")


def agk_detail(url: str) -> dict:
    s = get_text(url)
    text = strip_tags(s)
    m = SCHOOL_NAME_RE.search(text)
    i = text.find("Ihr Arbeitsumfeld")
    body = text[i:i + 3000] if i >= 0 else text[:3000]
    start = None
    for line in text.split("\n")[:400]:
        d = parse_date_ch(line)
        if d and re.search(r"\d{4}", line) and len(line) < 40:
            start = d
            break
    return {"school": m.group(1).strip() if m else "", "text": body, "start": start}


def source_agk(cache: dict, geo: Geo) -> list[dict]:
    data = get_json(f"{AGK}/jobs")
    jobs_raw = data.get("jobs", [])
    picked = []
    for x in jobs_raw:
        a = x.get("attributes", {})
        rub = " ".join(a.get("10", []) + a.get("15", []) + a.get("30", []))
        art = " ".join(a.get("50", []))
        title = x.get("title", "")
        if "Lehrstellen" in art or "Praktika" in art:
            continue
        if not (TEACH_RE.search(title) or LEAD_RE.search(title)) and "Bildung" not in rub:
            continue
        if "Bildung" not in rub and "Mittelschule" not in rub and not TEACH_RE.search(title):
            continue
        picked.append(x)
    todo = [x for x in picked if f"agk-{x['id']}" not in cache]
    with ThreadPoolExecutor(3) as ex:
        for x, det in zip(todo, ex.map(lambda j: safe(agk_detail, j["links"]["directlink"]), todo)):
            if det:
                cache[f"agk-{x['id']}"] = det
    jobs = []
    for x in picked:
        a = x.get("attributes", {})
        d = cache.get(f"agk-{x['id']}", {})
        title = clean(x.get("title"))
        sz = x.get("szas", {}) or {}
        pmin, pmax = to_int(sz.get("sza_pensum.min")), to_int(sz.get("sza_pensum.max"))
        tmin, tmax = parse_pensum(title)
        if tmax and (not pmax or pmax == 100 and tmax != 100):
            pmin, pmax = tmin, tmax
        place = (a.get("20") or [""])[0]
        g = geo.lookup(None, place, "AG")
        befr = " ".join(a.get("70", []))
        text = d.get("text", "")
        school = d.get("school") or ""
        primary = f"{title} {school}"
        lv = classify(LEVELS, primary, text)
        if not lv and "Mittelschule" in " ".join(a.get("30", [])):
            lv = ["Gymnasium / Mittelschule"] if "mittelschul" in title.lower() else ["Berufsfachschule"]
        jobs.append(make_job(
            id=f"agk-{x['id']}", source="Kanton Aargau", url=x["links"]["directlink"], title=title,
            employer=school or "Kanton Aargau", place=place, plz=None, canton="AG", geo=g,
            pmin=pmin, pmax=pmax, wl=parse_lektionen(title),
            start=d.get("start") or sz.get("sza_start_date"), end=None,
            temporary=("befristet" == befr.strip().lower()) if befr else None,
            substitute=bool(SUBST_RE.search(title)),
            levels=lv, subjects=classify(SUBJECTS, title),
            role=role_of(title), published=(x.get("startDate") or "")[:10] or None, text=text,
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Quelle 3: VSA-Stellenboerse Kanton Zuerich (Volksschule)
# --------------------------------------------------------------------------- #

VSA = "https://www.stellenboersevsa.ch"


def vsa_list(kind: str) -> list[dict]:
    s = get_text(f"{VSA}/public_suche.php?stellen_typ={kind}&suche=")
    out = []
    for row in re.findall(r"<tr>\s*<td class=\"export_item\".*?</tr>", s, re.S):
        m = re.search(r"stelle_id=(\d+)", row)
        if not m:
            continue
        tds = [clean(strip_tags(td)) for td in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        out.append({"sid": m.group(1), "kind": kind, "cells": tds})
    return out


def vsa_detail(sid: str) -> dict:
    s = get_text(f"{VSA}/public_suche.php?stelle_id={sid}")
    f: dict[str, str] = {}
    for dt, dd in re.findall(r'<dt class="label">(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', s, re.S):
        f[clean(strip_tags(dt))] = clean(strip_tags(dd))
    head = re.search(r"(Dauerstelle|Stellvertretung)\s+(\d+)", strip_tags(s))
    links = re.findall(r'href="(https?://[^"]+)"', s)
    apply = next((l for l in links if "publicjobs" in l or "bewerb" in l.lower()), None)
    return {"f": f, "nr": head.group(2) if head else "", "apply": apply}


def source_vsa(cache: dict, geo: Geo) -> list[dict]:
    rows = vsa_list("Dauerstelle") + vsa_list("Stellvertretung")
    todo = [r for r in rows if f"vsa-{r['sid']}" not in cache]
    def det(r):
        time.sleep(0.2)
        return safe(vsa_detail, r["sid"])
    with ThreadPoolExecutor(4) as ex:
        for r, d in zip(todo, ex.map(det, todo)):
            if d:
                cache[f"vsa-{r['sid']}"] = d
    jobs = []
    for r in rows:
        d = cache.get(f"vsa-{r['sid']}", {})
        f = d.get("f", {})
        c = r["cells"] + [""] * 8
        gemeinde = f.get("Schulgemeinde") or c[0]
        schulhaus = f.get("Schulhaus") or c[1]
        typ = f.get("Schulstufe und Schultyp") or c[2]
        klasse = f.get("Klasse") or c[3]
        faecher = f.get("Fächer", "")
        pens = f.get("Beschäftigungsgrad") or f.get("Pensum") or c[4]
        pmin, pmax = parse_pensum(pens)
        wl = parse_lektionen(f.get("Unterrichtspensum", "") or pens)
        if not pmax and wl:
            pmin = pmax = round(wl * 100 / 28)
        sub = r["kind"] == "Stellvertretung"
        title_bits = ["Stellvertretung" if sub else "Lehrperson", typ.split("/")[-1].strip()]
        if klasse and klasse.lower() not in typ.lower():
            title_bits.append(f"({klasse})")
        if faecher and faecher.lower() != "diverse":
            title_bits.append("– " + faecher)
        title = " ".join(b for b in title_bits if b)
        dauer = f.get("Dauer", "")
        g = geo.lookup(None, gemeinde, "ZH")
        primary = f"{typ} {klasse} {faecher}"
        jobs.append(make_job(
            id=f"vsa-{r['sid']}", source="Volksschule Zürich (VSA)",
            url=f"{VSA}/public_suche.php?stelle_id={r['sid']}", title=title,
            employer=(schulhaus if re.search(r"schul|kindergarten|\bkg\b", schulhaus, re.I) else f"Schulhaus {schulhaus}") if schulhaus else f"Schule {gemeinde}",
            place=gemeinde, plz=None, canton="ZH", geo=g,
            pmin=pmin, pmax=pmax, wl=wl,
            start=f.get("Antrittsdatum") or (c[5] if len(c) > 5 else None), end=dauer or None,
            temporary=True if sub else None, substitute=sub,
            levels=classify(LEVELS, primary) or (["Schulleitung"] if "schulleitung" in typ.lower() else []),
            subjects=classify(SUBJECTS, f"{faecher} {klasse}"),
            role="Schulleitung" if "schulleitung" in typ.lower() else "Lehrperson",
            published=parse_date_ch(f.get("Erfassungsdatum")), text=f"{typ} · {klasse} · Fächer: {faecher or '—'} · {dauer}",
            apply=d.get("apply"),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Quelle 4: job-room.ch (Stellenboerse des Bundes / arbeit.swiss)
# --------------------------------------------------------------------------- #

JR = "https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search"


def jobroom_search(keyword: str) -> list[dict]:
    out = []
    page = 0
    while True:
        body = json.dumps({
            "workloadPercentageMin": 0, "workloadPercentageMax": 100, "permanent": None,
            "onlineSince": 60, "displayRestricted": False, "keywords": [keyword],
            "professionCodes": [], "communalCodes": [], "cantonCodes": JOBROOM_CANTONS,
        }).encode()
        raw, headers = http(f"{JR}?page={page}&size=100&sort=date_desc", data=body,
                            headers={"Content-Type": "application/json", "Accept": "application/json"})
        batch = json.loads(raw)
        out.extend(batch)
        total = int({k.lower(): v for k, v in headers.items()}.get("x-total-count", "0") or 0)
        page += 1
        if not batch or page * 100 >= total or page >= 10:
            break
        time.sleep(0.5)
    return out


def source_jobroom(cache: dict, geo: Geo) -> list[dict]:
    seen: dict[str, dict] = {}
    for kw in JOBROOM_KEYWORDS:
        for x in jobroom_search(kw):
            ad = x.get("jobAdvertisement", x)
            seen.setdefault(ad["id"], ad)
        time.sleep(0.4)
    jobs = []
    for jid, ad in seen.items():
        c = ad.get("jobContent", {})
        descs = c.get("jobDescriptions") or [{}]
        de = next((d for d in descs if d.get("languageIsoCode") == "de"), descs[0])
        title = clean(de.get("title"))
        desc = clean(de.get("description"))
        comp = c.get("company") or {}
        employer = clean(comp.get("name"))
        role = role_of(title)
        if role not in ("Lehrperson", "Schulleitung"):
            continue
        if role == "Schulleitung" and not re.search(r"schul", f"{title} {employer}", re.I):
            continue
        loc = c.get("location") or {}
        coords = loc.get("coordinates") or {}
        g = None
        if coords.get("lat") and coords.get("lon"):
            g = (float(coords["lat"]), float(coords["lon"]), loc.get("cantonCode") or "")
        else:
            g = geo.lookup(loc.get("postalCode"), loc.get("city"), loc.get("cantonCode"))
        emp = c.get("employment") or {}
        pmin, pmax = to_int(emp.get("workloadPercentageMin")), to_int(emp.get("workloadPercentageMax"))
        perm = emp.get("permanent")
        start = emp.get("startDate") or ("sofort" if emp.get("immediately") else None)
        url = c.get("externalUrl") or f"https://www.job-room.ch/job-search/{jid}"
        if url and "utm_" in url:
            url = url.split("?")[0]
        primary = f"{title} {employer}"
        pub = (ad.get("publication") or {}).get("startDate") or ad.get("createdTime") or ""
        jobs.append(make_job(
            id=f"jr-{jid}", source="job-room.ch", url=url, title=title, employer=employer,
            place=loc.get("city") or "", plz=loc.get("postalCode"), canton=loc.get("cantonCode") or "",
            geo=g, pmin=pmin, pmax=pmax, wl=parse_lektionen(title),
            start=start, end=emp.get("endDate"),
            temporary=(None if perm is None else not perm),
            substitute=bool(SUBST_RE.search(title)),
            levels=classify(LEVELS, primary, desc), subjects=classify(SUBJECTS, title),
            role=role, published=pub[:10] or None, text=desc,
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Zusammenfuehren
# --------------------------------------------------------------------------- #

def make_job(*, id, source, url, title, employer, place, plz, canton, geo, pmin, pmax, wl,
             start, end, temporary, substitute, levels, subjects, role, published, text, apply=None) -> dict:
    if pmax is None and wl:
        pmin = pmax = min(100, round(wl * 100 / 28))
    if pmin is not None and pmax is not None and pmin > pmax:
        pmin, pmax = pmax, pmin
    start_iso = parse_date_ch(start) if isinstance(start, str) else None
    if start_iso is None and isinstance(start, str) and re.search(r"sofort", start, re.I):
        start_iso = "sofort"
    end_iso = parse_date_ch(end) if isinstance(end, str) else None
    return {
        "id": id, "source": source, "url": url, "apply": apply,
        "title": title, "employer": clean(employer), "place": clean(place), "plz": plz,
        "canton": (geo[2] if geo and geo[2] else canton) or canton,
        "lat": round(geo[0], 4) if geo else None, "lon": round(geo[1], 4) if geo else None,
        "pmin": pmin, "pmax": pmax, "wl": wl,
        "start": start_iso, "end": end_iso or (clean(end)[:60] if isinstance(end, str) and end else None),
        "temporary": temporary, "substitute": substitute,
        "levels": levels, "subjects": subjects,
        "subjects_text": mentioned(SUBJECTS, text or "", subjects), "role": role,
        "published": published, "text": snippet(text or ""),
    }


def norm_key(j: dict) -> str:
    t = re.sub(r"\(.*?\)|\d+\s*-?\s*\d*\s*%|[^a-zäöü]+", " ", j["title"].lower())
    return " ".join(t.split()) + "|" + (j.get("place") or "").lower()[:12]


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        print(f"  ! {fn.__name__}{args[:1]}: {e}", file=sys.stderr)
        return None


def main() -> int:
    geo = Geo(PLZ_FILE)
    previous = {}
    prev_meta = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            previous = {j["id"]: j for j in prev.get("jobs", [])}
            prev_meta = {s["name"]: s for s in prev.get("sources", [])}
        except Exception:  # noqa: BLE001
            pass
    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}

    sources = [
        ("Volksschule Aargau", "agvs", source_agvs, "https://www.schulen-aargau.ch/stellen"),
        ("Kanton Aargau", "agk", source_agk, "https://www.ag.ch/stellen"),
        ("Volksschule Zürich (VSA)", "vsa", source_vsa, "https://www.stellenboersevsa.ch/public_index.php"),
        ("job-room.ch", "jr", source_jobroom, "https://www.job-room.ch"),
    ]
    all_jobs: list[dict] = []
    meta = []
    for name, prefix, fn, home in sources:
        t0 = time.time()
        try:
            jobs = fn(cache, geo)
            if not jobs and any(k.startswith(prefix + "-") for k in previous):
                raise RuntimeError("Quelle lieferte 0 Stellen (vermutlich Seite geändert)")
            ok, err = True, None
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            jobs = [j for k, j in previous.items() if k.startswith(prefix + "-")]
            ok, err = False, str(e)[:300]
        print(f"{name}: {len(jobs)} Stellen ({time.time() - t0:.0f}s){'' if ok else ' FEHLER: ' + err}")
        meta.append({"name": name, "url": home, "count": len(jobs), "ok": ok, "error": err,
                     "last_ok": NOW.isoformat(timespec="minutes") if ok else prev_meta.get(name, {}).get("last_ok")})
        all_jobs.extend(jobs)

    # Doppelte (gleiche Stelle aus zwei Quellen) entfernen - offizielle Quellen bevorzugen
    out, keys = [], set()
    for j in all_jobs:
        k = norm_key(j)
        if k in keys:
            continue
        keys.add(k)
        old = previous.get(j["id"])
        if old:
            j["first_seen"] = old.get("first_seen", TODAY)
        elif not previous:
            # allererster Lauf: Inseratsdatum verwenden, sonst waere alles "neu"
            j["first_seen"] = j.get("published") if (j.get("published") or "") <= TODAY and j.get("published") else TODAY
        else:
            j["first_seen"] = TODAY
        out.append(j)

    out.sort(key=lambda j: (j.get("first_seen") or "", j.get("published") or ""), reverse=True)

    # Cache auf aktuelle Stellen beschraenken
    live = {j["id"] for j in out} | {j["id"] for j in all_jobs}
    cache = {k: v for k, v in cache.items() if k in live}
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "updated": NOW.isoformat(timespec="minutes"),
        "sources": meta,
        "jobs": out,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    if not PLZ_OUT_FILE.exists():
        plz = {p: [round(v[0], 3), round(v[1], 3)] for p, v in geo.by_plz.items()}
        PLZ_OUT_FILE.write_text(json.dumps(plz, separators=(",", ":")), encoding="utf-8")

    print(f"Total: {len(out)} Stellen -> {OUT_FILE}")
    return 0 if any(m["ok"] for m in meta) else 1


if __name__ == "__main__":
    sys.exit(main())
