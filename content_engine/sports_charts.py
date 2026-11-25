"""Source-bound sports charts, stored as ordinary ordered image blocks."""
import hashlib
import io
import math
import re
import threading
import unicodedata
from pathlib import Path

from .store import dumps

PLOT_LOCK = threading.Lock()
UNITS = {"得分": ("分", "points", "pts"), "篮板": ("篮板", "篮", "板", "rebounds", "reb"), "助攻": ("助攻", "assists", "ast"), "盖帽": ("盖帽", "blocks", "blk"), "抢断": ("抢断", "steals", "stl"), "进球": ("球", "goals", "goal"), "ACE": ("记ACE", "个ACE", "次ACE", "aces", "ace")}


def compact(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def supported_row(row, metric, unit, facts):
    fact = facts.get(row.get("fact_id"))
    value, label, quote = row.get("value"), str(row.get("label", "")).strip(), str(row.get("quote", "")).strip()
    if not fact or type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1000 or not 2 <= len(label) <= 45 or not 2 <= len(quote) <= 200:
        return None
    if compact(quote) not in compact(fact["text"]) or compact(label) not in compact(quote):
        return None
    terms = UNITS.get(metric)
    if not terms or unit not in ("次", "个", "分", "球"):
        return None
    if metric == "得分" and unit != "分" or metric != "得分" and unit == "分":
        return None
    # Exact value + unit, with the row's subject preceding it in the same short
    # clause. This deliberately rejects ambiguous multi-player score sentences.
    spelling = f"{value:g}"
    tokens = r"(?:" + "|".join(re.escape(t) for t in terms) + r")"
    clauses = re.split(r"[。；;\n]|(?<=[，,])(?=[^，,]{0,12}(?:得到|拿到|贡献|贡献了|砍下))", quote)
    for clause in clauses:
        start = compact(clause).find(compact(label))
        match = re.search(r"(?<![\d.])" + re.escape(spelling) + r"\s*(?:个|次)?" + tokens + r"(?![A-Za-z])", clause, re.I)
        label_at = clause.find(label)
        first = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:个|次)?" + tokens + r"(?![A-Za-z])", clause[label_at+len(label):], re.I) if label_at >= 0 else None
        if not first or float(first[1]) != value:
            continue
        if match and start >= 0 and compact(label) in compact(clause[:match.start()]) and len(clause[:match.start()]) <= 85:
            return {"label": label, "value": value, "fact_id": fact["id"], "quote": quote, "source_url": fact["url"]}
    return None


def validated(proposals, facts):
    result, by_id = [], {f["id"]: f for f in facts}
    for proposal in proposals[:2]:
        if not isinstance(proposal, dict): continue
        metric, unit = proposal.get("metric"), proposal.get("unit")
        rows = [supported_row(row, metric, unit, by_id) for row in proposal.get("rows", [])[:6]]
        rows = [r for r in rows if r]
        if 2 <= len(rows) <= 6 and len({r["label"] for r in rows}) == len(rows):
            # A chart cannot pool unrelated dates/events under one vague heading.
            result.append({"title": str(proposal.get("title", metric + "对比"))[:45], "metric": metric, "unit": unit, "rows": rows})
    return result


def extracted(facts, subjects):
    """Conservative deterministic fallback: one subject's explicit stat line."""
    result = []
    for fact in facts:
        clauses = re.split(r"[。；;\n]", fact["text"])
        for sentence in clauses:
            for subject in subjects:
                aliases = [a for a in subject.get("aliases", []) if a in sentence]
                if not aliases: continue
                label = max(aliases, key=len)
                after = re.split(r"[，,]", sentence[sentence.index(label):])[0]
                if len(after) > 180: after = after[:180]
                values = []
                for metric, terms in UNITS.items():
                    match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:个|次)?(?:" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z])", after, re.I)
                    if match:
                        values.append({"label": metric, "value": float(match[1]), "fact_id": fact["id"], "quote": after, "source_url": fact["url"]})
                if len(values) >= 2:
                    result.append({"title": label + " · 关键数据", "metric": "个人数据", "unit": "按项标注", "rows": values, "subject": label, "kind": "stat_card"})
    return result[:1]


def score_comparison(facts, subjects, scope):
    """Only accept an explicit A-score-score-B clause, never infer team identity."""
    for fact in facts:
        for clause in re.split(r"[。；;\n]",fact['text']):
            for match in re.finditer(r"(?<!\d)(\d{1,3})\s*(?:比|[:：-])\s*(\d{1,3})(?!\d)",clause):
                before,after=clause[:match.start()][-35:],clause[match.end():][:45]
                left=[a for s in subjects for a in s.get('aliases',[]) if len(a)>1 and a in before]
                right=[a for s in subjects for a in s.get('aliases',[]) if len(a)>1 and a in after]
                if not left or not right:continue
                a,b=max(left,key=len),max(right,key=len)
                if a==b:continue
                values=[int(match[1]),int(match[2])]
                unit='盘' if scope=='tennis' and max(values)<=5 else '局' if scope in ('tennis','racket') else '球' if scope=='football' else '分'
                return [{'title':a+' 与 '+b+' · 比分对照','metric':'比分','unit':unit,'rows':[{'label':name,'value':value,'fact_id':fact['id'],'quote':clause,'source_url':fact['url']} for name,value in zip((a,b),values)]}]
    return []


