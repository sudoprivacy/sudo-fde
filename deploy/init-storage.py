import os
import time

import boto3

client = boto3.client(
    "s3",
    endpoint_url=os.environ["FDE_S3_ENDPOINT"],
    region_name=os.environ.get("FDE_S3_REGION", "us-east-1"),
)
bucket = os.environ["FDE_S3_BUCKET"]
for _ in range(60):
    try:
        client.head_bucket(Bucket=bucket)
        break
    except Exception:
        try:
            client.create_bucket(Bucket=bucket)
            break
        except Exception:
            time.sleep(2)
else:
    raise SystemExit("object storage bucket was not ready")
