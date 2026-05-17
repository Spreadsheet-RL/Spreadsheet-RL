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

from sandbox.utils.antihack import ACpp, APython

PY_CODE_0 = """
print('hello')
"""

PY_CODE_1 = """
exit(0)
"""

PY_CODE_2 = """
import os
os._exit(0)
"""

PY_CODE_3 = """
import sys
sys.exit(0)
"""

CPP_CODE_0 = """
int main() {
    return 0;
}
"""

CPP_CODE_1 = """
#include <cstdlib>
int main() {
    exit(0);
}
"""


async def test_apython_expand_code():
    from sandbox.datasets.types import RunCodeRequest, RunStatus
    from sandbox.utils.sandbox_client import run_code_in_sandbox

    async def execute(code):
        r = await run_code_in_sandbox(RunCodeRequest(code=code, language='python'))
        accepted = r.status == RunStatus.Success
        return accepted

    code = APython.expand_code(PY_CODE_0)
    accepted = await execute(code)
    assert accepted == True

    code = APython.expand_code(PY_CODE_1)
    accepted = await execute(code)
    assert accepted == False

    code = APython.expand_code(PY_CODE_2)
    accepted = await execute(code)
    assert accepted == False

    code = APython.expand_code(PY_CODE_3)
    accepted = await execute(code)
    assert accepted == False


async def test_acpp_expand_code():
    from sandbox.datasets.types import RunCodeRequest, RunStatus
    from sandbox.utils.sandbox_client import run_code_in_sandbox

    async def execute(code):
        r = await run_code_in_sandbox(RunCodeRequest(code=code, language='cpp'))
        accepted = r.status == RunStatus.Success
        return accepted

    code = ACpp.expand_code(CPP_CODE_0)
    accepted = await execute(code)
    assert accepted == True

    code = ACpp.expand_code(CPP_CODE_1)
    accepted = await execute(code)
    assert accepted == False
