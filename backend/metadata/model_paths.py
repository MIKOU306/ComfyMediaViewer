"""Resolve model files within an explicitly registered root before starting I/O."""
from pathlib import Path
import stat


def resolve_model_path(root: str | Path, relative_path: str) -> Path:
    """Reject traversal and reparse points, including Windows junctions.

    This is a preflight boundary, not protection against subsequent filesystem
    mutation. The verification worker and confirmation transaction must recheck.
    """
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Model path must be a non-empty relative path")
    relative = Path(relative_path)
    if relative.is_absolute() or relative.drive or relative.root or ".." in relative.parts:
        raise ValueError("Model path must stay within the registered root")
    if any(":" in part for part in relative.parts):
        raise ValueError("Alternate file streams are not model files")
    base = Path(root).absolute()
    current = base
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        observed = current.lstat()
        if stat.S_ISLNK(observed.st_mode) or (
            getattr(observed, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("Model paths must not contain symlinks or reparse points")
    resolved_base = base.resolve(strict=True)
    target = current.resolve(strict=True)
    if not resolved_base.is_dir() or not target.is_relative_to(resolved_base) or not target.is_file():
        raise ValueError("Model path must identify a regular file inside its root")
    return target
