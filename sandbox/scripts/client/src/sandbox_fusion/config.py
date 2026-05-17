import os

SANDBOX_ENDPOINT = os.environ.get('SANDBOX_FUSION_ENDPOINT', 'http://localhost:8080')
# dataset services have different patterns compared to sandbox services, thus we may
# need to access these 2 sets of apis separately.
DATASET_ENDPOINT = os.environ.get('SANDBOX_FUSION_DATASET_ENDPOINT', SANDBOX_ENDPOINT)
