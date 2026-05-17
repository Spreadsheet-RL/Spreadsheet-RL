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

from sandbox.datasets.types import EvalResult, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)


async def test_code_contests_get():
    request = GetPromptsRequest(dataset='code_contests', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    print(results)


async def test_code_contests_get_id():
    request = GetPromptByIdRequest(dataset='code_contests', id=4, config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)


async def test_code_contests_list_ids():
    request = GetPromptsRequest(dataset='code_contests', config=TestConfig())
    response = client.post('/list_ids', json=request.model_dump())
    assert response.status_code == 200
    print(response.json())


async def test_code_contests_submit_python():
    request = SubmitRequest(dataset='code_contests',
                            id=17,
                            config=TestConfig(language='python'),
                            completion='''
```python
import sys

def get_smallest_number(n):
    # Convert the number into a list of its digits
    digits = [d for d in str(n)]
    # Sort the digits
    digits.sort()
    # If the smallest digit is '0', find the next smallest non-zero digit
    # and swap them to avoid leading zeroes
    if digits[0] == '0':
        for i in range(1, len(digits)):
            if digits[i] != '0':
                digits[0], digits[i] = digits[i], digits[0]
                break
    # Join the digits back into a string and convert to an integer
    return int(''.join(digits))

def validate_answer(n, m):
    # Get the smallest number from the original number
    smallest = get_smallest_number(n)
    # Check if Bob's answer is equal to the smallest number
    return 'OK' if smallest == m else 'WRONG_ANSWER'

# Read from standard input
input_n = int(sys.stdin.readline().strip())
input_m = int(sys.stdin.readline().strip())

# Validate and print the result
print(validate_answer(input_n, input_m))
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    print(result.model_dump_json(indent=2))
    assert result.accepted == False


async def test_code_contests_submit_cpp_failed():
    request = SubmitRequest(dataset='code_contests',
                            id=17,
                            config=TestConfig(language='cpp'),
                            completion='''
```cpp
#include <iostream>

using namespace std;

int main() {
    cout << -1 << endl;
    return 0;
}
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == False


async def test_code_contests_submit_cpp_no_custom():
    request = SubmitRequest(dataset='code_contests',
                            id=17,
                            config=TestConfig(language='cpp'),
                            completion='''
*/

#include <iostream>
#include <vector>
#include <algorithm>
#include <queue>
#include <cstring>
#include <cstdio>
#include <cmath>
#include <stack>
#include <map>
#include <set>

using namespace std;

int main() {
    int n;
    cin >> n;
    vector<vector<int>> edges(n, vector<int>(n, 0));
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            cin >> edges[i][j];
        }
    }
    vector<int> dist(n, 1e9);
    dist[0] = 0;
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            if (dist[j] > dist[j] + edges[j][i]) {
                dist[j] = dist[j] + edges[j][i];
            }
        }
    }
    for (int i = 0; i < n; i++) {
        cout << dist[i] << " ";
    }
    cout << endl;
    return 0;
}

>>> print("*/\n\n#include <stdio.h>\n\nint main()\n{\n    int num;\n    scanf(\"%d\", &num);\n    printf(\"%d\", num);\n    return 0;\n}\n")
*/

#include <stdio.h>

int main()
{
    int num;
    scanf("%d", &num);
    printf("%d", num);
    return 0;
}
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.extracted_code == ''


async def test_code_contests_submit_cpp_custom():
    request = SubmitRequest(dataset='code_contests',
                            id=17,
                            config=TestConfig(language='cpp',
                                              custom_extract_logic="""
include_header = "#include<bits/stdc++.h>\\n#include<stdlib.h>\\n"
try:
    completion = completion.lstrip()
    pattern = r'''(?:\\*/)?(.*)'''
    matches = re.findall(pattern, completion, re.DOTALL)
    result = include_header + matches[0]
except:
    result = include_header + completion
submit_code_blocks([CodeBlock(priority=40, code=result, language='cpp')])
                            """),
                            completion='''
*/

#include <iostream>
#include <vector>
#include <algorithm>
#include <queue>
#include <cstring>
#include <cstdio>
#include <cmath>
#include <stack>
#include <map>
#include <set>

using namespace std;

int main() {
    int n;
    cin >> n;
    vector<vector<int>> edges(n, vector<int>(n, 0));
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            cin >> edges[i][j];
        }
    }
    vector<int> dist(n, 1e9);
    dist[0] = 0;
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            if (dist[j] > dist[j] + edges[j][i]) {
                dist[j] = dist[j] + edges[j][i];
            }
        }
    }
    for (int i = 0; i < n; i++) {
        cout << dist[i] << " ";
    }
    cout << endl;
    return 0;
}

>>> print("*/\n\n#include <stdio.h>\n\nint main()\n{\n    int num;\n    scanf(\"%d\", &num);\n    printf(\"%d\", num);\n    return 0;\n}\n")
*/

#include <stdio.h>

int main()
{
    int num;
    scanf("%d", &num);
    printf("%d", num);
    return 0;
}
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert 'include <stdio' in result.extracted_code
