"""Pin pending work to its executable pipeline and prompt revision."""
import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache
def pipeline_revision():
    import ps
    root = Path(ps.__file__).parent
    hasher = hashlib.sha256()
    for file in sorted(root.rglob("*")):
        if file.is_file() and file.suffix in {".py", ".md", ".yaml"}:
            hasher.update(str(file.relative_to(root)).encode())
            hasher.update(file.read_bytes())
    for name in ("pattern.py", "router.py"):
        hasher.update(Path(__file__).with_name(name).read_bytes())
    return hasher.hexdigest()
