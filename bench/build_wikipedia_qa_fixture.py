"""Build bench/fixtures/wikipedia_qa.json — natural Wikipedia QA fixture.

Each task: 10-20 Wikipedia paragraphs as context (one per message),
question about ONE specific fact from the article, short expected answer.
No injected distractors — all paragraphs are about the same topic,
but only 1-2 directly answer the question.

This tests ContextCompress on natural long-form text where relevance
is graded (some paragraphs more relevant than others) rather than binary
(supporting vs. injected distractor).
"""
from __future__ import annotations
import json, re, time
from pathlib import Path
import wikipedia  # type: ignore

wikipedia.set_lang("en")

OUTPUT = Path(__file__).parent / "fixtures" / "wikipedia_qa.json"
MIN_PARAGRAPHS = 8
MAX_PARAGRAPHS = 18

# Pre-selected Q&A pairs: (question, wikipedia_page_title, expected_answer_substring)
# Expected is a short phrase that must appear (case-insensitive substring) in the
# model's answer. Verified against Wikipedia at writing time.
QA_PAIRS: list[tuple[str, str, str]] = [
    # Science / Technology
    ("In what year was the theory of general relativity published by Einstein?", "General relativity", "1915"),
    ("What is the chemical symbol for gold?", "Gold", "Au"),
    ("How many bones are in the adult human body?", "Human skeleton", "206"),
    ("What planet is known as the Red Planet?", "Mars", "Mars"),
    ("What is the powerhouse of the cell?", "Mitochondrion", "mitochondri"),
    ("What gas do plants absorb during photosynthesis?", "Photosynthesis", "carbon dioxide"),
    ("What is the speed of light in a vacuum in meters per second?", "Speed of light", "299"),
    ("What is the hardest natural substance on Earth?", "Diamond", "diamond"),
    ("In what year was the World Wide Web invented?", "World Wide Web", "1989"),
    ("What element has atomic number 1?", "Hydrogen", "hydrogen"),
    ("What is the largest organ of the human body?", "Skin", "skin"),
    ("What is the chemical formula for water?", "Water", "H2O"),
    ("Who developed the theory of evolution by natural selection?", "Charles Darwin", "Darwin"),
    ("What is the most abundant gas in Earth's atmosphere?", "Atmosphere of Earth", "nitrogen"),
    ("In what year did the first moon landing occur?", "Apollo 11", "1969"),
    ("What is the nucleus of an atom made of?", "Atomic nucleus", "proton"),
    ("What is the unit of electrical resistance?", "Ohm", "ohm"),
    ("What planet has the most moons in the solar system?", "Saturn", "Saturn"),
    # History
    ("In what year did World War II end?", "World War II", "1945"),
    ("Who was the first President of the United States?", "George Washington", "Washington"),
    ("In what year did the Berlin Wall fall?", "Berlin Wall", "1989"),
    ("What ancient wonder was located in Alexandria, Egypt?", "Lighthouse of Alexandria", "lighthouse"),
    ("Who wrote the Declaration of Independence?", "United States Declaration of Independence", "Jefferson"),
    ("In what year was the Magna Carta signed?", "Magna Carta", "1215"),
    ("What empire did Julius Caesar rule?", "Julius Caesar", "Rome"),
    ("In what year did the French Revolution begin?", "French Revolution", "1789"),
    ("Who was the first woman to win a Nobel Prize?", "Marie Curie", "Curie"),
    ("What city was the capital of the Byzantine Empire?", "Constantinople", "Constantinople"),
    ("In what year did the American Civil War end?", "American Civil War", "1865"),
    ("What country was Napoleon Bonaparte born in?", "Napoleon", "Corsica"),
    # Geography
    ("What is the longest river in the world?", "Nile", "Nile"),
    ("What is the capital city of Australia?", "Canberra", "Canberra"),
    ("What is the smallest country in the world by area?", "Vatican City", "Vatican"),
    ("What is the highest mountain in Africa?", "Mount Kilimanjaro", "Kilimanjaro"),
    ("What ocean is the largest?", "Pacific Ocean", "Pacific"),
    ("What is the capital of Canada?", "Ottawa", "Ottawa"),
    ("What is the largest desert in the world?", "Sahara", "Sahara"),
    ("Through how many countries does the Amazon River flow?", "Amazon River", "Brazil"),
    ("What is the deepest lake in the world?", "Lake Baikal", "Baikal"),
    ("What mountain range separates Europe from Asia?", "Ural Mountains", "Ural"),
    # Literature / Arts / Culture
    ("Who wrote the play Hamlet?", "Hamlet", "Shakespeare"),
    ("In what year was the Eiffel Tower completed?", "Eiffel Tower", "1889"),
    ("Who painted the Mona Lisa?", "Mona Lisa", "Leonardo"),
    ("What is the name of the hobbit in The Hobbit?", "The Hobbit", "Bilbo"),
    ("Who composed the Fifth Symphony?", "Symphony No. 5 (Beethoven)", "Beethoven"),
    ("In what city is the Colosseum located?", "Colosseum", "Rome"),
    ("Who wrote 1984?", "Nineteen Eighty-Four", "Orwell"),
    ("Who wrote Pride and Prejudice?", "Pride and Prejudice", "Austen"),
    ("What is the name of the famous clock tower in London?", "Big Ben", "Big Ben"),
    ("In what country was Leonardo da Vinci born?", "Leonardo da Vinci", "Italy"),
    # Economics / Society
    ("What is the most widely spoken language in the world?", "Mandarin Chinese", "Mandarin"),
    ("What is the currency of Japan?", "Japanese yen", "yen"),
    ("What does GDP stand for?", "Gross domestic product", "Gross Domestic Product"),
    ("What year was the United Nations founded?", "United Nations", "1945"),
    ("What country has the largest population?", "Demographics of India", "India"),
    ("What is the oldest university in the world?", "University of Bologna", "Bologna"),
    ("What is the name of the global climate agreement signed in Paris in 2015?", "Paris Agreement", "Paris Agreement"),
    ("In what year was the internet protocol TCP/IP standardized?", "Internet protocol suite", "1983"),
    ("What company was founded by Steve Jobs and Steve Wozniak?", "Apple Inc.", "Apple"),
    ("What is the name of the largest stock exchange in the world?", "New York Stock Exchange", "New York Stock Exchange"),
    # Biology / Medicine
    ("What is the most common blood type worldwide?", "Blood type", "O"),
    ("What is the name of the process by which cells divide?", "Mitosis", "mitosis"),
    ("What organ produces insulin?", "Pancreas", "pancreas"),
    ("What is the Latin name for the domestic dog?", "Dog", "Canis lupus familiaris"),
    ("How many chromosomes does a normal human cell have?", "Human genome", "46"),
    ("What protein carries oxygen in red blood cells?", "Hemoglobin", "hemoglobin"),
    ("What is the study of fungi called?", "Mycology", "mycology"),
    ("What is the name of the enzyme that unzips DNA during replication?", "DNA helicase", "helicase"),
]


