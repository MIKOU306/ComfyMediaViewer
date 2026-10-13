"""Raw media metadata readers; they do not mutate the catalog or inspect user files."""

from .raw_png import read_png_text

__all__ = ["read_png_text"]
