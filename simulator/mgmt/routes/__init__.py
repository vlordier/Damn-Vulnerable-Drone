# simulator/mgmt/routes/__init__.py
from flask import Blueprint

# Single blueprint used across all route modules
bp = Blueprint("main", __name__)

# Import modules so their routes register on the blueprint
from . import (
    bridge,  # noqa: F401
    errors,  # noqa: F401
    gcs,  # noqa: F401
    pages_attacks,  # noqa: F401
    pages_guide,  # noqa: F401
    pages_learning,  # noqa: F401
    stages,  # noqa: F401
)

# Expose as "main" for app.register_blueprint(main)
main = bp