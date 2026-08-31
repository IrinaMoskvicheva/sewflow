"""Калькулятор себестоимости материалов изделия по техкарте.

Вход: техкарта (markdown с таблицей материалов) + таблица цен (Google Sheets).
Выход: словарь с расчётом + HTML-дашборд в cost_reports/.
"""

import csv
import io
import os
import re
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

COST_REPORTS = ROOT / "cost_reports"


# ---------- Цены из Google Sheets ----------

def _csv_url(sheet_url: str) -> str:
    sid = re.search(r"/d/([^/]+)", sheet_url).group(1)
    gid_m = re.search(r"gid=(\d+)", sheet_url)
    gid = gid_m.group(1) if gid_m else "0"
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"


def _num(s: str):
    s = s.replace(" ", "").replace(" ", "").replace(",", ".").strip().strip('"')
    try:
        return float(s)
    except ValueError:
        return None


def load_prices(sheet_url: str | None = None) -> list[dict]:
    """Читает таблицу «Расходы»: [{name, unit, price}]. Цена: факт, иначе план."""
    sheet_url = sheet_url or os.getenv("MATERIALS_PRICE_SHEET")
    if not sheet_url:
        raise RuntimeError("Не задан MATERIALS_PRICE_SHEET в .env")
    r = requests.get(_csv_url(sheet_url), timeout=30)
    r.raise_for_status()
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    prices = []
    for row in rows:
        if len(row) < 8 or not row[0].strip():
            continue
        price = _num(row[6]) or _num(row[5])
        if price is None:
            continue
        prices.append({"name": row[0].strip().lower(), "unit": row[1].strip(), "price": price})
    return prices


def find_price(material: str, prices: list[dict]):
    """Нечёткое совпадение названия материала с прайсом."""
    m = material.lower()
    stop = {"ткань", "мм", "см", "шт", "однотон"}
    keys = [w for w in re.findall(r"[0-9a-zа-яё]+", m) if w not in stop and len(w) > 2]
    best, best_score = None, 0
    for p in prices:
        score = sum(1 for k in keys if k in p["name"])
        if score > best_score:
            best, best_score = p, score
    return best if best_score > 0 else None


# ---------- Парсинг техкарты ----------

def parse_materials(md_path: Path) -> list[dict]:
    """Ищет markdown-таблицу материалов: | Материал | Расход | Ед. | Комментарий |"""
    text = md_path.read_text(encoding="utf-8")
    items = []
    header_ok = False
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not header_ok:
            if len(cells) >= 3 and any("материал" in c.lower() for c in cells) \
                    and any("расход" in c.lower() for c in cells):
                header_ok = True
            continue
        if line.strip().startswith("|---") or set(line.strip()) <= set("|-: "):
            continue
        if not line.strip().startswith("|"):
            if items:
                break
            continue
        qty = _num(cells[1])
        if len(cells) >= 3 and qty is not None:
            items.append({"material": cells[0], "qty": qty,
                          "unit": cells[2], "note": cells[3] if len(cells) > 3 else ""})
    return items


# ---------- Раскладка кроя по ширине рулона ----------

GAP_CM = 1.0  # технологический зазор между деталями в ряду


def parse_cut_parts(md_path: Path) -> list[dict]:
    """Ищет markdown-таблицу «Детали кроя»: | Деталь | Размер, см | Кол-во |.

    Размер «A × B» (допускается «30/38 × 22» для трапеций — берётся бо́льшая сторона).
    Кол-во «2» или «1 (из полос)» — берётся целое число.
    """
    text = Path(md_path).read_text(encoding="utf-8")
    parts = []
    header_ok = False
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not header_ok:
            if len(cells) >= 3 and any("детал" in c.lower() for c in cells) \
                    and any("размер" in c.lower() for c in cells) \
                    and any("кол" in c.lower() for c in cells):
                header_ok = True
            continue
        if line.strip().startswith("|---") or set(line.strip()) <= set("|-: "):
            continue
        if not line.strip().startswith("|"):
            if parts:
                break
            continue
        m = re.match(r"([\d/.,\s]+)\s*[×xх]\s*([\d/.,\s]+)", cells[1])
        if len(cells) >= 3 and m:
            sides = []
            for g in m.groups():
                vals = [_num(v) for v in g.split("/")]
                vals = [v for v in vals if v]
                sides.append(max(vals) if vals else None)
            cnt_m = re.search(r"\d+", cells[2])
            if all(sides) and cnt_m:
                parts.append({"name": cells[0], "a_cm": sides[0], "b_cm": sides[1],
                              "count": int(cnt_m.group())})
    return parts


