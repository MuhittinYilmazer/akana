"""Root conftest — explicit plugin loading for the canonical (autoload-off) runners.

The canonical test entry points (``akana.py test`` / ``akana.py smoke``) set
``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` to keep broken ROS/ament *global* plugins out
of the isolated venv. That switch also blocks entry-point autoloading of
**pytest-asyncio** (a pinned dev dependency), so ``pytest.ini``'s
``asyncio_mode = auto`` never applied there and every ``async def`` test failed
with "async def functions are not natively supported" — which is why older test
files drive coroutines with ``asyncio.run`` by hand.

Declaring the plugin here (the ROOT conftest — pytest rejects ``pytest_plugins``
in non-root conftests) loads it explicitly from the venv WITHOUT re-opening the
global-plugin door, restoring the exact semantics of a normal run. New tests may
therefore use plain ``async def``.

The declaration is GATED on the same env check pytest itself uses: with autoload
ON, the entry point already registered this module under the name ``asyncio``,
and registering it again under its module path raises "Plugin already registered
under a different name".
"""

import os

if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
    pytest_plugins = ("pytest_asyncio.plugin",)
