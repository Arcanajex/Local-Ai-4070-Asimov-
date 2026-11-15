from slice import first_n


def test_first_two():
    assert first_n(["a", "b", "c"], 2) == ["a", "b"]


def test_short():
    assert first_n(["a"], 5) == ["a"]
