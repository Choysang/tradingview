"""Export the actual FastAPI route contract; no duplicate hand-written route list."""
import argparse
import json
from pathlib import Path


def main() -> None:
    from backend.app.main import app

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).with_name("openapi.json")
    content = json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            raise SystemExit("OpenAPI drift: run python -m contracts.export_openapi")
        print("OpenAPI is current")
    else:
        target.write_text(content, encoding="utf-8", newline="\n")
        print(f"Exported {target}")


if __name__ == "__main__":
    main()
