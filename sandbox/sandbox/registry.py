import importlib
from typing import List

from sandbox.configs.run_config import RunConfig
from sandbox.datasets.types import CodingDataset

config = RunConfig.get_instance_sync()

classes = {
    item.class_name: {
        'class': getattr(importlib.import_module(item.module_path), item.class_name),
        'dataset_ids': item.dataset_tables.keys(),
    } for item in config.dataset.registry
}


def get_coding_class_by_dataset(dataset_id) -> CodingDataset:
    for item in classes.values():
        if dataset_id in item['dataset_ids']:
            return item['class']


def get_coding_class_by_name(class_name):
    if class_name in classes:
        return classes[class_name]['class']


def get_all_dataset_ids() -> List[str]:
    return sum([list(i['dataset_ids']) for i in classes.values()], [])