def render(chart, event_date):
    # Use a standard plot renderer; no AI-generated numbers or invented axes.
    try:
        import matplotlib
    except ModuleNotFoundError:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "chart-libs"))
        import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    font_path = next((p for p in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf"), Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")) if p.is_file()), None)
    font = FontProperties(fname=str(font_path)) if font_path else FontProperties()
    with PLOT_LOCK:
        fig, ax = plt.subplots(figsize=(10, 3.5 if chart.get("kind") == "stat_card" else 5.5), dpi=160)
        fig.patch.set_facecolor("#f5f7f2"); ax.set_facecolor("#f5f7f2")
        fig.text(.075, .90, chart["title"], fontproperties=font, fontsize=20, color="#183b36", weight="bold")
        fig.text(.075, .84, (event_date or "日期见正文") + "  ·  " + chart["metric"], fontproperties=font, fontsize=11, color="#64766c")
        rows = chart["rows"]
        if chart.get("kind") == "stat_card":
            ax.axis("off")
            for i, row in enumerate(rows):
                x = .08 + i * (.84 / len(rows))
                fig.text(x, .49, f"{row['value']:g}", fontsize=35, color="#147766", weight="bold")
                fig.text(x, .39, row["label"], fontproperties=font, fontsize=14, color="#384d44")
        else:
            fig.subplots_adjust(left=.23, right=.90, top=.73, bottom=.23)
            values = [r["value"] for r in rows]
            bars = ax.barh(range(len(rows)), values, color=["#167e6c", "#a2c5b1", "#5c9a82", "#bdd7c7", "#83ae99", "#d1e3d7"][:len(rows)], height=.58)
            ax.set_yticks(range(len(rows)), [r["label"] for r in rows], fontproperties=font, fontsize=12)
            ax.invert_yaxis(); ax.set_xlim(0, max(values)*1.2 if max(values)>0 else 1)
            ax.set_xlabel(chart["unit"], fontproperties=font, color="#617269")
            ax.spines[["top", "right", "left"]].set_visible(False); ax.tick_params(axis="y", length=0)
            ax.grid(axis="x", alpha=.12); ax.set_axisbelow(True)
            for bar, value in zip(bars, values): ax.text(bar.get_width()+max(max(values),1)*.025, bar.get_y()+bar.get_height()/2, f"{value:g}", va="center", fontsize=13, color="#213c32")
        hosts = list(dict.fromkeys(__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(r["source_url"]).hostname or "原文" for r in rows))
        fig.text(.075, .09, "数据出处：" + " / ".join(hosts)[:100], fontproperties=font, fontsize=9, color="#64766c")
        fig.text(.075, .045, "数据范围与比赛背景见正文。", fontproperties=font, fontsize=9, color="#64766c")
        output = io.BytesIO();fig.savefig(output, format="png", facecolor=fig.get_facecolor());plt.close(fig)
        return output.getvalue()


def build_for_article(manager, article_id, proposals, subjects, emit):
    article = manager.engine.article(article_id)
    topic = manager.engine.topic(article["topic_id"])
    if topic["mode_id"] != "sports": return []
    facts = topic["data"]["facts"]
    scope=topic['data'].get('agent_origin',{}).get('profile',{}).get('scope',{}).get('id','')
    charts = validated(proposals, facts) or extracted(facts, subjects) or score_comparison(facts, subjects, scope)
    ids = []
    for chart in charts[:2]:
        basis = hashlib.sha256(dumps(chart).encode()).hexdigest()
        existing = next((p for p in manager.list(article_id) if p["data"].get("chart_hash") == basis), None)
        if existing:
            ids.append(existing["id"]);continue
        content = render(chart, topic["data"].get("event_date_cn"))
        position = max(2, len(article["data"]["paragraphs"]) // 2)
        aid = manager.save_generated(article_id, content, {"caption": chart["title"], "source_title": chart["title"], "source_url": chart["rows"][0]["source_url"], "chart_data": chart, "chart_hash": basis, "basis_fact_ids": list(dict.fromkeys(r["fact_id"] for r in chart["rows"]))}, position)
        ids.append(aid)
        if not article["data"].get("document"):
            with manager.store.db() as db: db.execute("UPDATE photo_assets SET selected=1 WHERE id=?", (aid,))
    emit("chart", f"已生成 {len(ids)} 张基于原文事实的数据图表" if ids else "已有事实不足以绘制有意义的数据图表，未补造数据", {"asset_ids": ids})
    return ids
