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

import tempfile

from sandbox.runners.base import restore_files, run_command_bare
from sandbox.runners.major import get_python_rt_env
from sandbox.runners.types import CodeRunArgs, CodeRunResult, CommandRunStatus
from sandbox.utils.execution import max_concurrency

run_command_compile = max_concurrency(12)(run_command_bare)
run_command_run = max_concurrency(1)(run_command_bare)


async def run_cuda(args: CodeRunArgs) -> CodeRunResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        # here we do not write code to specific entry but use the files as the whole project
        restore_files(tmp_dir, args.files)

        compile_res = await run_command_compile(f'mkdir build && cd build && cmake .. && make -j 4',
                                                timeout=args.compile_timeout,
                                                cwd=tmp_dir,
                                                extra_env=get_python_rt_env('sandbox-runtime'))

        run_res = None
        if compile_res.status == CommandRunStatus.Finished and compile_res.return_code == 0:
            run_res = await run_command_run(f'./build/main', timeout=args.run_timeout, cwd=tmp_dir, stdin=args.stdin)
        return CodeRunResult(compile_result=compile_res, run_result=run_res)


async def run_python_gpu(args: CodeRunArgs) -> CodeRunResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        # here we do not write code to specific entry but use the files as the whole project
        restore_files(tmp_dir, args.files)

        compile_res = await run_command_compile(f'python compile.py',
                                                timeout=args.compile_timeout,
                                                cwd=tmp_dir,
                                                extra_env=get_python_rt_env('sandbox-runtime'))

        run_res = None
        if compile_res.status == CommandRunStatus.Finished and compile_res.return_code == 0:
            run_res = await run_command_run(f'python run.py',
                                            timeout=args.run_timeout,
                                            cwd=tmp_dir,
                                            stdin=args.stdin,
                                            extra_env=get_python_rt_env('sandbox-runtime'))
        return CodeRunResult(compile_result=compile_res, run_result=run_res)


GPU_RUNNERS = {
    'cuda': run_cuda,
    'python_gpu': run_python_gpu,
}
