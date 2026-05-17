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

import asyncio
import base64
import json
import os
import shutil
import tempfile

import structlog

from sandbox.runners.base import restore_files, run_commands
from sandbox.runners.major import get_python_rt_env
from sandbox.runners.types import CodeRunArgs, CommandRunStatus, RunJupyterRequest, RunJupyterResult
from sandbox.utils.execution import get_tmp_dir

logger = structlog.stdlib.get_logger()


async def run_jupyter(args: RunJupyterRequest) -> RunJupyterResult:
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)

        deps_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/jupyter'))
        shutil.copy2(os.path.join(deps_dir, 'main.py'), tmp_dir)
        fn = os.path.join(tmp_dir, 'tmp/sandbox/configs/input.json')
        os.makedirs(os.path.dirname(fn))
        with open(fn, 'w') as f:
            json.dump(
                {
                    'kernel': args.kernel,
                    'cells': args.cells,
                    'cell_timeout': args.cell_timeout,
                    'total_timeout': args.total_timeout,
                },
                f,
                indent=2)

        driver = await run_commands(None,
                                    f'python main.py',
                                    tmp_dir,
                                    get_python_rt_env('sandbox-runtime'),
                                    CodeRunArgs(code='',
                                                run_timeout=args.total_timeout + 10,
                                                fetch_files=args.fetch_files + ['tmp/sandbox/configs/output.json']),
                                    netns_no_bridge=True)

        if driver.run_result.status != CommandRunStatus.Finished:
            return RunJupyterResult(status=CommandRunStatus.Error, driver=driver.run_result)
        if 'tmp/sandbox/configs/output.json' not in driver.files:
            return RunJupyterResult(status=CommandRunStatus.Error,
                                    driver=driver.run_result,
                                    cells=[],
                                    files=driver.files)
        output = json.loads(base64.b64decode(driver.files['tmp/sandbox/configs/output.json'].encode()).decode())
        del driver.files['tmp/sandbox/configs/output.json']
        return RunJupyterResult(status=output['status'],
                                driver=driver.run_result,
                                cells=output['cells'],
                                files=driver.files)