def clean_paragraphs(text: str) -> list[str]:
    """Split Wikipedia article text into clean, non-trivial paragraphs."""
    raw = [p.strip() for p in text.split("\n") if p.strip()]
    # Drop very short paragraphs (headings, stubs) and keep substantive ones
    return [p for p in raw if len(p) >= 80 and not p.startswith("==")]


def build_fixture(pairs: list[tuple[str, str, str]], min_paras: int, max_paras: int) -> list[dict]:
    tasks = []
    for i, (question, title, expected) in enumerate(pairs):
        try:
            page = wikipedia.page(title, auto_suggest=False)
            paras = clean_paragraphs(page.content)
            if len(paras) < min_paras:
                print(f"  SKIP {title}: only {len(paras)} clean paragraphs")
                continue
            paras = paras[:max_paras]
            tasks.append({
                "task_id": f"wikipedia_qa_{i:04d}",
                "prompt": question,
                "expected": expected,
                "meta": {
                    "article_title": title,
                    "paragraphs": [{"title": title, "sentences": [p]} for p in paras],
                    "gold_answer": expected,
                    "n_paragraphs": len(paras),
                    "total_chars": sum(len(p) for p in paras),
                },
            })
            if len(tasks) % 10 == 0:
                print(f"  Built {len(tasks)} tasks so far...")
            time.sleep(0.3)  # polite rate limiting
        except Exception as e:
            print(f"  ERROR {title}: {e}")
            continue
    return tasks


if __name__ == "__main__":
    print(f"Building Wikipedia QA fixture from {len(QA_PAIRS)} Q&A pairs...")
    tasks = build_fixture(QA_PAIRS, MIN_PARAGRAPHS, MAX_PARAGRAPHS)
    print(f"\nBuilt {len(tasks)} tasks.")
    if tasks:
        avg_paras = sum(t["meta"]["n_paragraphs"] for t in tasks) / len(tasks)
        avg_chars = sum(t["meta"]["total_chars"] for t in tasks) / len(tasks)
        print(f"Avg paragraphs per task: {avg_paras:.1f}")
        print(f"Avg total chars per task: {avg_chars:.0f}")
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(tasks, indent=2))
        print(f"Wrote {OUTPUT}")
    else:
        print("No tasks built.")
