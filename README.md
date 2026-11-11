# Lehrerstellen

Persönliche Übersicht offener Lehrerstellen rund um Neuenhof/Spreitenbach (AG/ZH),
mit Filtern für Fach, Schulstufe, Pensum, Stellvertretungen und Umkreis.

- **Webseite:** `docs/index.html` (wird über GitHub Pages veröffentlicht)
- **Daten:** `docs/jobs.json`, automatisch erzeugt von `scraper/fetch_jobs.py`
- **Automatik:** `.github/workflows/update.yml` läuft zweimal täglich auf GitHub

## Quellen

| Quelle | Inhalt |
|---|---|
| schulen-aargau.ch/stellen | Volksschule Aargau (Kindergarten bis Bezirksschule, Stellvertretungen) |
| ag.ch/stellen | Kanton Aargau: Kantonsschulen, Berufsfachschulen |
| stellenboersevsa.ch | Volksschule Kanton Zürich (Dauerstellen + Stellvertretungen) |
| sonderpaedagogisches.stellenboersevsa.ch | Sonderschulen Kanton Zürich (Unterricht) |
| job-room.ch | Stellenbörse des Bundes (AG, ZH, LU, ZG, SO, BL, BS, SZ) |
| jobs.ch | Öffentliche Such-API, Regionen Zürich und Aargau |
| jobup.ch | Gleiche Plattform wie jobs.ch, nur Stellen aus AG, ZH, LU, ZG, SO, BL, BS, SZ |
| sozjobs.ch | Sozial- und Bildungsbereich (gleiche Kantone) |
| publicjobs.ch | Gemeinden, Städte, Kantone (gleiche Kantone) |

## Lokal testen

```
python scraper/fetch_jobs.py
cd docs && python -m http.server 8000
```

Dann http://localhost:8000 öffnen.

## Manuell aktualisieren

Auf GitHub: *Actions → Stellen aktualisieren → Run workflow*.
