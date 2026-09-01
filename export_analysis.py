"""Готовит CSV-выгрузки и регрессионный анализ динамики по сводкам МВД."""

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np

SRC = Path('TGSpyder_Output/pressmvd/mvd_stop_jun_aug_2026')
SOURCES = Path('TGSpyder_Output/summer2026_sources')
OUT = Path('TGSpyder_Output/csv_export')
OUT.mkdir(parents=True, exist_ok=True)

METRICS = ['appeals', 'crimes', 'requisites', 'accounts', 'couriers']
RU = {
    'appeals': 'Сообщения_о_звонках',
    'crimes': 'Преступления_ИКТ',
    'requisites': 'Заблокировано_иностр_реквизитов',
    'accounts': 'Счетов_с_приостановкой',
    'couriers': 'Задержано_курьеров',
}

rows = list(csv.DictReader((SRC / 'per_post_stats.csv').open(encoding='utf-8-sig')))
summary = json.loads((SRC / 'summary.json').read_text(encoding='utf-8'))

# ── 1. Ежедневная выгрузка ───────────────────────────────────────────────────
daily = defaultdict(lambda: Counter())
for r in rows:
    for m in METRICS:
        if r[m]:
            daily[r['date']][m] += int(r[m])

days = sorted(daily)
with (OUT / '01_daily_metrics.csv').open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['Дата', 'День_лета'] + [RU[m] for m in METRICS])
    for i, d in enumerate(days, 1):
        w.writerow([d, i] + [daily[d][m] or '' for m in METRICS])

# ── 2. Помесячная выгрузка ───────────────────────────────────────────────────
monthly = defaultdict(lambda: Counter())
month_days = defaultdict(set)
for d in days:
    monthly[d[:7]].update(daily[d])
    month_days[d[:7]].add(d)

with (OUT / '02_monthly_totals.csv').open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['Месяц', 'Дней_со_сводкой'] + [RU[m] for m in METRICS])
    for mth in sorted(monthly):
        w.writerow([mth, len(month_days[mth])] + [monthly[mth][m] for m in METRICS])
    w.writerow(['ИТОГО', len(days)] + [sum(monthly[x][m] for x in monthly) for m in METRICS])

# ── 3. Регрессия тренда по каждому показателю ────────────────────────────────
trend_rows = []
x = np.arange(len(days), dtype=float)
for m in METRICS:
    y = np.array([daily[d][m] for d in days], dtype=float)
    mask = y > 0
    if mask.sum() < 5:
        continue
    xs, ys = x[mask], y[mask]
    slope, intercept = np.polyfit(xs, ys, 1)
    pred = slope * xs + intercept
    ss_res = float(((ys - pred) ** 2).sum())
    ss_tot = float(((ys - ys.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    n = len(xs)
    r = float(np.corrcoef(xs, ys)[0, 1])
    t_stat = r * ((n - 2) ** 0.5) / ((1 - r ** 2) ** 0.5) if abs(r) < 1 else float('inf')
    trend_rows.append({
        'Показатель': RU[m],
        'Дней_с_данными': n,
        'Среднее_в_сутки': round(float(ys.mean()), 1),
        'Медиана': round(float(np.median(ys)), 1),
        'Мин': int(ys.min()),
        'Макс': int(ys.max()),
        'Тренд_в_сутки': round(float(slope), 3),
        'Тренд_за_лето': round(float(slope) * (len(days) - 1), 1),
        'Корреляция_с_датой': round(r, 3),
        'R2': round(float(r2), 3),
        't_статистика': round(float(t_stat), 2),
        'Значим_p<0.05': abs(t_stat) > 1.99,
    })

with (OUT / '03_trend_regression.csv').open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, fieldnames=list(trend_rows[0].keys()))
    w.writeheader()
    w.writerows(trend_rows)

# ── 4. Корреляции между показателями ─────────────────────────────────────────
mat = np.array([[daily[d][m] for m in METRICS] for d in days], dtype=float)
with (OUT / '04_correlation_matrix.csv').open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow([''] + [RU[m] for m in METRICS])
    for i, m in enumerate(METRICS):
        line = [RU[m]]
        for j in range(len(METRICS)):
            a, b = mat[:, i], mat[:, j]
            ok = (a > 0) & (b > 0)
            line.append(round(float(np.corrcoef(a[ok], b[ok])[0, 1]), 3)
                        if ok.sum() > 3 else '')
        w.writerow(line)

# ── 5. Способы мошенничества ─────────────────────────────────────────────────
with (OUT / '05_fraud_methods.csv').open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['Способ', 'Упоминаний_в_сводках', 'Раз_назван_лидирующим', 'Взвешенный_балл'])
    lead = dict(summary['methods_leading'])
    pts = dict(summary['methods_weighted'])
    for name, cnt in summary['methods_mentions']:
        w.writerow([name, cnt, lead.get(name, 0), pts.get(name, 0)])

# ── 6. Публикационная активность каналов ─────────────────────────────────────
KW = re.compile(
    r'мошенн|фишинг|кибер|вишинг|дроп|антифрод|информационн\w* безопасн|хище', re.I)
act = [{
    'Канал': '@pressmvd',
    'Постов_за_лето': summary['posts_total'],
    'Из_них_по_теме': summary['daily_digests'],
    'Доля_по_теме_%': round(summary['daily_digests'] / summary['posts_total'] * 100, 1),
    'Дней_с_публикацией_по_теме': summary['unique_days'],
    'Покрытие_92_дней_%': round(summary['unique_days'] / 92 * 100, 1),
}]
for ch in ['pressnbrb', 'skgovby', 'cyberpoolofsharks']:
    path = SOURCES / f'{ch}.json'
    if not path.exists():
        continue
    posts = json.loads(path.read_text(encoding='utf-8'))
    hits = [p for p in posts if KW.search(p['text'])]
    act.append({
        'Канал': f'@{ch}',
        'Постов_за_лето': len(posts),
        'Из_них_по_теме': len(hits),
        'Доля_по_теме_%': round(len(hits) / len(posts) * 100, 1) if posts else 0,
        'Дней_с_публикацией_по_теме': len({p['date'][:10] for p in hits}),
        'Покрытие_92_дней_%': round(len({p['date'][:10] for p in hits}) / 92 * 100, 1),
    })

with (OUT / '06_channel_activity.csv').open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, fieldnames=list(act[0].keys()))
    w.writeheader()
    w.writerows(act)

print(f'CSV готовы в {OUT}/')
for row in trend_rows:
    print(f"  {row['Показатель']:35} тренд/сут {row['Тренд_в_сутки']:+8.3f}  "
          f"R2={row['R2']:.3f}  значим={row['Значим_p<0.05']}")
for a in act:
    print(f"  {a['Канал']:22} по теме {a['Из_них_по_теме']:>4}/{a['Постов_за_лето']:<4} "
          f"покрытие дней {a['Покрытие_92_дней_%']}%")
