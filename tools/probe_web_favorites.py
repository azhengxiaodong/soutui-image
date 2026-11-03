"""接口只读实测入口：用现有会话一次读取收藏，保存不含授权信息的证据。"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.web_session import MusefitReadClient


def main():
    client = MusefitReadClient(ROOT / "data" / "local" / "musefit_session.dpapi")
    start = time.perf_counter()
    data = client.get_json("/api/web/favorites", {"mediaType": "image"})
    elapsed = round(time.perf_counter() - start, 3)
    target = ROOT / "validation" / "site_api" / "authenticated_favorites.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"elapsed_seconds": elapsed, "response": data}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"elapsed_seconds": elapsed, "response_keys": list(data),
                      "success": data.get("success"), "folder_count": len(data.get("folders", [])),
                      "favorite_count": len(data.get("favorites", [])), "total": data.get("total")}, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=True))
        raise SystemExit(2)
