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

from fastapi.testclient import TestClient

from sandbox.server.sandbox_api import RunJupyterRequest, RunJupyterResponse
from sandbox.server.server import app

client = TestClient(app)


def test_jupyter_print():
    request = RunJupyterRequest(cells=['print(123)'])
    response = client.post('/run_jupyter', json=request.model_dump())
    assert response.status_code == 200
    result = RunJupyterResponse(**response.json())
    print(result.model_dump_json(indent=2))
    assert result.cells[0].stdout == '123\n'


def test_jupyter_multi_cell():
    request = RunJupyterRequest(cells=['a = "hello"', 'a', 'print(a)'])
    response = client.post('/run_jupyter', json=request.model_dump())
    assert response.status_code == 200
    result = RunJupyterResponse(**response.json())
    print(result.model_dump_json(indent=2))
    assert result.cells[0].stdout == ''
    assert result.cells[1].display[0]['text/plain'] == "'hello'"
    assert result.cells[2].stdout == 'hello\n'


def test_jupyter_multi_cell_raise():
    request = RunJupyterRequest(cells=[
        'a = "hello"', 'import sys\nsys.stderr.write("This is an error message\\n")', 'assert False', 'print(a)'
    ])
    response = client.post('/run_jupyter', json=request.model_dump())
    assert response.status_code == 200
    result = RunJupyterResponse(**response.json())
    print(result.model_dump_json(indent=2))
    assert result.cells[0].stdout == ''
    assert result.cells[1].stderr == 'This is an error message\n'
    assert result.cells[2].error[0]['ename'] == 'AssertionError'
    assert result.cells[3].stdout == 'hello\n'


def test_jupyter_plot():
    request = RunJupyterRequest(cells=[
        'import matplotlib.pyplot as plt', '''
x = [1, 2, 3, 4, 5]
y = [2, 3, 5, 7, 11]

plt.plot(x, y)

plt.title('Simple Plot')
plt.xlabel('X-axis')
plt.ylabel('Y-axis')

plt.show()
    '''
    ])
    response = client.post('/run_jupyter', json=request.model_dump())
    assert response.status_code == 200
    result = RunJupyterResponse(**response.json())
    assert 'image/png' in result.cells[1].display[0]
