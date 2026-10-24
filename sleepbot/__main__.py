import os
from pathlib import Path

from .bot import main


def load_env(path: Path) -> None:
    """Подхватывает .env рядом с проектом; уже заданные переменные не перезаписывает."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env(Path(__file__).resolve().parent.parent / ".env")
main()
