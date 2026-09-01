import json
import re
from collections import Counter
from pathlib import Path

OUT = Path('TGSpyder_Output/pressmvd/mvd_stop_jun_aug_2026')
posts = json.loads((OUT / 'posts.json').read_text(encoding='utf-8'))

WORD_NUM = {
    'одном': 1, 'одного': 1, 'одна': 1, 'один': 1, 'двух': 2, 'два': 2, 'две': 2,
    'трех': 3, 'трёх': 3, 'три': 3, 'четырех': 4, 'четырёх': 4, 'четыре': 4,
    'пяти': 5, 'пять': 5, 'шести': 6, 'шесть': 6, 'семи': 7, 'семь': 7,
    'восьми': 8, 'восемь': 8, 'девяти': 9, 'девять': 9, 'десяти': 10, 'десять': 10,
    'ста': 100, 'сто': 100,
}


def num(token: str) -> int | None:
    t = token.replace(' ', '').replace('\u00a0', '')
    if t.isdigit():
        return int(t)
    return WORD_NUM.get(token.strip().lower())


def find(pattern, text):
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return num(m.group('n'))


P_APPEALS = r'поступил[оа][^.]{0,80}?(?P<n>\d[\d\s\u00a0]*|[а-яё]+)\s*сообщени'
P_CRIMES = r'[Зз]арегистрирован[оы][^.]{0,60}?(?P<n>\d[\d\s\u00a0]*|[а-яё]+)\s*преступлени'
P_REQ = r'(?P<n>\d[\d\s\u00a0]*|[а-яё]+)\s*иностранн\w*\s*платежн\w*\s*реквизит'
P_REQ_SINGLE = r'в адрес иностранного платежного реквизита'
P_ACC = r'операци\w*\s*по\s*(?P<n>\d[\d\s\u00a0]*|[а-яё]+)\s*(?:счет|счёт|банковск)'
P_ACC_ANY = r'(?:счет|счёт)'
P_COURIER = r'задержан\w*[^.]{0,120}?(?P<n>\d[\d\s\u00a0]*|[а-яё]+)\s*(?:курьер|дроппер)'
P_COURIER2 = r'(?P<n>\d[\d\s\u00a0]*|[а-яё]+)\s*курьер\w*[^.]{0,40}?задержан'

rows = []
methods = Counter()
method_first = Counter()
method_points = Counter()
misses = {'appeals': [], 'crimes': [], 'req': [], 'acc': []}

METHOD_MAP = [
    (r'вишинг', 'Вишинг — звонки от имени должностных лиц (банк, милиция, госорганы)'),
    (r'соцсет|социальн\w*\s*сет|мессендж', 'Хищения с использованием соцсетей (взлом аккаунтов)'),
    (r'покупк\w*\s*товар|интернет-магазин|торгов\w*\s*площад|маркетплейс',
     'Мошенничество при покупке товаров в интернете'),
    (r'аренд\w*\s*жиль|аренд\w*\s*кварт', 'Аренда жилья'),
    (r'фишинг', 'Фишинговые сайты и ссылки'),
    (r'вымогательств|шантаж', 'Вымогательство/шантаж'),
    (r'инвестиц|финансов\w*\s*бирж|торговл\w*\s*на\s*бирж|заработ',
     'Ложные инвестиции и «торговля на биржах»'),
    (r'знакомств', 'Сайты знакомств'),
]

for p in posts:
    t = p['text']
    is_digest = 'сообщени' in t and 'преступлени' in t
    req = find(P_REQ, t)
    if req is None and re.search(P_REQ_SINGLE, t, re.IGNORECASE):
        req = 1
    r = {
        'id': p['id'], 'date': p['date'][:10], 'url': p['url'],
        'appeals': find(P_APPEALS, t),
        'crimes': find(P_CRIMES, t),
        'requisites': req,
        'accounts': find(P_ACC, t),
        'couriers': find(P_COURIER, t) or find(P_COURIER2, t),
        'digest': is_digest,
    }
    rows.append(r)
    if is_digest:
        for key, val, present in (
            ('appeals', r['appeals'], True),
            ('crimes', r['crimes'], True),
            ('req', r['requisites'], bool(re.search('реквизит', t, re.I))),
            ('acc', r['accounts'], bool(re.search(P_ACC_ANY, t, re.I))),
        ):
            if val is None and present:
                misses[key].append(p['id'])

    seg = re.search(
        r'способу совершения(.*?)(?:👮|Работа по выявлению|Помните)', t,
        re.DOTALL | re.IGNORECASE,
    )
    if seg:
        s = seg.group(1)
        order = []
        for pat, label in METHOD_MAP:
            m = re.search(pat, s, re.IGNORECASE)
            if m:
                order.append((m.start(), label))
        order.sort()
        tail = re.search(r'[Зз]атем идут|[Дд]алее', s)
        cut = tail.start() if tail else len(s)
        for i, (pos, label) in enumerate(order):
            methods[label] += 1
            method_points[label] += max(6 - i, 1)
            if pos < cut:
                method_first[label] += 1


def total(key):
    return sum(r[key] for r in rows if r[key])


def count(key):
    return sum(1 for r in rows if r[key])


digests = [r for r in rows if r['digest']]
summary = {
    'period': '2026-06-01 — 2026-08-31',
    'channel': '@pressmvd',
    'hashtag': '#мвд_стоп_мошенники',
    'posts_total': len(posts),
    'daily_digests': len(digests),
    'unique_days': len({r['date'] for r in rows}),
    'appeals_total': total('appeals'),
    'appeals_posts': count('appeals'),
    'crimes_total': total('crimes'),
    'crimes_posts': count('crimes'),
    'requisites_total': total('requisites'),
    'requisites_posts': count('requisites'),
    'accounts_total': total('accounts'),
    'accounts_posts': count('accounts'),
    'couriers_total': total('couriers'),
    'couriers_posts': count('couriers'),
    'methods_mentions': methods.most_common(),
    'methods_leading': method_first.most_common(),
    'methods_weighted': method_points.most_common(),
    'digests_with_methods': sum(methods.values()) and len(
        [r for r in rows if r['digest']]),
    'misses': misses,
}

(OUT / 'summary.json').write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

import csv
with (OUT / 'per_post_stats.csv').open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

print(json.dumps(summary, ensure_ascii=False, indent=2))
