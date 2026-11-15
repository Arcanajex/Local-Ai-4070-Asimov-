from greet import main


def test_missing_name_exits_2(capsys):
    assert main([]) == 2
    assert "NAME" in capsys.readouterr().out


def test_hello():
    assert main(["Ada"]) == 0
