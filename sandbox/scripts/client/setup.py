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

from setuptools import setup, find_packages


def get_pydantic_requirement():
    try:
        import pydantic
        pydantic_version = pydantic.__version__
        if int(pydantic_version.split('.')[0]) < 2:
            print("Pydantic v1 detected. Installing compatible version.")
            return "pydantic>=1.0.0,<2.0.0"
        else:
            print("Pydantic v2 detected. Installing compatible version.")
            return "pydantic>=2.0.0"
    except ImportError:
        print("Pydantic not found. Installing latest version.")
        return "pydantic>=2.0.0"


setup(
    name="sandbox_fusion",
    version="0.3.4",
    packages=find_packages(),
    install_requires=[
        "requests>=2.0.0",
        "tenacity>=8.0.0",
        get_pydantic_requirement(),
    ],
)
