---
test_command: python -m pytest -q
---

# Smoke task 2: text stats

Create `textstats.py` with these functions, standard library only:

- `word_count(text: str) -> int`: number of words. Words are runs of letters, digits, or apostrophes; punctuation is ignored. Empty or whitespace-only text returns 0.
- `top_words(text: str, n: int = 3) -> list[tuple[str, int]]`: the `n` most common words, lowercased, as `(word, count)` pairs sorted by count descending, then alphabetically for ties. Raise `ValueError` if `n` is negative.
- `average_word_length(text: str) -> float`: mean word length rounded to 2 decimals, or `0.0` when there are no words.

Create `test_textstats.py` with pytest tests covering normal input, punctuation, mixed case, ties, empty text, and the `ValueError` case.

Add docstrings and type hints. Do not add dependencies. The test command is `python -m pytest -q`.
