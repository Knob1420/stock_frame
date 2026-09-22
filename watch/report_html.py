# -*- coding: utf-8 -*-
"""report_html.py —— 静态 HTML 日报(层2主展示形态):六区结构,单文件自包含。
图 base64 内嵌、CSS 内联、Jinja2 模板内嵌;一天一文件 + 滚动 index.html。
scan.py 末尾自动调用;独立重渲染: python report_html.py <date>(Task 5 加 main)。
区:①大盘总览 ②板块横览(留位) ③触发卡片 ④跟踪中 ⑤历史回填 ⑥追问记录。
"""
import base64
import glob
import os

import jinja2
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")

CSS = """body{margin:0;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#fcfcfb;color:#0b0b0b}header{padding:16px 20px;border-bottom:1px solid #e1e0d9}
h1{font-size:20px;margin:0 0 8px}nav a{margin-right:14px;color:#2a78d6;text-decoration:none;font-size:13px}
main{max-width:960px;margin:0 auto;padding:12px 20px 40px}
section{margin:22px 0;padding:14px 16px;border:1px solid #e1e0d9;border-radius:8px;background:#fff}
h2{font-size:16px;margin:0 0 10px}h2 small{color:#898781;font-weight:400;margin-left:8px}
.kv{margin:4px 0}.muted{color:#898781}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid #e1e0d9;text-align:left}
th{color:#898781;font-weight:500}img{max-width:100%;height:auto;margin:8px 0;border:1px solid #e1e0d9;border-radius:6px}
details{margin:8px 0;padding:6px 10px;background:#fcfcfb;border:1px dashed #c3c2b7;border-radius:6px}
summary{cursor:pointer;color:#2a78d6;font-size:13px}pre{white-space:pre-wrap;margin:6px 0}
.card{margin:18px 0;padding:14px 16px;border:1px solid #e1e0d9;border-radius:8px;background:#fff}
.card h3{margin:0 0 6px;font-size:15px}.card .head{color:#898781;font-size:13px;margin-bottom:8px}
.pos{color:#d03b3b}.neg{color:#008300}tr.ms td{background:#fdf6e3}
.badge{display:inline-block;padding:0 6px;border-radius:8px;background:#2a78d6;color:#fff;font-size:11px;margin-left:6px}
footer{padding:14px 20px;color:#898781;font-size:12px;border-top:1px solid #e1e0d9}"""

PAGE_T = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>盯盘日报 {{ date }}</title><style>""" + CSS + """</style></head><body>
<header><h1>盯盘日报 {{ date }}</h1><nav>
<a href="#mkt">大盘</a><a href="#sector">板块</a><a href="#cards">触发</a>
<a href="#track">跟踪</a><a href="#backfill">回填</a><a href="#qa">追问</a>
<a href="index.html">索引</a></nav></header>
<main>{{ sections|safe }}</main>
<footer>生成于 {{ ts }} · stock_frame watch</footer></body></html>"""

MKT_T = """<section id="mkt"><h2>① 大盘总览 <small>{{ m.date }}</small></h2>
<p class="kv">涨{{ m.adv }} / 跌{{ m.dec }} / 平{{ m.flat }} ·
站上年线(MA250) {{ m.above }}/{{ m.total }} ({{ m.above_pct }}%) · 中位量比 {{ m.med_vr }}</p>
<p class="kv">沪深300 近5日 {{ m.d5 }} · 近20日 {{ m.d20 }} · 距年线 {{ m.dist250 }}</p>
{% if m.img %}<img src="{{ m.img }}" alt="大盘总览">{% else %}<p class="muted">大盘图未生成</p>{% endif %}
</section>"""

SECTOR_T = """<section id="sector"><h2>② 板块横览</h2>
<p class="muted">板块数据待接入(触发按行业分组、同板块多票=集群信号)</p></section>"""

TRACK_T = """<section id="track"><h2>④ 跟踪中 <small>持续触发未钝化</small></h2>
{% if rows %}<table><tr><th>代码</th><th>名称</th><th>规则</th><th>持续天数</th></tr>
{% for r in rows %}<tr><td>{{ r.code }}</td><td>{{ r.name }}</td><td>{{ r.rule }}</td><td>{{ r.days }}</td></tr>{% endfor %}
</table>{% else %}<p class="muted">无跟踪中条目</p>{% endif %}</section>"""


def _b64(path):
    """PNG → data URI;缺失返回 None(渲染降级为无图)。"""
    if not path or not os.path.exists(path):
        return None
    return "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()


def _nan(x):
    return x is None or (isinstance(x, float) and pd.isna(x))


def _pct(x, nd=1):
    """百分比;None/NaN → N/A。"""
    return "N/A" if _nan(x) else "%+.*f%%" % (nd, 100 * x)


def _f(x):
    return "N/A" if _nan(x) else "%g" % x


def _f2(x):
    return "N/A" if _nan(x) else "%.2f" % x


def _sec_market(**kw):
    st = kw["market"] or {}
    m = {"date": st.get("date", "?"), "adv": st.get("adv", "N/A"), "dec": st.get("dec", "N/A"),
         "flat": st.get("flat", "N/A"), "above": st.get("above", "N/A"), "total": st.get("total", "N/A"),
         "med_vr": _f2(st.get("med_vr")),
         "d5": _pct(st.get("bench_d5")), "d20": _pct(st.get("bench_d20")),
         "dist250": _pct(st.get("bench_dist250")),
         "above_pct": "%.0f" % (100.0 * st["above"] / st["total"]) if st.get("total") else "N/A",
         "img": _b64((kw["charts"] or {}).get("market"))}
    return jinja2.Template(MKT_T).render(m=m)


def _sec_sector(**kw):
    return SECTOR_T


def _sec_tracking(**kw):
    rows = kw["tracking"] or []
    return jinja2.Template(TRACK_T).render(rows=rows)


def build(events, tracking, briefs, charts, market, date, out_dir=REPORTS):
    """渲染单日 HTML + index.html,返回 HTML 路径;单区失败降级为错误行不阻断。"""
    os.makedirs(out_dir, exist_ok=True)
    secs = []
    for fn in (_sec_market, _sec_sector, _sec_tracking):
        try:
            secs.append(fn(events=events, tracking=tracking, briefs=briefs,
                           charts=charts, market=market, date=date, out_dir=out_dir))
        except Exception as ex:
            secs.append('<section><h2>⚠ %s 渲染失败: %s</h2></section>' % (fn.__name__, ex))
    html = jinja2.Template(PAGE_T).render(
        date=date, sections="\n".join(secs),
        ts=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
    p = os.path.join(out_dir, "%s.html" % date)
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    write_index(out_dir)
    return p


def write_index(out_dir=REPORTS):
    """滚动索引:倒序链 reports/ 下所有 YYYY-MM-DD.html。"""
    pat = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].html"
    files = sorted(glob.glob(os.path.join(out_dir, pat)), reverse=True)
    rows = "".join('<tr><td>%s</td><td><a href="%s">打开</a></td></tr>'
                   % (os.path.basename(f)[:-5], os.path.basename(f)) for f in files)
    html = ('<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>盯盘日报索引</title>'
            '<style>body{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;max-width:640px;margin:24px auto;'
            'padding:0 16px}table{border-collapse:collapse}td,th{padding:6px 12px;border-bottom:1px solid #e1e0d9}'
            'a{color:#2a78d6}</style></head><body><h1>盯盘日报索引</h1>'
            '<table><tr><th>日期</th><th></th></tr>%s</table></body></html>') % rows
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
