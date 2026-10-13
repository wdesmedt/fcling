"""The lenses. Importing this package registers them all."""

from ..lens import LENSES
from . import endpoints, path, service, where  # noqa: F401 - registers the lenses

__all__ = ["LENSES"]
