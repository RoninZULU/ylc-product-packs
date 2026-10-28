#!/usr/bin/env python3
"""Paquetes de productos por país para Your Last Coach.

De la exportación diaria de Open Food Facts (CSV) se quedan, por país, los
productos con tabla nutricional que más se han escaneado allí este año y el
anterior (`popularity_tags`: top-N-<país>-scans-<año>). En EE. UU. se añaden
los productos de USDA FoodData Central (Branded Foods) que Open Food Facts no
tiene con tabla.

Cada paquete es una base SQLite comprimida (`<país>.sqlite.gz`) con una fila
por código de barras, y `index.json` describe la última versión. La app los
baja con wifi y los cambia enteros cada mes: nunca se acumulan.

Licencias: lo que viene de Open Food Facts es ODbL 1.0 (contenido DbCL 1.0);
lo de USDA, dominio público (CC0).

Uso:
  python build_packs.py --off products.csv.gz --usda branded.zip --out dist
"""
import argparse
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import zipfile

csv.field_size_limit(2**31 - 1)

# Países con paquete: su etiqueta en Open Food Facts, su código en
# `popularity_tags` y el máximo de productos de Open Food Facts.
COUNTRIES = {
    'es': {'tag': 'en:spain', 'cc': 'es', 'max': 60000, 'usda': False},
    'us': {'tag': 'en:united-states', 'cc': 'us', 'max': 60000, 'usda': True},
}

# Puestos de popularidad, del mejor al peor.
BUCKETS = ['top-1000', 'top-5000', 'top-10000', 'top-50000', 'top-100000']

# Las únicas categorías que mira la app (off_parser.dart: tipo de alimento y
# si es líquido; micro_estimate.dart: enriquecidos). El resto no se guarda.
KEEP_TAGS = set('''
alcoholic-beverages beverages biscuits biscuits-and-cakes bodybuilding-supplements
breads breakfast-cereals burgers butters cakes candies canned-fishes
canned-vegetables cereal-grains cereals-and-their-products cheeses chips
chocolates coffees compotes condiments confectioneries crisps dairies desserts
dietary-supplements dressings dried-fruits eggs fast-foods fats
fermented-milk-products fishes flours frozen-vegetables fruit-juices fruits hams
hot-dogs ice-creams juices ketchup legumes legumes-and-their-products margarines
mayonnaises meals meats milks nut-butters nuts olive-oils pastas pizzas
plant-based-milk-alternatives plant-milks potatoes poultries prepared-meals
prepared-meats protein-powders pulses rices salty-snacks sandwiches sauces
sausages seafood seeds sodas soups sweet-snacks sweet-spreads teas tofu tunas
vegetable-oils vegetables waters yogurts
dairy-substitutes nectars fruit-nectars protein-bars meal-replacements
baby-foods baby-milks infant-formulas energy-drinks sports-drinks
'''.split())

SCHEMA = '''
CREATE TABLE p(
  code TEXT PRIMARY KEY,
  src INTEGER NOT NULL,     -- 0 Open Food Facts · 1 USDA
  name TEXT, brand TEXT, qty TEXT, serving TEXT, serving_g INTEGER,
  -- Números enteros escalados (ver SCALE): ocupan 1-3 bytes y no 8.
  kcal INTEGER, fat INTEGER, sat INTEGER, carbs INTEGER, sugars INTEGER,
  fiber INTEGER, prot INTEGER, salt INTEGER,
  vitc INTEGER, ca INTEGER, fe INTEGER, mg INTEGER, k INTEGER,
  fv INTEGER,               -- % de fruta, verdura y frutos secos
  ns TEXT, nova INTEGER,
  adds TEXT, cats TEXT, pnns TEXT, alg TEXT, trc TEXT, img TEXT
) WITHOUT ROWID;
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
'''
COLUMNS = ['code', 'src', 'name', 'brand', 'qty', 'serving', 'serving_g',
           'kcal', 'fat', 'sat', 'carbs', 'sugars', 'fiber', 'prot', 'salt',
           'vitc', 'ca', 'fe', 'mg', 'k', 'fv', 'ns', 'nova',
           'adds', 'cats', 'pnns', 'alg', 'trc', 'img']

