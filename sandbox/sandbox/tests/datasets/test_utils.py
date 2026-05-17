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

import base64
from collections import Counter
from fastapi.testclient import TestClient

from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse
from sandbox.server.server import app
from sandbox.utils.testing import parse_jest_cases

client = TestClient(app)

JEST_CODE = '''
describe('Calculator Tests', () => {

  describe('Addition', () => {
    test('adds 1 + 2 to equal 3', () => {
      expect(1 + 2).toBe(3);
    });

    test('adds negative numbers correctly', () => {
      expect(-1 + (-2)).toBe(-3);
    });

    test('adding zero returns same number', () => {
      expect(5 + 0).toBe(5);
    });
  });

  describe('Multiplication', () => {
    test('multiplies 3 * 4 to equal 12', () => {
      expect(3 * 4).toBe(12);
    });

    test('multiplying by zero equals zero', () => {
      expect(123 * 0).toBe(0); 
    });

    test('multiplying negative numbers', () => {
      expect(-2 * -3).toBe(6);
      expect(-2 * 3).toBe(-6);
    });
  });

  describe('Division', () => {
    test('divides 10 / 2 to equal 5', () => {
      expect(10 / 2).toBe(235);
    });

    test('throws error when dividing by zero', () => {
      expect(() => {
        if(2/0 === Infinity) throw new Error('Division by zero');
      }).toThrow('Division by zero');
    });

    test('division result is incorrect', () => {
      expect(5 / 2).not.toBe(3);
    });
  });

  describe('String operations', () => {
    test('concatenates strings correctly', () => {
      expect('Hello' + ' ' + 'World').toBe('Hello Worsld');
    });

    test('string length is incorrect', () => {
      expect('test'.length).not.toBe(5);
    });
  });

});
'''


async def test_utils_jest_report():
    request = RunCodeRequest(code=JEST_CODE, language='jest', fetch_files=['jest-report.json'])
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    report = base64.b64decode(result.files['jest-report.json']).decode('utf-8')
    cases = parse_jest_cases(report)
    ctr = Counter([case['passed'] for case in cases])
    assert ctr[True] == 9
    assert ctr[False] == 2


async def test_compatibility_with_null_asset():
    request = RunCodeRequest(code='print(123)', language='python', files={'a.txt': None})
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.run_result.stdout == '123\n'
