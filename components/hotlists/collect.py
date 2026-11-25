"""Public hotlists for the editorial workbench."""
import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

BOARDS = [
    ("weibo", "微博体育热搜", "https://s.weibo.com/top/summary?cate=sport", 60),
    ("weibo", "微博实时热搜", "https://s.weibo.com/top/summary?cate=realtimehot", 50),
    ("douyin", "抖音热点榜", "https://www.douyin.com/hot", 50),
]


async def collect(output_path, smoke=False):
    from playwright.async_api import async_playwright
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True, timeout=20000)
        context = await browser.new_context()
        if smoke:
            async def fixture(route):
                html = (
                    '<table><tbody><tr><td class="td-01">1</td><td class="td-02"><a href="/weibo?q=NBA"><b>NBA</b> 赛后</a><span>50000</span></td></tr></tbody></table>'
                    if route.request.url.startswith("https://s.weibo.com/")
                    else '<div>1 <a href="/hot/1"><b>NBA</b> 赛后</a> 3.5万热度</div>'
                )
                await route.fulfill(content_type="text/html; charset=utf-8", body=html)

            await context.route("**/*", fixture)

        async def board(platform, name, url, limit):
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=22000)
                selector = "td.td-02 a" if platform == "weibo" else 'a[href*="/hot/"]'
                await page.locator(selector).first.wait_for(state="attached", timeout=8000)
                if platform == "weibo":
                    rows = await page.locator("tbody tr").evaluate_all("rows => rows.map(r=>({title:r.querySelector('td.td-02 a')?.textContent?.trim(),url:r.querySelector('td.td-02 a')?.href,rank:r.querySelector('td.td-01')?.textContent?.trim(),heat_raw:r.querySelector('td.td-02 span')?.textContent?.trim()})).filter(x=>x.title&&x.url).slice(0,60)")
                else:
                    rows = await page.locator(selector).evaluate_all("""rows => rows.map((a,i)=>{let node=a,text=a.textContent;for(let n=0;n<4&&node;n++,node=node.parentElement){if(node.querySelectorAll('a[href*="/hot/"]').length>1)break;text=node.innerText||node.textContent;}const rank=text.match(/^\\s*(\\d{1,2})(?:\\s|\\n)/);return {title:a.textContent.trim(),url:a.href,rank:rank?Number(rank[1]):null,list_position:i+1,heat_raw:text.match(/[\\d.]+万?热度/)?.[0]||null}}).filter(x=>x.title&&x.url).slice(0,50)""")
                observed = dt.datetime.now(dt.timezone.utc).isoformat()
                records = []
                for row in rows[:limit]:
                    rank = str(row.get("rank") or "")
                    records.append({**row, "title": row["title"].strip()[:250], "rank": int(rank) if rank.isdigit() else None, "platform": platform, "board_url": url, "board_category": name, "observed_at": observed, "source_kind": "hotlist", "retrieval_method": "public_browser_dom", "desc": "公开榜单快照，仅为线索，比赛事实仍需核实"})
                return {"platform": platform, "board": name, "url": url, "status": "completed", "http_status": response.status if response else None, "count": len(records), "records": records}
            except Exception as exc:
                return {"platform": platform, "board": name, "url": url, "status": "source_unavailable", "count": 0, "records": [], "error_type": type(exc).__name__, "message": "该榜单不可读或需要登录／验证，本轮停止此源；其他来源继续"}
            finally:
                await page.close()

        completed = []
        async def observed_board(item):
            result = await board(*item)
            completed.append(result)
            if output_path:
                rows = [record for value in completed for record in value.get("records", [])]
                output_path.write_text(json.dumps({"status":"completed" if rows else "source_unavailable", "records":rows, "boards":[{k:v for k,v in value.items() if k!='records'} for value in completed], "message":"公开榜单分源采集快照"},ensure_ascii=False),encoding="utf-8")
            return result
        try:
            results = await asyncio.gather(*(observed_board(item) for item in BOARDS))
        finally:
            await browser.close()
    records = [record for result in results for record in result.pop("records")]
    return {"status": "completed" if records else "source_unavailable", "records": records, "boards": results, "adapter": "bundled_public_hotlists", "message": "公开榜单采集完成" if records else "榜单不可用，继续赛事公开检索"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    # The Windows portable Python shares this directory with the Agent.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime" / "hotlists" / "dependencies"))
    path = Path(args.output)
    try:
        result = asyncio.run(asyncio.wait_for(collect(path, args.smoke), timeout=45))
    except Exception as exc:
        result = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"status":"source_unavailable","records":[],"boards":[]}
        result.update({"message":"部分榜单超时，保留已取得的快照，其他公开检索继续" if result.get("records") else "公开榜单采集超时或不可用，继续赛事公开检索", "error_type": type(exc).__name__})
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": result["status"], "count": len(result["records"])}, ensure_ascii=False))
