"""THEUNIT offline search index (spec 3.10, phase 8c).

Reads named OpenStreetMap features (osmium export -f geojsonseq) and writes a SQLite file with an FTS5 table
the app searches offline: towns, streets, places and POIs with their Arabic / French / English names.

    python3 search_index.py named.geojsonseq tunisia-YYYYMMDD.search.sqlite

Data: (c) OpenStreetMap contributors, ODbL.
"""
import json
import math
import sqlite3
import sys

PLACE_RANK = {'city': 0, 'town': 1, 'suburb': 2, 'village': 3, 'quarter': 4, 'neighbourhood': 4,
              'hamlet': 5, 'locality': 5, 'island': 5}
ROADS = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
         'living_street', 'service', 'pedestrian', 'motorway_link', 'trunk_link', 'primary_link',
         'secondary_link', 'tertiary_link', 'road'}
STREET_RANK = 6
POI_RANK = 7
TOWN_KINDS = {'city', 'town', 'village', 'suburb'}


_AR_MARKS = {chr(c) for c in range(0x064B, 0x0653)} | {'ٰ', 'ـ'}   # tashkeel, superscript alef, tatweel
_AR_MAP = str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ى': 'ي',
                         'ة': 'ه'})


def ar_norm(s):
    """Arabic search form (the app does the same to the query): no vowel marks, one alef / ya / ha,
    no "al-" (ال) at the start of a word. "شارع الحبيب" -> "شارع حبيب"."""
    if not s:
        return None
    s = ''.join(ch for ch in s if ch not in _AR_MARKS).translate(_AR_MAP)
    words = [w[2:] if w.startswith('ال') and len(w) > 3 else w for w in s.split()]
    return ' '.join(words)


def point_of(geom):
    """A representative point: the point, the middle vertex of a line, the average of a ring."""
    t, c = geom['type'], geom['coordinates']
    if t == 'Point':
        return c
    if t == 'LineString':
        return c[len(c) // 2]
    if t == 'MultiLineString':
        return c[0][len(c[0]) // 2]
    if t == 'Polygon':
        ring = c[0]
    elif t == 'MultiPolygon':
        ring = c[0][0]
    else:
        return None
    n = len(ring)
    return [sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n]


def kind_of(p):
    if p.get('place') in PLACE_RANK:
        return p['place'], PLACE_RANK[p['place']]
    if p.get('highway') in ROADS:
        return 'street', STREET_RANK
    for k in ('amenity', 'shop', 'tourism', 'leisure', 'aeroway', 'railway'):
        if p.get(k):
            return f'{k}:{p[k]}', POI_RANK
    return None, None


def main(src, dst):
    rows = []
    streets = {}                                   # (lower name, 1 km cell) -> row index: one row per street
    with open(src, encoding='utf-8') as f:
        for line in f:
            line = line.strip().lstrip('\x1e')
            if not line:
                continue
            feat = json.loads(line)
            p = feat.get('properties') or {}
            names = [p.get('name'), p.get('name:fr'), p.get('name:ar'), p.get('name:en')]
            if not any(names):
                continue
            kind, rank = kind_of(p)
            if kind is None:
                continue
            pt = point_of(feat.get('geometry') or {})
            if not pt:
                continue
            lng, lat = float(pt[0]), float(pt[1])
            if kind == 'street':
                key = ((names[0] or names[1] or names[2] or names[3]).lower(), round(lat, 2), round(lng, 2))
                if key in streets:
                    continue
                streets[key] = len(rows)
            rows.append([kind, rank, *names, lat, lng, None, ar_norm(names[2] or names[0])])

    # Context for streets and POIs: the nearest town (0.1 degree grid, ~15 km).
    towns = {}
    for r in rows:
        if r[0] in TOWN_KINDS:
            towns.setdefault((int(r[6] * 10), int(r[7] * 10)), []).append(r)
    for r in rows:
        if r[1] < STREET_RANK:
            continue
        best, bd = None, 1e9
        cy, cx = int(r[6] * 10), int(r[7] * 10)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for t in towns.get((cy + dy, cx + dx), ()):
                    d = (t[6] - r[6]) ** 2 + ((t[7] - r[7]) * math.cos(math.radians(r[6]))) ** 2
                    if d < bd:
                        best, bd = t, d
        if best:
            r[8] = best[3] or best[2] or best[4]

    db = sqlite3.connect(dst)
    db.executescript('''
        DROP TABLE IF EXISTS place; DROP TABLE IF EXISTS place_fts;
        CREATE TABLE place (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, rank INTEGER NOT NULL, name TEXT,
                            name_fr TEXT, name_ar TEXT, name_en TEXT, lat REAL NOT NULL, lng REAL NOT NULL, town TEXT,
                            ar TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, name_fr, name_ar, name_en, ar, content='place',
                            content_rowid='id', tokenize='unicode61 remove_diacritics 2', prefix='2 3');
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    ''')
    db.executemany('INSERT INTO place (kind, rank, name, name_fr, name_ar, name_en, lat, lng, town, ar) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
    db.execute("INSERT INTO place_fts(place_fts) VALUES ('rebuild')")
    db.executemany('INSERT INTO meta VALUES (?, ?)', [('version', '1'), ('rows', str(len(rows))),
                                                      ('attribution', '(c) OpenStreetMap contributors, ODbL')])
    db.commit()
    db.execute('VACUUM')
    db.close()
    by = {}
    for r in rows:
        k = r[0].split(':')[0] if r[1] == POI_RANK else ('street' if r[0] == 'street' else 'place')
        by[k] = by.get(k, 0) + 1
    print(f'{len(rows)} rows: {by}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
