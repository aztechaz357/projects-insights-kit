"""setup.html に配布用の 2 ファイル (ワークフローと insights_kit.py) の中身を埋め込み直す。

ワークフローや insights_kit.py を変更したら実行する:  python tools/sync_setup_html.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "setup.html"
EMBEDS = {
    "workflow-template": ROOT / ".github" / "workflows" / "insights-kit.yml",
    "kit-template": ROOT / ".github" / "insights-kit" / "insights_kit.py",
}


def embedded(html: str, tag_id: str) -> str:
    m = re.search(rf'<script type="text/plain" id="{tag_id}">\n?(.*?)</script>', html, re.S)
    if not m:
        raise SystemExit(f"setup.html に id={tag_id} の埋め込みがありません")
    return m.group(1)


def main() -> None:
    html = HTML.read_text(encoding="utf-8")
    for tag_id, path in EMBEDS.items():
        body = path.read_text(encoding="utf-8")
        if "</script" in body.lower():
            raise SystemExit(f"{path.name} に </script が含まれていて埋め込めません")
        html = re.sub(rf'(<script type="text/plain" id="{tag_id}">).*?(</script>)',
                      lambda m: m.group(1) + "\n" + body + m.group(2), html, count=1, flags=re.S)
    HTML.write_text(html, encoding="utf-8", newline="\n")
    print("setup.html を更新しました")


if __name__ == "__main__":
    main()