IMG_PREFIX = 'https://images.openfoodfacts.org/images/products/'

# Cada número se guarda multiplicado por esto y redondeado (por 100 g; las
# vitaminas y minerales, en gramos como en Open Food Facts).
SCALE = {
    'serving_g': 10, 'kcal': 10, 'fat': 100, 'sat': 100, 'carbs': 100,
    'sugars': 100, 'fiber': 100, 'prot': 100, 'salt': 1000,
    'vitc': 100000, 'ca': 100000, 'fe': 1000000, 'mg': 100000, 'k': 100000,
    'fv': 10,
}


def image_path(code):
    """La carpeta de las imágenes de un código en Open Food Facts."""
    if len(code) <= 8:
        return code
    return f'{code[:3]}/{code[3:6]}/{code[6:9]}/{code[9:]}'


def num(v, digits=2):
    if v is None or v == '':
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    if x != x or x < 0 or x > 100000:
        return None
    return round(x, digits)


def text(v, limit=90):
    v = (v or '').strip()
    return v[:limit] if v else None


def valid_code(code):
    """EAN-8, EAN-13 o UPC-A (que pasa a EAN-13 con un cero), con su dígito de control."""
    code = re.sub(r'\D', '', code or '')
    if len(code) == 14 and code.startswith('0'):
        code = code[1:]
    if len(code) == 12:
        code = '0' + code
    if len(code) not in (8, 13) or code.startswith('0000000'):
        # Los que empiezan por siete ceros son códigos internos, no de tienda.
        return None
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if (len(body) - i) % 2 == 1 else 1) for i, d in enumerate(body))
    return code if (10 - total % 10) % 10 == check else None


def sane(kcal, prot, carbs, fat):
    """Descarta tablas imposibles: kJ apuntados como kcal, macros que no caben."""
    if None in (kcal, prot, carbs, fat):
        return False
    if kcal > 950 or prot + carbs + fat > 105:
        return False
    computed = 4 * prot + 4 * carbs + 9 * fat
    if max(kcal, computed) > 60 and abs(computed - kcal) / max(kcal, computed) > 0.5:
        return False
    return True


def tags(v, keep=None):
    out = []
    for t in (v or '').split(','):
        t = t.strip()
        if t.startswith('en:'):
            t = t[3:]
        if t and (keep is None or t in keep) and t not in out:
            out.append(t)
    return ','.join(out) if out else None


def rank(popularity, cc, years):
    """El mejor puesto del producto en el país estos años (0 = mejor); None si nada."""
    best = None
    for t in popularity.split(','):
        if f'-{cc}-scans-' not in t:
            continue
        b, _, year = t.partition(f'-{cc}-scans-')
        if year not in years or b not in BUCKETS:
            continue
        i = BUCKETS.index(b)
        best = i if best is None else min(best, i)
    return best


