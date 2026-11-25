"""Small advisory diagnostics used to target the single allowed polish pass."""
import difflib
import re


def diagnostics(data):
    paragraphs=[p for p in data.get('paragraphs',[]) if p.get('kind')=='analysis']
    issues=[]
    normalized=[re.sub(r'\d+(?:\.\d+)?','#',p['text']) for p in paragraphs if len(p.get('text',''))>=35]
    if any(difflib.SequenceMatcher(None,left,right,autojunk=False).ratio()>.62 for i,left in enumerate(normalized) for right in normalized[i+1:]):
        issues.append('多个分析段重复同一判断，请合并，并把篇幅留给不同的具体解释。')
    risky=re.compile(r'(?:篮板|助攻|数据|统计).{0,24}(?:说明|因此|自然).{0,26}(?:交给|角色|战术|任务|导致)')
    if any(risky.search(p['text']) for p in paragraphs):
        issues.append('检查是否仅凭统计变化断言角色或战术因果；没有过程支持时改成有边界的解释，不反复强调同一免责声明。')
    return issues
