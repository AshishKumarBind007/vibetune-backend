import os

# VibeTune cloud-ready recommendation dataset
DATASET_PATH = os.getenv(
    "VIBETUNE_DATASET_PATH",
    "vibetune_recommendation_data.parquet"
)
