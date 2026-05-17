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

import pytest
from collections import Counter
from fastapi.testclient import TestClient

from sandbox.datasets.types import EvalResult, TestConfig
from sandbox.server.online_judge_api import SubmitRequest
from sandbox.server.server import app
from sandbox.tests.datasets.test_utils import JEST_CODE

client = TestClient(app)


@pytest.mark.parametrize("dt", ['AutoEvalV4Dataset', 'AutoEvalDataset'])
async def test_autoeval_jest_cases(dt):
    request = SubmitRequest(dataset='',
                            id=0,
                            config=TestConfig(dataset_type=dt,
                                              provided_data={
                                                  'id': 0,
                                                  'content': '',
                                                  'labels': {
                                                      'programming_language': 'javascript',
                                                      'execution_language': 'jest'
                                                  },
                                                  'test': {
                                                      'code': JEST_CODE,
                                                  }
                                              }),
                            completion=f'''
```javascript
let a = 1;
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == False
    assert 'jest_cases' in result.extra
    ctr = Counter([case['passed'] for case in result.extra['jest_cases']])
    assert ctr[True] == 9
    assert ctr[False] == 2


async def test_autoeval_extraction():
    request = SubmitRequest(dataset='',
                            id=0,
                            config=TestConfig(dataset_type='AutoEvalDataset',
                                              provided_data={
                                                  'id': 0,
                                                  'content': '',
                                                  'labels': {
                                                      'programming_language': 'python',
                                                      'execution_language': 'python'
                                                  },
                                                  'test': {
                                                      'code': '',
                                                  }
                                              }),
                            completion=f'''
```bash
blah
```

```python
let a = 1;
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == False
    print(result.extracted_code)
    assert 'let a = 1;' in result.extracted_code


async def test_autoeval_extraction_first():
    request = SubmitRequest(dataset='',
                            id=0,
                            config=TestConfig(dataset_type='AutoEvalDataset',
                                              provided_data={
                                                  'id': 0,
                                                  'content': '',
                                                  'labels': {
                                                      'programming_language': 'python',
                                                      'execution_language': 'python'
                                                  },
                                                  'test': {
                                                      'code': '',
                                                  }
                                              }),
                            completion=f'''
```python
let a = 1;
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == False
    print(result.extracted_code)
    assert 'let a = 1;' in result.extracted_code
