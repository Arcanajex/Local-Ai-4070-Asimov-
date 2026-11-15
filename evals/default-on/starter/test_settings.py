from settings import enabled


def test_default_is_on():
    assert enabled(None) is True
    assert enabled() is True


def test_explicit():
    assert enabled(False) is False
    assert enabled(True) is True