def read_off(path, years):
    """Una pasada por el CSV: los candidatos de cada país y todos los códigos con tabla."""
    picked = {c: [] for c in COUNTRIES}
    with_table = set()
    rows = 0
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as f:
        reader = csv.reader(f, delimiter='\t', quoting=csv.QUOTE_NONE)
        head = next(reader)
        col = {name: i for i, name in enumerate(head)}

        def g(row, name):
            i = col.get(name)
            return row[i] if i is not None and i < len(row) else ''

        for row in reader:
            rows += 1
            if rows % 500000 == 0:
                print(f'  OFF: {rows:,} filas', file=sys.stderr)
            code = valid_code(g(row, 'code'))
            if code is None:
                continue
            kcal = num(g(row, 'energy-kcal_100g'))
            if kcal is None:
                kj = num(g(row, 'energy_100g'))
                kcal = round(kj / 4.184, 1) if kj is not None else None
            prot, carbs, fat = num(g(row, 'proteins_100g')), num(g(row, 'carbohydrates_100g')), num(g(row, 'fat_100g'))
            if not sane(kcal, prot, carbs, fat):
                continue
            with_table.add(code)
            popularity = g(row, 'popularity_tags')
            if not popularity:
                continue
            ranks = {c: rank(popularity, cfg['cc'], years) for c, cfg in COUNTRIES.items()}
            if all(r is None for r in ranks.values()):
                continue
            name = text(g(row, 'product_name')) or text(g(row, 'abbreviated_product_name')) or text(g(row, 'generic_name'))
            if name is None:
                continue
            salt = num(g(row, 'salt_100g'), 3)
            if salt is None:
                sodium = num(g(row, 'sodium_100g'), 4)
                salt = round(sodium * 2.5, 3) if sodium is not None else None
            img = g(row, 'image_small_url')
            nova = num(g(row, 'nova_group'), 0)
            ns = (g(row, 'nutriscore_grade') or '').strip().lower()
            rec = {
                'code': code,
                'src': 0,
                'name': name,
                'brand': text((g(row, 'brands') or '').split(',')[0], 50),
                'qty': text(g(row, 'quantity'), 40),
                'serving': text(g(row, 'serving_size'), 40),
                'serving_g': num(g(row, 'serving_quantity'), 1),
                'kcal': kcal, 'fat': fat, 'sat': num(g(row, 'saturated-fat_100g')),
                'carbs': carbs, 'sugars': num(g(row, 'sugars_100g')),
                'fiber': num(g(row, 'fiber_100g')), 'prot': prot, 'salt': salt,
                'vitc': num(g(row, 'vitamin-c_100g'), 5), 'ca': num(g(row, 'calcium_100g'), 5),
                'fe': num(g(row, 'iron_100g'), 6), 'mg': num(g(row, 'magnesium_100g'), 5),
                'k': num(g(row, 'potassium_100g'), 5),
                'fv': num(g(row, 'fruits-vegetables-nuts-estimate-from-ingredients_100g'), 1)
                      or num(g(row, 'fruits-vegetables-legumes_100g'), 1),
                'ns': ns if ns in ('a', 'b', 'c', 'd', 'e') else None,
                'nova': int(nova) if nova in (1, 2, 3, 4) else None,
                'adds': tags(g(row, 'additives_tags')),
                'cats': tags(g(row, 'categories_tags'), KEEP_TAGS),
                'pnns': text('|'.join(x for x in (g(row, 'pnns_groups_1'), g(row, 'pnns_groups_2')) if x and x != 'unknown'), 60),
                'alg': tags(g(row, 'allergens')),
                'trc': tags(g(row, 'traces_tags')),
                'img': short_image(code, img),
            }
            try:
                scans = int(g(row, 'unique_scans_n') or 0)
            except ValueError:
                scans = 0
            for c, r in ranks.items():
                if r is not None:
                    picked[c].append((r, -scans, rec))
    print(f'  OFF: {rows:,} filas, {len(with_table):,} con tabla', file=sys.stderr)
    return picked, with_table


