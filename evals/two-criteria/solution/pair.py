def parse_pair(text):
    if "=" not in text:
        raise ValueError("expected key=value")
    key, value = text.split("=", 1)
    return {"key": key, "value": value}
