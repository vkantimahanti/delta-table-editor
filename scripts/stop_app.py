"""
Databricks notebook — scheduled to stop the Databricks App after hours.
Run As: app service principal — must have CAN_MANAGE.
No hardcoded tokens — uses the job's own identity via WorkspaceClient.
"""
import os
import logging
from datetime import datetime
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("stop_app")


def stop_app():
    app_name = os.environ.get("DATABRICKS_APP_NAME", "datacanvas")

    logger.info("Stop job triggered at %s", datetime.now())
    logger.info("Target app: %s", app_name)

    try:
        w = WorkspaceClient()
        app = w.apps.get(app_name)
        logger.info("Current app status: %s",
                    app.compute_status.state if app.compute_status else "unknown")

        w.apps.stop(app_name)
        logger.info("Stop command sent successfully for: %s", app_name)

    except NotFound:
        logger.error("App '%s' not found. Check DATABRICKS_APP_NAME.", app_name)
        raise
    except PermissionDenied:
        logger.error(
            "Permission denied. Grant CAN_MANAGE to the app service "
            "principal on the app permissions page."
        )
        raise
    except Exception as e:
        logger.error("Failed to stop app: %s", e)
        raise


if __name__ == "__main__":
    stop_app()