# Categorías de USDA (branded_food_category) → las de Open Food Facts que mira
# la app. Por orden y por inicio de palabra: gana la primera que encaja. Vacío,
# sin categoría (bebidas en polvo o aditivos de la leche: no son líquidos).
USDA_CATEGORIES = [
    ('non alcoholic', 'beverages'),
    ('powdered drink', ''),
    ('milk additive', ''),
    ('juice', 'beverages,juices,fruit-juices'),
    ('nectar', 'beverages,juices,nectars'),
    ('plant based milk', 'beverages,plant-based-milk-alternatives'),
    ('milk/milk substitutes', 'dairies,milks'),
    ('milk substitute', 'beverages,plant-based-milk-alternatives'),
    ('soda', 'beverages,sodas'),
    ('soft drink', 'beverages,sodas'),
    ('energy drink', 'beverages,energy-drinks'),
    ('sport drink', 'beverages,sports-drinks'),
    ('water', 'beverages,waters'),
    ('coffee', 'beverages,coffees'),
    ('tea', 'beverages,teas'),
    ('alcohol', 'beverages,alcoholic-beverages'),
    ('beer', 'beverages,alcoholic-beverages'),
    ('wine', 'beverages,alcoholic-beverages'),
    ('drink', 'beverages'),
    ('ice cream', 'desserts,ice-creams'),
    ('frozen dessert', 'desserts,ice-creams'),
    ('ketchup', 'sauces,ketchup'),
    ('dressing', 'sauces,dressings'),
    ('mayonnaise', 'sauces,mayonnaises'),
    ('dessert', 'desserts'),
    ('pudding', 'desserts'),
    ('gelatin', 'desserts'),
    ('sauce', 'sauces'),
    ('dip', 'sauces'),
    ('gravy', 'sauces'),
    ('condiment', 'condiments'),
    ('yogurt', 'dairies,yogurts'),
    ('cheese', 'dairies,cheeses'),
    ('milk', 'dairies,milks'),
    ('cream', 'dairies'),
    ('nut & seed butter', 'nut-butters'),
    ('peanut butter', 'nut-butters'),
    ('nut butter', 'nut-butters'),
    ('butter', 'fats,butters'),
    ('margarine', 'fats,margarines'),
    ('oil', 'fats,vegetable-oils'),
    ('jam', 'sweet-spreads'),
    ('honey', 'sweet-spreads'),
    ('protein bar', 'protein-bars'),
    ('energy & granola', 'sweet-snacks'),
    ('granola bar', 'sweet-snacks'),
    ('supplement', 'dietary-supplements'),
    ('protein', 'protein-powders'),
    ('cereal', 'breakfast-cereals'),
    ('bread', 'breads'),
    ('noodle', 'pastas'),
    ('pasta', 'pastas'),
    ('rice', 'rices'),
    ('flour', 'flours'),
    ('pizza', 'pizzas'),
    ('soup', 'soups'),
    ('chili', 'meals'),
    ('sandwich', 'sandwiches'),
    ('deli salad', 'meals'),
    ('frozen dinner', 'meals,prepared-meals'),
    ('entree', 'meals,prepared-meals'),
    ('combination meal', 'meals,prepared-meals'),
    ('wraps', 'meals,prepared-meals'),
    ('sausage', 'meats,sausages'),
    ('hotdog', 'meats,hot-dogs'),
    ('hot dog', 'meats,hot-dogs'),
    ('bacon', 'meats,prepared-meats'),
    ('pepperoni', 'meats,prepared-meats'),
    ('cold cut', 'meats,prepared-meats'),
    ('deli', 'meats,prepared-meats'),
    ('patties', 'meats,burgers'),
    ('burger', 'meats,burgers'),
    ('ham', 'meats,hams'),
    ('chicken', 'meats,poultries'),
    ('turkey', 'meats,poultries'),
    ('poultry', 'meats,poultries'),
    ('meat', 'meats'),
    ('tuna', 'fishes,tunas'),
    ('fish', 'fishes'),
    ('seafood', 'seafood'),
    ('egg', 'eggs'),
    ('cookie', 'sweet-snacks,biscuits'),
    ('biscuit', 'sweet-snacks,biscuits'),
    ('pancake', 'sweet-snacks,cakes'),
    ('cake', 'sweet-snacks,cakes'),
    ('pastr', 'sweet-snacks,cakes'),
    ('sweet bakery', 'sweet-snacks,cakes'),
    ('chocolate', 'sweet-snacks,chocolates'),
    ('candy', 'sweet-snacks,candies'),
    ('chewing gum', 'sweet-snacks,candies'),
    ('confection', 'sweet-snacks,confectioneries'),
    ('cracker', 'salty-snacks'),
    ('chips', 'salty-snacks,chips'),
    ('crisps', 'salty-snacks,crisps'),
    ('popcorn', 'salty-snacks'),
    ('pretzel', 'salty-snacks'),
    ('snack', 'salty-snacks'),
    ('nut', 'nuts'),
    ('seed', 'seeds'),
    ('lentil', 'legumes'),
    ('bean', 'legumes'),
    ('legume', 'legumes'),
    ('tofu', 'tofu'),
    ('french fries', 'potatoes'),
    ('potato', 'potatoes'),
    ('tomato', 'vegetables'),
    ('pickle', 'vegetables'),
    ('vegetable', 'vegetables'),
    ('fruit', 'fruits'),
]


