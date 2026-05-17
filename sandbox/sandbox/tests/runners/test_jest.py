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

from sandbox.runners import CommandRunStatus
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.server.server import app

client = TestClient(app)


def test_jest_pass():
    request = RunCodeRequest(language='jest',
                             code='''
import { describe, expect, test } from '@jest/globals';

function MinSubArrayLen(target: number, nums: number[]): number {
    let n = nums.length;
    let minLength = Infinity; // 初始化为极大值
    let sum = 0; // 存储子数组的和
    let start = 0; // 子数组的起始索引

    for (let end = 0; end < n; end++) {
        sum += nums[end]; // 将当前元素加到和中

        while (sum >= target) {
            minLength = Math.min(minLength, end - start + 1); // 更新最小长度
            sum -= nums[start]; // 移动起始索引，并减去这个值
            start++;
        }
    }

    return minLength === Infinity ? 0 : minLength; // 如果长度未被更新过，则返回0，表示没有找到
}

describe('MinSubArrayLen', () => {
    test('finds minimum length of a subarray with sum >= target', () => {
        expect(MinSubArrayLen(7, [2, 3, 1, 2, 4, 3])).toBe(2);
        expect(MinSubArrayLen(15, [1, 2, 3, 4, 5])).toBe(5);
        expect(MinSubArrayLen(4, [1, 4, 4])).toBe(1);
        expect(MinSubArrayLen(11, [1, 1, 1, 1, 1, 1, 1, 1])).toBe(0);
    });

    test('returns 0 if no such subarray exists', () => {
        expect(MinSubArrayLen(100, [1, 2, 3, 4, 5])).toBe(0);
        expect(MinSubArrayLen(7, [2, 1])).toBe(0);
    });

    test('works with single element array', () => {
        expect(MinSubArrayLen(3, [3])).toBe(1);
        expect(MinSubArrayLen(3, [1])).toBe(0);
    });
});

                             ''',
                             run_timeout=10)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished


def test_jest_fail():
    request = RunCodeRequest(language='jest',
                             code='''
import { describe, expect, test } from '@jest/globals';

function MinSubArrayLen(target: number, nums: number[]): number {
    return 0
}

describe('MinSubArrayLen', () => {
    test('finds minimum length of a subarray with sum >= target', () => {
        expect(MinSubArrayLen(7, [2, 3, 1, 2, 4, 3])).toBe(2);
        expect(MinSubArrayLen(15, [1, 2, 3, 4, 5])).toBe(5);
        expect(MinSubArrayLen(4, [1, 4, 4])).toBe(1);
        expect(MinSubArrayLen(11, [1, 1, 1, 1, 1, 1, 1, 1])).toBe(0);
    });

    test('returns 0 if no such subarray exists', () => {
        expect(MinSubArrayLen(100, [1, 2, 3, 4, 5])).toBe(0);
        expect(MinSubArrayLen(7, [2, 1])).toBe(0);
    });

    test('works with single element array', () => {
        expect(MinSubArrayLen(3, [3])).toBe(1);
        expect(MinSubArrayLen(3, [1])).toBe(0);
    });
});

                             ''',
                             run_timeout=10)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
