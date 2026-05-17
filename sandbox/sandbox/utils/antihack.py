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

import re
from abc import ABC, abstractmethod


class AntiHackBase(ABC):

    @staticmethod
    @abstractmethod
    def expand_code(code: str) -> str:
        ...

    @staticmethod
    @abstractmethod
    def judge(code: str) -> bool:
        return True


PY_HEADER = """
# --- anti-hack code begin
import os
import sys
exit = None
os._exit = None
sys.exit = None
# --- anti-hack code end
"""


class APython(AntiHackBase):

    @staticmethod
    def expand_code(code: str) -> str:
        out = f'{PY_HEADER}\n\n{code}'
        return out

    @staticmethod
    def judge(code: str) -> bool:
        r = re.findall(r"exit\(\s*0\s*\)", code)
        return False if r else True


CPP_HEADER = """
// --- anti-hack code begin
#include <cstdlib>

void exit(int) {
    std::abort();
}
// --- anti-hack code end
"""


class ACpp(AntiHackBase):

    @staticmethod
    def expand_code(code: str) -> str:
        out = f'{CPP_HEADER}\n\n{code}'
        return out

    @staticmethod
    def judge(code: str) -> bool:
        r = re.findall(r"exit\(\s*0\s*\)", code)
        return False if r else True


antis = {
    'python': APython,
    'cpp': ACpp,
}