def pack_strips(parts: list[dict], width_m: float) -> dict:
    """Полочная раскладка (first-fit decreasing) поперёк рулона шириной width_m.

    Каждая деталь поворачивается длинной стороной вдоль рулона. Детали сортируются
    по длине (по убыванию) и укладываются в полосы: новая деталь встаёт в первую
    полосу, где хватает ширины, иначе открывается новая полоса. Длина полосы =
    длина первой (самой длинной) детали. Зазор между деталями — GAP_CM.
    Возвращает метраж (м.пог.), площадь по раскладке и КИМ (долю использования).
    """
    W_cm = width_m * 100
    pieces = []  # (вдоль, поперёк, имя детали)
    for p in parts:
        along, across = max(p["a_cm"], p["b_cm"]), min(p["a_cm"], p["b_cm"])
        pieces += [(along, across, p["name"])] * p["count"]
    pieces.sort(key=lambda x: -x[0])

    strips = []  # {"len": длина полосы, "left": остаток ширины}
    for along, across, _ in pieces:
        need = across + GAP_CM
        placed = False
        for s in strips:
            if need <= s["left"]:
                s["left"] -= need
                placed = True
                break
        if not placed:
            strips.append({"len": along, "left": max(0.0, W_cm - across)})
    total_len_cm = sum(s["len"] + GAP_CM for s in strips)

    area_m2 = sum(p["a_cm"] * p["b_cm"] * p["count"] for p in parts) / 10_000
    length_m = round(total_len_cm / 100, 2)
    used_m2 = round(length_m * width_m, 2)
    rows = [{"name": f"Полоса {i+1}", "a_cm": s["len"], "b_cm": round(W_cm - s["left"], 1),
             "count": "", "per_row": "—", "rows": "—", "row_len_cm": s["len"]}
            for i, s in enumerate(strips)]
    return {"width_m": width_m, "length_m": length_m, "used_m2": used_m2,
            "area_m2": round(area_m2, 2),
            "kim": round(area_m2 / used_m2, 3) if used_m2 else None,
            "rows": rows, "strips": len(strips)}


def compute_layout(md_path: Path, width_m: float | None = None) -> dict | None:
    """Раскладка по таблице кроя; None, если таблицы нет или детали не распарсились."""
    width_m = width_m or float(os.getenv("FABRIC_WIDTH", "1.5"))
    parts = parse_cut_parts(md_path)
    if not parts:
        return None
    return {"width_m": width_m, **pack_strips(parts, width_m)}


# ---------- Расчёт ----------

def calculate(md_path: Path, prices: list[dict] | None = None) -> dict:
    prices = prices if prices is not None else load_prices()
    items, unaccounted = [], []
    for it in parse_materials(Path(md_path)):
        p = find_price(it["material"], prices)
        if p:
            total = round(it["qty"] * p["price"], 2)
            note = it["note"]
            if p["name"] != it["material"].lower():
                note = (note + " " if note else "") + f"(прайс: {p['name']})"
            items.append({**it, "price": p["price"], "price_unit": p["unit"],
                          "total": total, "note": note.strip()})
        else:
            unaccounted.append(it)
    return {"techcard": Path(md_path).stem, "items": items,
            "unaccounted": unaccounted,
            "total": round(sum(i["total"] for i in items), 2),
            "layout": compute_layout(Path(md_path))}


# ---------- HTML-дашборд ----------

_CSS = """
:root{--bg:#0f1115;--panel:#181c24;--panel2:#1f242e;--border:#2a303c;--text:#e6e9ef;
--muted:#8b93a3;--accent:#4cc38a;--blue:#58a6ff;--warn:#e3b341}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:"Segoe UI",system-ui,sans-serif;padding:32px 20px}
.wrap{max-width:960px;margin:0 auto}
header{margin-bottom:24px}h1{font-size:22px;font-weight:600}
.meta{color:var(--muted);font-size:13px;margin-top:6px;line-height:1.6}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:28px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px 20px}
.card .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.card .value{font-size:26px;font-weight:700;color:var(--accent);margin-top:8px}
.card .value.blue{color:var(--blue);font-size:20px}
.card .sub{font-size:12px;color:var(--muted);margin-top:6px}
section{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px 22px;margin-bottom:24px}
h2{font-size:15px;font-weight:600;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.num,th.num{text-align:right}tr:last-child td{border-bottom:none}
tr.total td{font-weight:700;color:var(--accent);border-top:2px solid var(--border)}
.note{color:var(--warn);font-size:11px}
.bar-row{display:flex;align-items:center;gap:12px;margin-bottom:10px;font-size:13px}
.bar-row .name{width:220px;flex-shrink:0;color:var(--muted)}
.bar-row .track{flex:1;background:var(--panel2);border-radius:6px;height:18px;overflow:hidden}
.bar-row .fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--accent));border-radius:6px}
.bar-row .pct{width:64px;text-align:right}
.warn{border-color:#4a3f22;background:#1d1a12}.warn h2{color:var(--warn)}
.warn ul{margin:4px 0 0 18px;font-size:13.5px;line-height:1.9}.warn .est{color:var(--muted)}
footer{color:var(--muted);font-size:12px;text-align:center}
"""


