"""Enable `python -m tapout` (fallback when uvx is unavailable)."""

from .cli import app

if __name__ == "__main__":
    app()
