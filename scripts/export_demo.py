"""Export a clearly marked synthetic example; never touches a publishing platform."""
import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from content_engine.engine import Engine


def main():
    parser = argparse.ArgumentParser(description="生成标明合成演示的图文包")
    parser.add_argument("--output-dir", default=str(ROOT / "dist"))
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    engine = Engine(ROOT / "data" / "release-example")
    sample = engine.seed_demo()
    article = engine.article(sample["article_id"])
    if not engine.topic(article["topic_id"])["is_demo"]:
        raise RuntimeError("只允许脚本导出演示内容")
    engine.approve(article["id"], {"account_id": "demo", "revision_hash": article["revision_hash"], "facts_ack": True, "rights_ack": True, "ai_ack": True})
    content = engine.export_article(article["id"])
    archive = output / "合成演示-图文包.zip"
    archive.write_bytes(content)
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        if bundle.testzip() is not None or not json.loads(bundle.read("manifest.json"))["is_demo"]:
            raise RuntimeError("演示导出包检查失败")
        (output / "合成演示-封面.png").write_bytes(bundle.read("cover.png"))
    print(json.dumps({"example_archive": str(archive), "is_demo": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