def render_dashboard(calc: dict, model: str = "", size: str = "") -> Path:
    COST_REPORTS.mkdir(exist_ok=True)
    out = COST_REPORTS / f"{calc['techcard']}-cost.html"
    total = calc["total"] or 1
    top = max(calc["items"], key=lambda i: i["total"], default=None)

    rows = "".join(
        f"<tr><td>{i['material']} "
        + (f'<span class="note">{i["note"]}</span>' if i["note"] else "")
        + f"</td><td>{i['qty']:g} {i['unit']}</td><td class='num'>{i['price']:g} ₽/{i['price_unit']}</td>"
        + f"<td class='num'>{i['total']:,.1f} ₽</td><td class='num'>{i['total']/total*100:.1f}%</td></tr>"
        for i in calc["items"])
    bars = "".join(
        f"<div class='bar-row'><div class='name'>{i['material']}</div>"
        f"<div class='track'><div class='fill' style='width:{i['total']/total*100:.1f}%'></div></div>"
        f"<div class='pct'>{i['total']/total*100:.1f}%</div></div>"
        for i in sorted(calc["items"], key=lambda x: -x["total"]))
    layout_html = ""
    lay = calc.get("layout")
    if lay:
        lrows = "".join(
            f"<tr><td>{r['name']}</td><td class='num'>{r['row_len_cm']:g} см</td>"
            f"<td class='num'>{r['b_cm']:g} см</td></tr>"
            for r in lay["rows"])
        kim_pct = f"{lay['kim']*100:.0f}%" if lay["kim"] else "—"
        layout_html = f"""
<section><h2>Раскладка кроя · ширина рулона {lay['width_m']:g} м · {lay['strips']} полос</h2>
<div class="cards" style="margin-bottom:16px">
<div class="card"><div class="label">Метраж по раскладке</div><div class="value blue">{lay['length_m']:g} м.пог.</div>
<div class="sub">≈ {lay['used_m2']:g} м² ткани фактически</div></div>
<div class="card"><div class="label">Площадь деталей</div><div class="value blue">{lay['area_m2']:g} м²</div>
<div class="sub">сумма площадей всех деталей кроя</div></div>
<div class="card"><div class="label">КИМ (использование ткани)</div><div class="value blue">{kim_pct}</div>
<div class="sub">площадь деталей / площадь по раскладке</div></div>
</div>
<table><thead><tr><th>Полоса раскладки</th><th class="num">Длина полосы</th>
<th class="num">Занято по ширине</th></tr></thead>
<tbody>{lrows}</tbody></table></section>"""
    warn = ""
    if calc["unaccounted"]:
        lis = "".join(f"<li>{u['material']} — {u['qty']:g} {u['unit']} "
                      f"<span class='est'>{u.get('note','цены нет в прайсе')}</span></li>"
                      for u in calc["unaccounted"])
        warn = f"<section class='warn'><h2>Не учтено — нет цены в таблице</h2><ul>{lis}</ul></section>"

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Себестоимость {calc['techcard']}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<header><h1>Себестоимость материалов · {model or calc['techcard']}{(' ' + size) if size else ''}</h1>
<div class="meta">Дата расчёта: {date.today().strftime('%d.%m.%Y')} · Источник цен: таблица материалов (Google Sheets)</div></header>
<div class="cards">
<div class="card"><div class="label">Себестоимость (учтённые позиции)</div><div class="value">{calc['total']:,.0f} ₽</div>
<div class="sub">{len(calc['items'])} позиций из прайса</div></div>
<div class="card"><div class="label">Самая дорогая позиция</div>
<div class="value blue">{top['material'] if top else '—'}</div>
<div class="sub">{top['total']:,.0f} ₽ · {top['total']/total*100:.1f}%</div> </div>
<div class="card"><div class="label">Без цены в прайсе</div><div class="value blue">{len(calc['unaccounted'])}</div>
<div class="sub">см. блок «Не учтено»</div></div>
</div>
<section><h2>Детализация затрат</h2><table>
<thead><tr><th>Материал</th><th>Расход</th><th class="num">Цена за ед.</th><th class="num">Сумма</th><th class="num">Доля</th></tr></thead>
<tbody>{rows}
<tr class="total"><td>Итого учтено</td><td></td><td></td><td class="num">{calc['total']:,.1f} ₽</td><td class="num">100%</td></tr>
</tbody></table></section>
<section><h2>Распределение затрат</h2>{bars}</section>
{layout_html}
{warn}
<footer>sewflow · tkcost</footer>
</div></body></html>"""
    out.write_text(html, encoding="utf-8")
    return out


def run(techcard: str, model: str = "", size: str = "") -> dict:
    """Полный цикл: техкарта → расчёт → дашборд."""
    calc = calculate(ROOT / "tech_maps" / techcard)
    calc["report"] = str(render_dashboard(calc, model, size))
    return calc


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    res = run(sys.argv[1] if len(sys.argv) > 1 else "SB-146.md")
    lay = res.get("layout")
    lay_msg = f" · раскладка: {lay['length_m']:g} м.пог., КИМ {lay['kim']:.0%}" if lay else ""
    print(f"Итого: {res['total']} ₽{lay_msg} · отчёт: {res['report']}")
