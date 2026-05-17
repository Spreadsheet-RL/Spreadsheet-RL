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
import json
import os
from typing import Any, List, Literal
from pydantic import BaseModel
from jupyter_client import AsyncKernelManager

class InputFile(BaseModel):
    kernel: Literal['python3']
    cells: List[str]
    cell_timeout: float = 0
    total_timeout: float = 120

class OutputFile(BaseModel):
    status: Literal['Finished', 'TimeLimitExceeded', 'Error']
    cells: List[Any]

async def get_healthy_jupyter(config):
    while True:
        try:
            km = AsyncKernelManager(kernel_name=config.kernel)
            await km.start_kernel()
            kc = km.client()
            kc.start_channels()
            await kc.wait_for_ready()

            stdout = ''
            def hook(msg):
                nonlocal stdout
                msg_type = msg["header"]["msg_type"]
                content = msg["content"]
                if msg_type == "stream" and content["name"] == 'stdout':
                    stdout += content["text"]
            await kc.execute_interactive('print(123)', output_hook=hook, timeout=2)
            if stdout != '123\n':
                raise Exception('incorrect kernel')
            return km, kc
        except Exception as e:
            print(f'kernel failed to start, trying a new one. error: {e}')
            try:
                await km.shutdown_kernel()
            except Exception as err:
                print(f'failed to shutdown kernel, ignored: {err}')

async def main():
    with open(os.path.abspath(os.path.join(__file__, '../tmp/sandbox/configs/input.json'))) as f:
        config = InputFile(**json.load(f))
    print(f'config: \n{config.model_dump_json(indent=2)}')

    km, kc = await get_healthy_jupyter(config)
    results = []
    for cell in config.cells:
        cell_result = {
            'stdout': '',
            'stderr': '',
            'display': [],
            'error': [],
        }
        def hook(msg):
            msg_type = msg["header"]["msg_type"]
            content = msg["content"]
            if msg_type == "stream":
                cell_result[content["name"]] += content["text"]
            elif msg_type in ("display_data", "execute_result"):
                cell_result['display'].append(content["data"])
            elif msg_type == "error":
                cell_result['error'].append(content)
        result = await kc.execute_interactive(cell, output_hook=hook)
        cell_result['status'] = result['content']['status']
        results.append(cell_result)
    output = OutputFile(status='Finished', cells=results)
    try:
        await km.shutdown_kernel()
    except Exception as err:
        print(f'failed to shutdown kernel, ignored: {err}')
    ofn = os.path.abspath(os.path.join(__file__, '../tmp/sandbox/configs/output.json'))
    os.makedirs(os.path.dirname(ofn), exist_ok=True)
    with open(ofn, 'w') as f:
        f.write(output.model_dump_json(indent=2))

asyncio.run(main())
