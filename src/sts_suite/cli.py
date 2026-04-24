"""Single entry point — launches the Textual TUI."""

from __future__ import annotations


def main() -> None:
    from .tui import run

    run()


if __name__ == "__main__":
    main()
