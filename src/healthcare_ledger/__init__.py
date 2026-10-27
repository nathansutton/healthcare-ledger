"""healthcare-ledger: dated histories of the U.S. health care provider registries and the links between them."""

__version__ = "0.1.0"

from .read import connect  # noqa: E402  the reader's entry point: healthcare_ledger.connect(export, graph)

__all__ = ["connect"]
