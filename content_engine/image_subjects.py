"""Bounded bilingual subject resolution. IDs are stable NBA person identifiers."""
import re

NBA = [
    (1629029, "Luka Doncic", "东契奇|東契奇|Doncic|Dončić"),
    (1641705, "Victor Wembanyama", "文班亚马|文班亞馬|Wembanyama"),
    (202691, "Klay Thompson", "克莱|克雷|Klay Thompson"),
    (201939, "Stephen Curry", "斯蒂芬.*库里|库里|庫里|Stephen Curry|Steph Curry"),
    (2544, "LeBron James", "勒布朗|詹姆斯|LeBron"),
    (203999, "Nikola Jokic", "约基奇|約基奇|Jokic|Jokić"),
    (1627750, "Jamal Murray", "贾马尔.*穆雷|賈馬爾.*穆雷|Jamal Murray"),
    (1629639, "Tyler Herro", "希罗|希羅|Tyler Herro"),
    (1628389, "Bam Adebayo", "阿德巴约|阿德巴約|阿德巴耶|Bam Adebayo"),
    (201142, "Kevin Durant", "杜兰特|杜蘭特|Durant"),
    (203507, "Giannis Antetokounmpo", "字母哥|阿德托昆博|Giannis|Antetokounmpo"),
    (203076, "Anthony Davis", "安东尼.*戴维斯|戴维斯|Anthony Davis"),
    (1628983, "Shai Gilgeous-Alexander", "亚历山大|亞歷山大|Gilgeous|Shai"),
    (1628973, "Jalen Brunson", "布伦森|布倫森|Brunson"),
    (1628378, "Donovan Mitchell", "米切尔|米切爾|Donovan Mitchell"),
    (1627759, "Jaylen Brown", "杰伦.*布朗|Jaylen Brown"),
    (1628369, "Jayson Tatum", "塔图姆|塔圖姆|Tatum"),
    (1630162, "Anthony Edwards", "爱德华兹|愛德華茲|Anthony Edwards"),
    (1630578, "Alperen Sengun", "申京|Sengun|Şengün"),
    (202681, "Kyrie Irving", "欧文|歐文|Kyrie Irving"),
    (1626164, "Devin Booker", "布克|Devin Booker"),
    (203081, "Damian Lillard", "利拉德|Lillard"),
    (1630595, "Cade Cunningham", "坎宁安|坎寧安|Cunningham"),
    (1630560, "Cam Thomas", "卡姆.*托马斯|Cam Thomas"),
]

NON_ACTION = re.compile(r"festival|expo\b|ceremony|fan.?meet|press conference|red carpet|presentation|visit to|meet.and.greet|fashion|concert|stage event|球迷见面|球迷見面|访华|訪華|舞台|发布会|發佈會|颁奖|頒獎|庆典|慶典|走秀|商业活动", re.I)


def subjects(text, queries=()):
    result = []
    for person_id, name, aliases in NBA:
        if re.search(aliases, text + " " + " ".join(queries), re.I):
            result.append({"name": name, "aliases": list(dict.fromkeys([name] + [x for x in aliases.split("|") if ".*" not in x])), "nba_id": person_id, "kind": "person"})
    for value in queries:
        words = re.findall(r"[A-Za-zÀ-öø-ÿ]+(?:[-’'][A-Za-zÀ-öø-ÿ]+)*", value)
        if 2 <= len(words) <= 4:
            name = " ".join(words)
            if not any(name.casefold() in (r["name"] + " " + " ".join(r["aliases"])).casefold() for r in result):
                result.append({"name": name, "aliases": [name], "kind": "subject"})
    return result[:4]


def matches(value, subject):
    normalized = str(value).casefold().replace("_", " ").replace("č", "c").replace("ć", "c")
    return any(alias.casefold().replace("č", "c").replace("ć", "c") in normalized for alias in subject.get("aliases", [subject["name"]]))


def title_matches(value, subject):
    import unicodedata
    normalize = lambda t: "".join(c for c in unicodedata.normalize("NFKD", t).casefold() if not unicodedata.combining(c)).replace("_", " ")
    text = normalize(str(value))
    words = re.findall(r"[a-z]+", normalize(subject["name"]))
    return len(words) >= 2 and all(w in text for w in words)
