#!/usr/bin/env python3
"""Build the Lunar Code knowledge JSON from the author's 111-page source guide.

The page ranges intentionally match author_moon.pdf: signs 4–15, houses 19–30,
aspects 31–35, and relaxation/rest practices 90–93 and 103. Extracted wording is
kept verbatim (apart from PDF line-wrap/footer cleanup).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SIGNS = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]
ASPECTS = ["Соединение", "Трин", "Секстиль", "Оппозиция", "Квадрат"]
ASPECT_SYMBOLS = {"Соединение": "☌", "Трин": "△", "Секстиль": "⚹", "Оппозиция": "☍", "Квадрат": "□"}
RELAXATION_PAGES = {90: "Дневник чувств", 91: "Дом как храм", 92: "Лунные ритмы", 93: "Тишина и вода", 103: "Отдых"}


def read_pdf(pdf_path: Path) -> tuple[list[str], int]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise SystemExit("Установите зависимости: python -m pip install pdfplumber") from exc
    with pdfplumber.open(pdf_path) as pdf:
        return [(page.extract_text() or "") for page in pdf.pages], len(pdf.pages)


def clean_page(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    lines = [line for line in lines if not re.fullmatch(r"\s*[—–-]\s*\d+\s*[—–-]\s*", line)]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def build_knowledge(pdf_path: Path) -> dict:
    pages, page_count = read_pdf(pdf_path)
    if page_count < 103:
        raise ValueError(f"Ожидалось не менее 103 страниц в авторском PDF, получено: {page_count}")

    signs = {}
    for page_number, sign in enumerate(SIGNS, start=4):
        text = clean_page(pages[page_number - 1])
        lines = text.splitlines()
        signs[sign] = {
            "source_page": page_number,
            "title": lines[1] if len(lines) > 1 else f"Луна в {sign}",
            "archetype": lines[2] if len(lines) > 2 else "",
            "text": text,
        }

    houses = {}
    for house_number, page_number in enumerate(range(19, 31), start=1):
        text = clean_page(pages[page_number - 1])
        lines = text.splitlines()
        houses[str(house_number)] = {
            "source_page": page_number,
            "title": lines[1] if len(lines) > 1 else f"Луна в {house_number} доме",
            "theme": lines[2] if len(lines) > 2 else "",
            "text": text,
        }

    aspects = {}
    for aspect, page_number in zip(ASPECTS, range(31, 36)):
        aspects[aspect] = {
            "source_page": page_number,
            "symbol": ASPECT_SYMBOLS[aspect],
            "text": clean_page(pages[page_number - 1]),
        }

    practices = {
        title: {"source_page": page_number, "text": clean_page(pages[page_number - 1])}
        for page_number, title in RELAXATION_PAGES.items()
    }
    relaxation_text = "\n\n".join(item["text"] for item in practices.values())
    full_text = "\n\n".join(f"===== PAGE {i} =====\n{clean_page(text)}" for i, text in enumerate(pages, 1) if text.strip())
    return {
        "source": {"file": pdf_path.name, "page_count": page_count, "language": "ru"},
        "signs": signs,
        "houses": houses,
        "aspects": aspects,
        "relaxation": {"source_pages": list(RELAXATION_PAGES), "practices": practices, "text": relaxation_text},
        "source_text": full_text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, default=Path("author_moon.pdf"))
    parser.add_argument("-o", "--output", type=Path, default=Path("data/moon_knowledge.json"))
    args = parser.parse_args()
    data = build_knowledge(args.pdf)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Сохранено {args.output}: {len(data['signs'])} знаков, {len(data['houses'])} домов, {len(data['aspects'])} аспектов, {len(data['source_text'])} символов исходного текста")


if __name__ == "__main__":
    main()
