"""o4_engine — the Controller layer of Ortho4XP's one-engine / many-views
architecture (docs/specs/engine-protocol-multi-gui.md).

Views import :class:`EngineSession` and the event types; core pipeline
modules never import this package (they reach it through the
``O4_UI_Utils.engine_session`` attribute the session registers).
"""

from .events import PROTOCOL_VERSION  # noqa: F401
from .session import EngineSession    # noqa: F401
