"""
plugins_impl/ - Concrete judge plugin implementations.

Importing this package side-effect-registers every plugin with the
judge.plugins registry. Add a new plugin by:
  1. Writing a class that implements JudgePlugin in a new module here.
  2. Calling judge.plugins.register(MyPlugin()) at module bottom.
  3. Importing the module from this __init__.py so it loads.
"""

# Side-effect imports register each plugin into the registry.
from . import citation_integrity  # noqa: F401
from . import url_health  # noqa: F401
from . import json_parses  # noqa: F401
