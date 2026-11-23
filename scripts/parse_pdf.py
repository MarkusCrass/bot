#!/usr/bin/env python3
"""Extract author text from a PDF into the moon_knowledge.json structure.

Recognized headings include zodiac signs, houses (e.g. "Луна в 1 доме"),
the "Расслабление" block. Unclassified text is retained under source_text
so that extraction never silently discards useful source material.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SIGNS = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]


def extract_text(path: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        if text.strip():
            return text
    except ImportError:
        pass
    except Exception as exc:
        print(f"pdfplumber не смог прочитать PDF ({exc}); пробую pypdf…")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit("Установите pdfplumber или pypdf: pip install -r requirements.txt") from exc
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_sections(text: str) -> dict:
    sections: dict[str, str] = {"signs": {}, "houses": {}, "relaxation": "", "source_text": text.strip()}
    current: tuple[str, str] | None = None
    buffer: list[str] = []

    def save() -> None:
        nonlocal buffer
        if current and buffer:
            value = "\n".join(buffer).strip()
            if current[0] == "sign":
                sections["signs"][current[1]] = value
            elif current[0] == "house":
                sections["houses"][current[1]] = value
            elif current[0] == "relaxation":
                sections["relaxation"] = value
        buffer = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if buffer:
                buffer.append("")
            continue
        sign = next((s for s in SIGNS if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", stripped, re.I)), None)
        house_match = re.search(r"(?:дом(?:е|а|ов)?\s*(1[0-2]|[1-9])|(?:луна\s*(?:в|:)?\s*)?(1[0-2]|[1-9])\s*(?:[-–—.]?\s*)?дом(?:е|а|ов)?\b|house\s*(1[0-2]|[1-9]))", stripped, re.I)
        relax = re.search(r"расслаблен|релакс", stripped, re.I)
        new: tuple[str, str] | None = None
        if relax:
            new = ("relaxation", "")
        elif house_match:
            house_number = next(group for group in house_match.groups() if group is not None)
            new = ("house", str(int(house_number)))
        elif sign and (len(stripped) < 80 or stripped.lower().startswith(("луна", "знак"))):
            new = ("sign", sign)
        if new:
            save()
            current = new
            # Drop likely heading text; retain remainder after a colon or dash.
            remainder = re.split(r"[:—–]\s*", stripped, maxsplit=1)
            if len(remainder) == 2 and len(remainder[1]) > 20:
                buffer.append(remainder[1])
        elif current:
            buffer.append(stripped)
    save()
    return sections


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="исходный PDF автора")
    parser.add_argument("-o", "--output", type=Path, default=Path("data/moon_knowledge.json"), help="путь JSON результата")
    args = parser.parse_args()
    text = extract_text(args.pdf)
    if not text.strip():
        raise SystemExit("Текст не извлечён. Возможно, PDF состоит из сканов — сначала выполните OCR.")
    result = parse_sections(text)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Сохранено: {args.output} (знаки: {len(result['signs'])}, дома: {len(result['houses'])}, символов: {len(text)})")


if __name__ == "__main__":
    main()