def usda_tags(category):
    c = (category or '').lower()
    for key, value in USDA_CATEGORIES:
        if re.search(r'\b' + re.escape(key), c):
            return value or None
    return None


# Nutrientes de USDA: id → (columna, factor a g por 100 g).
USDA_NUTRIENTS = {
    1008: ('kcal', 1), 1003: ('prot', 1), 1004: ('fat', 1), 1005: ('carbs', 1),
    2000: ('sugars', 1), 1079: ('fiber', 1), 1258: ('sat', 1), 1093: ('sodium', 0.001),
    1162: ('vitc', 0.001), 1087: ('ca', 0.001), 1089: ('fe', 0.001),
    1090: ('mg', 0.001), 1092: ('k', 0.001),
}


def nice(s):
    """«ORGANIC PINEAPPLE JUICE» → «Organic pineapple juice». Lo que ya viene bien, igual."""
    s = re.sub(r'\s+', ' ', (s or '').strip())
    if not s:
        return None
    if s.upper() == s:
        s = s.lower()
        s = s[0].upper() + s[1:]
    return s[:90]


def brand_case(s):
    s = re.sub(r'\s+', ' ', (s or '').strip())
    if not s:
        return None
    if s.upper() == s:
        s = ' '.join(w.capitalize() for w in s.lower().split(' '))
    return s[:50]


def read_usda(path, skip_codes, since):
    """Productos de USDA Branded vigentes, desde [since], que no están ya con tabla."""
    with zipfile.ZipFile(path) as z:
        names = {os.path.basename(n): n for n in z.namelist()}

        def rows(name):
            with z.open(names[name]) as raw:
                yield from csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8', newline=''))

        cands = {}
        by_code = {}
        for r in rows('branded_food.csv'):
            if (r.get('discontinued_date') or '').strip():
                continue
            date = max((r.get('modified_date') or ''), (r.get('available_date') or ''))[:10]
            if date < since:
                continue
            code = valid_code(r.get('gtin_upc'))
            if code is None or code in skip_codes:
                continue
            fdc = int(r['fdc_id'])
            prev = by_code.get(code)
            if prev is not None:
                if (cands[prev]['date'], prev) >= (date, fdc):
                    continue
                del cands[prev]
            by_code[code] = fdc
            unit = (r.get('serving_size_unit') or '').strip().lower()
            size = num(r.get('serving_size'), 1)
            liquid = unit in ('ml', 'mlt')
            household = (r.get('household_serving_fulltext') or '').strip()
            u = 'ml' if liquid else 'g'
            serving = None
            if household and re.search(r'\d\s*(g|ml)\b', household, re.I):
                serving = household
            elif size:
                serving = f'{household} ({size:g} {u})' if household else f'{size:g} {u}'
            cats = usda_tags(r.get('branded_food_category'))
            cands[fdc] = {
                'date': date,
                'code': code,
                'src': 1,
                'brand': brand_case(r.get('brand_name') or r.get('brand_owner')),
                'qty': text(r.get('package_weight'), 40),
                'serving': text(serving, 40),
                'serving_g': size if unit in ('g', 'grm', 'ml', 'mlt') else None,
                'cats': cats,
            }
        print(f'  USDA: {len(cands):,} candidatos', file=sys.stderr)

        for r in rows('food.csv'):
            c = cands.get(int(r['fdc_id']))
            if c is not None:
                c['name'] = nice(r.get('description'))

        n = 0
        for r in rows('food_nutrient.csv'):
            n += 1
            if n % 5000000 == 0:
                print(f'  USDA: {n:,} nutrientes', file=sys.stderr)
            c = cands.get(int(r['fdc_id']))
            if c is None:
                continue
            spec = USDA_NUTRIENTS.get(int(r['nutrient_id']))
            if spec is None:
                continue
            v = num(r.get('amount'), 6)
            if v is not None:
                c[spec[0]] = round(v * spec[1], 6)

    out = []
    for c in cands.values():
        sodium = c.pop('sodium', None)
        c['salt'] = round(sodium * 2.5, 3) if sodium is not None else None
        c.pop('date', None)
        if not c.get('name') or not sane(c.get('kcal'), c.get('prot'), c.get('carbs'), c.get('fat')):
            continue
        out.append(c)
    print(f'  USDA: {len(out):,} con tabla válida', file=sys.stderr)
    return out


