def count_words(text):
    if not text or not str(text).strip():
        return 0
    return len(str(text).split())
