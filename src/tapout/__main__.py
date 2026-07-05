"""Enable `python -m tapout` (fallback when uvx is unavailable)."""

from .cli import main

if __name__ == "__main__":
    main()
