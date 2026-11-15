"""Small desktop helper. The window stays open until it is closed."""

from pathlib import Path


def output_file(folder, name):
    return Path(folder) / str(name)


def build(root):
    import tkinter as tk

    frame = tk.Frame(root)
    frame.pack(fill="both", expand=True)
    label = tk.Label(frame, text="Items")
    label.pack()
    return frame


def main():
    path = output_file(Path("data"), "items.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    import tkinter as tk

    root = tk.Tk()
    root.title(str(path))
    build(root)
    root.mainloop()


if __name__ == "__main__":
    main()
