# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import json
import os
import random
import secrets
import string
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Dict


def generate_random_string(length):
    characters = string.ascii_letters + string.digits
    random_string = ''.join(secrets.choice(characters) for _ in range(length))
    return random_string


def random_cgroup_name() -> str:
    return ''.join(random.choice(string.ascii_lowercase) for _ in range(6))


def cached_context(cm_factory):

    def hash_args(args, kwargs):
        return str(args) + str(kwargs)

    pool = defaultdict(list)

    @functools.wraps(cm_factory)
    @asynccontextmanager
    async def wrapper(*args, **kwargs):
        key = hash_args(args, kwargs)
        if pool[key]:
            resource = pool[key].pop()
        else:
            cm_instance = cm_factory(*args, **kwargs)
            resource = await cm_instance.__aenter__()

        yield resource

        pool[key].append(resource)

    return wrapper


def find_conda_root():
    try:
        python_executable = sys.executable
        env_root = python_executable
        current_dir = env_root

        while current_dir:
            if env_root != current_dir and os.path.exists(os.path.join(current_dir, 'condabin')):
                # This indicates we are in a Conda environment
                conda_root = current_dir
                break
            parent_dir = os.path.dirname(current_dir)
            if parent_dir == current_dir:
                # We have reached the root of the filesystem
                conda_root = None
                break
            current_dir = parent_dir

        if conda_root and os.path.isdir(conda_root):
            return conda_root
        else:
            return "Conda root directory not found."
    except Exception as e:
        return f"An unexpected error occurred: {e}"


def set_permissions_recursively(path, mode):
    for root, dirs, files in os.walk(path):
        for dir_name in dirs:
            os.chmod(os.path.join(root, dir_name), mode)
        for file_name in files:
            os.chmod(os.path.join(root, file_name), mode)
    os.chmod(path, mode)


def ensure_php_tag_in_string(php_code: str) -> str:
    """
    Ensure that a string containing PHP code starts with <?php tag.
    
    :param php_code: A string containing PHP code
    :return: The PHP code string, with <?php tag prepended if it was missing
    """
    php_code = php_code.lstrip()
    if not php_code.startswith('<?php'):
        php_code = '<?php\n' + php_code
    return php_code


def ensure_json(obj: Dict[str, Any], key: str) -> Dict[str, Any]:
    if isinstance(obj[key], str):
        obj[key] = json.loads(obj[key], strict=False)
    return obj[key]


def truncate_str(s: str, max_length: int = 1000, placeholder: str = '...') -> str:
    """
    Truncate string if it exceeds max_length by keeping both ends and adding placeholder in middle
    
    Args:
        s: Input string
        max_length: Maximum length limit, defaults to 1000
        placeholder: String used as placeholder, defaults to '...'
        
    Returns:
        Processed string, either original or truncated
    """
    if not s or len(s) <= max_length:
        return s

    # Ensure at least 1 character on each side of placeholder
    if max_length < len(placeholder) + 2:
        max_length = len(placeholder) + 2

    # Calculate length to keep at beginning and end
    keep_length = (max_length - len(placeholder)) // 2

    return s[:keep_length] + placeholder + s[-keep_length:]


def load_jsonl(file_path):
    with open(file_path, 'r') as f:
        data = [json.loads(line) for line in f.readlines()]
    return data
