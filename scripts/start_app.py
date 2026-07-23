"""
Databricks notebook — scheduled to run at 7:45 AM Mon–Fri.
Starts the Databricks App before users arrive at 8 AM.
Run As: app-1vwgx6 (app service principal — must have CAN_MANAGE).
No hardcoded tokens — uses the job's own identity via WorkspaceClient.
"""
import os
import time
import logging
from datetime import datetime
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("start_app")


def start_app():
    app_name = os.environ.get("DATABRICKS_APP_NAME", "datacanvas")

    logger.info("Start job triggered at %s", datetime.now())
    logger.info("Target app: %s", app_name)

    try:
        w = WorkspaceClient()

        app = w.apps.get(app_name)
        current_state = (app.compute_status.state
                         if app.compute_status else "UNKNOWN")
        logger.info("Current app status: %s", current_state)

        if current_state == "ACTIVE":
            logger.info("App is already running. Nothing to do.")
            return

        w.apps.start(app_name)
        logger.info("Start command sent for: %s", app_name)

        # Poll until ACTIVE or timeout
        for attempt in range(12):
            time.sleep(10)
            app = w.apps.get(app_name)
            state = (app.compute_status.state
                     if app.compute_status else "UNKNOWN")
            logger.info("Attempt %d/12 — status: %s", attempt + 1, state)
            if state == "ACTIVE":
                logger.info("App is ACTIVE and ready for users.")
                return

        logger.warning("App did not reach ACTIVE within 2 minutes. "
                       "Check Databricks Apps UI.")

    except NotFound:
        logger.error("App '%s' not found.", app_name)
        raise
    except PermissionDenied:
        logger.error(
            "Permission denied. Grant CAN_MANAGE to the app service "
            "principal on the app permissions page."
        )
        raise
    except Exception as e:
        logger.error("Failed to start app: %s", e)
        raise


if __name__ == "__main__":
    start_app()
