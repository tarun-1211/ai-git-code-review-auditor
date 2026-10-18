"""DynamoDB item storage and a conservative claim with no automatic reclaim."""

from datetime import datetime, timezone

from .errors import IntegrationError


def _now():
    return datetime.now(timezone.utc).isoformat()


class DynamoDBStore:
    def __init__(self, settings, *, table=None):
        if table is None:
            import boto3
            from botocore.config import Config
            table = boto3.resource("dynamodb", region_name=settings.region,
                                   config=Config(connect_timeout=5, read_timeout=10,
                                                 retries={"total_max_attempts": 1})).Table(settings.table_name)
        self._table = table
        self.model_id = settings.model_id

    def claim(self, pr):
        try:
            self._table.put_item(Item={
                "review_id": pr.review_id, "repository": pr.repository,
                "pr_number": pr.number, "head_sha": pr.head_sha,
                "model_id": self.model_id, "status": "processing", "created_at": _now(),
            }, ConditionExpression="attribute_not_exists(review_id)")
            return True
        except Exception as error:
            response = getattr(error, "response", {})
            details = response.get("Error") if isinstance(response, dict) else None
            if isinstance(details, dict) and details.get("Code") == "ConditionalCheckFailedException":
                return False
            raise IntegrationError("storage_claim_failed") from None

    def transition(self, pr, expected, status, **fields):
        values = {":expected": expected, ":status": status, ":updated": _now()}
        names = {"#status": "status", "#updated": "updated_at"}
        assignments = ["#status = :status", "#updated = :updated"]
        for index, (key, value) in enumerate(fields.items()):
            names[f"#f{index}"] = key
            values[f":f{index}"] = value
            assignments.append(f"#f{index} = :f{index}")
        try:
            self._table.update_item(
                Key={"review_id": pr.review_id},
                ConditionExpression="#status = :expected",
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names, ExpressionAttributeValues=values,
            )
        except Exception:
            raise IntegrationError("storage_update_failed") from None
