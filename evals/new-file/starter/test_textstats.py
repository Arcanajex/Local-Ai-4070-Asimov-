from textstats import count_words


def test_two_words():
    assert count_words("a b") == 2


def test_empty():
    assert count_words("") == 0
