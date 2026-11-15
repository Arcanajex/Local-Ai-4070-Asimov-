def main(argv):
    if len(argv) != 1:
        print("usage: greet.py NAME")
        return 0
    print("hello " + argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
