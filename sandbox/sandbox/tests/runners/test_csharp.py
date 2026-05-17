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


def test_csharp_print():
    request = RunCodeRequest(language='csharp',
                             code='''
    using System;

    public class Program
    {
        public static void Main(string[] args)
        {
            Console.WriteLine("123");
        }
    }
    ''',
                             run_timeout=20)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.stdout.strip() == '123'


def test_csharp_timeout():
    request = RunCodeRequest(language='csharp',
                             code='''
    using System;
    using System.Threading;

    public class Program
    {
        public static void Main(string[] args)
        {
            Thread.Sleep(200); // Sleep for 200 milliseconds
        }
    }
    ''',
                             run_timeout=0.1,
                             compile_timeout=1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


def test_csharp_assertion_error():
    request = RunCodeRequest(language='csharp',
                             code='''
    using System;
    using System.Diagnostics;

    public class Program
    {
        public static void Main(string[] args)
        {
            Debug.Assert(1 == 2, "Assertion failed");
        }
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    # Note: Handling of assertions may vary depending on how the sandbox captures output and errors
    assert "Assertion failed" in result.run_result.stderr


def test_csharp_stdin():
    request = RunCodeRequest(language='csharp',
                             code='''
    public class Program
    {
        public static void Main(string[] args)
        {
            Console.WriteLine(Convert.ToInt32(Console.ReadLine()));
        }
    }
                             ''',
                             stdin='65535')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout == '65535\n'
