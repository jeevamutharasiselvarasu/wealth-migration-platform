import logging

logger = logging.getLogger("DataPlaneTelemetry")

class DataPlaneTelemetryPublisher:
    """Publishes execution telemetry from Data Plane to Control Plane."""

    @staticmethod
    def record_entity_metrics(run_id: str, entity_name: str, record_count: int, execution_time_sec: float):
        logger.info(f"Telemetry [Run: {run_id}] Entity: {entity_name} | Records: {record_count} | Duration: {execution_time_sec}s")