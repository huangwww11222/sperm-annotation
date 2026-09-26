"""Register the reviewed-data workflow and gated training export API."""

from . import training_export
from .confirmation_workflow_routes import final_router
from .confirmation_workflow_routes import router as confirm_router
from .db import connect
from .review_schema import apply_review_schema
from .training_export_routes import legacy_router
from .training_export_routes import router as dataset_router


def register_review_routers(app) -> None:
    """Install review, confirmation, final-versions and dataset routers."""
    from .review_logging import configure_review_logging

    configure_review_logging()
    from .review_workflow_routes import router as workflow_router

    app.include_router(workflow_router)
    app.include_router(confirm_router)
    app.include_router(final_router)
    app.include_router(dataset_router)
    app.include_router(legacy_router)
    # Ensure review schema applied (idempotent)
    from .db import _DB_LOCK

    with _DB_LOCK, connect() as conn:
        apply_review_schema(conn)
        from .review_workflow import migrate

        migrate(conn)
        from .confirmation_workflow import migrate as migrate_confirmation

        migrate_confirmation(conn)
        training_export.migrate(conn)
        conn.commit()
