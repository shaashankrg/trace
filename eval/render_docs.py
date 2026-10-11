"""Render assistant/doc_templates/ into assistant/docs/ using values from facts.yaml.

Usage: python -m eval.render_docs
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FACTS_PATH = ROOT / "assistant" / "facts.yaml"
TEMPLATES_DIR = ROOT / "assistant" / "doc_templates"
DOCS_DIR = ROOT / "assistant" / "docs"
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load_facts() -> dict:
    return yaml.safe_load(FACTS_PATH.read_text(encoding="utf-8"))


def render(template: str, facts: dict) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in facts:
            raise KeyError(f"unknown fact {key!r}")
        return str(facts[key])

    return PLACEHOLDER.sub(sub, template)


def main() -> None:
    facts = load_facts()
    DOCS_DIR.mkdir(exist_ok=True)
    for old in DOCS_DIR.glob("*.md"):
        old.unlink()
    templates = sorted(TEMPLATES_DIR.glob("*.md"))
    for path in templates:
        (DOCS_DIR / path.name).write_text(
            render(path.read_text(encoding="utf-8"), facts), encoding="utf-8"
        )
    print(f"rendered {len(templates)} docs into {DOCS_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
