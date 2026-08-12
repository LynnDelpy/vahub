"""vahub: a self-hosted voice assistant hub.

The shape of the system is one path:

    intent -> language model -> tool call -> policy gate -> module -> action

Everything else exists to serve or protect that path. The gate is code, not a
prompt: what the model may do is decided outside anything the model can write.
"""

from .__about__ import __version__

__all__ = ["__version__"]