def short_image(code, url):
    """Sólo el nombre del archivo si está en su carpeta de siempre."""
    if not url.startswith(IMG_PREFIX):
        return None
    rest = url[len(IMG_PREFIX):]
    folder = image_path(code) + '/'
    return rest[len(folder):] if rest.startswith(folder) else rest


def packed(rec, k):
    v = rec.get(k)
    if v is None or k not in SCALE:
        return v
    return int(round(v * SCALE[k]))


def write_pack(path, records, meta):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany(
        f'INSERT OR REPLACE INTO p({",".join(COLUMNS)}) VALUES ({",".join("?" * len(COLUMNS))})',
        ([packed(rec, k) for k in COLUMNS] for rec in sorted(records, key=lambda r: r['code'])),
    )
    db.executemany('INSERT INTO meta VALUES (?, ?)', [(k, str(v)) for k, v in meta.items()])
    db.commit()
    db.execute('VACUUM')
    db.close()
    gz = path + '.gz'
    with open(path, 'rb') as src, gzip.GzipFile(gz, 'wb', compresslevel=9, mtime=0) as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    raw = os.path.getsize(path)
    os.remove(path)
    with open(gz, 'rb') as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    return {'bytes': os.path.getsize(gz), 'raw_bytes': raw, 'sha256': sha}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--off', required=True, help='en.openfoodfacts.org.products.csv.gz')
    ap.add_argument('--usda', help='FoodData_Central_branded_food_csv_*.zip')
    ap.add_argument('--usda-release', default='', help='fecha de la versión de USDA')
    ap.add_argument('--usda-since', default='2022-01-01', help='sólo productos de USDA revisados desde esta fecha')
    ap.add_argument('--out', default='dist')
    args = ap.parse_args()

    today = datetime.date.today()
    years = {str(today.year), str(today.year - 1)}
    os.makedirs(args.out, exist_ok=True)

    print('Open Food Facts…', file=sys.stderr)
    picked, with_table = read_off(args.off, years)

    index = {'version': 1, 'date': today.isoformat(), 'scale': SCALE, 'packs': {}}
    for country, cfg in COUNTRIES.items():
        chosen = [rec for _, _, rec in sorted(picked[country], key=lambda t: (t[0], t[1]))[:cfg['max']]]
        off_n = len(chosen)
        usda_n = 0
        if cfg['usda'] and args.usda:
            print('USDA…', file=sys.stderr)
            extra = read_usda(args.usda, with_table, args.usda_since)
            usda_n = len(extra)
            chosen += extra
        file = f'{country}.sqlite'
        meta = {
            'country': country, 'date': today.isoformat(), 'count': len(chosen),
            'off': off_n, 'usda': usda_n, 'usda_release': args.usda_release,
        }
        info = write_pack(os.path.join(args.out, file), chosen, meta)
        index['packs'][country] = {'file': file + '.gz', 'count': len(chosen), 'off': off_n, 'usda': usda_n, **info}
        print(f'{country}: {len(chosen):,} productos ({off_n:,} OFF + {usda_n:,} USDA), '
              f'{info["bytes"] / 1e6:.1f} MB comprimido, {info["raw_bytes"] / 1e6:.1f} MB en el móvil', file=sys.stderr)

    with open(os.path.join(args.out, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2)


if __name__ == '__main__':
    main()
