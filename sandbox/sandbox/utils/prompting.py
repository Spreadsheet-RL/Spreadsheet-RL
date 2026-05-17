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

HANDCRAFT_FEWSHOTS_1_PROMPT = """
写一个Python函数以整数为输入，执行下面的操作后返回结果
1. 计算整数的阶乘，得到一个字符串
2. 再把字符串中的大写转成小写，小写转成大写
3. 把字符串中奇数位和偶数位对调
4. 计算字符串中所有字符的ASCII码之和，得到一个整数
5. 最后计算整数所有位的数字之和, 返回结果
"""

HANDCRAFT_FEWSHOTS_1_RESPONSE = """
```python
def factorial(n):
    if n == 0 or n == 1:
        return 1
    else:
        return n * factorial(n - 1)

def swap_case(s):
    return s.swapcase()

def swap_odd_even(s):
    chars = list(s)
    for i in range(0, len(chars) - 1, 2):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return ''.join(chars)

def sum_of_ascii(s):
    return sum(ord(c) for c in s)

def sum_of_digits(n):
    return sum(map(int, str(n)))

def result(n):
    fact = factorial(n)
    fact_str = f"{fact}"
    swapped_case_str = swap_case(fact_str)
    swapped_odd_even_str = swap_odd_even(swapped_case_str)
    ascii_sum = sum_of_ascii(swapped_odd_even_str)
    return sum_of_digits(ascii_sum)
```
"""

HANDCRAFT_FEWSHOTS = [{
    "prompt": HANDCRAFT_FEWSHOTS_1_PROMPT,
    "response": HANDCRAFT_FEWSHOTS_1_RESPONSE,
}]


def handcraft_fewshot_prompt(prompt: str) -> str:
    """ Handcraft fewshot prompt for code autoeval task.
    """
    shots_delimiter = '---\n'
    shots = ['问题:\n{}\n答案:\n{}\n'.format(s['prompt'], s['response']) for s in HANDCRAFT_FEWSHOTS]
    shots.append('问题:\n{}\n答案:\n'.format(prompt))
    return shots_delimiter.join(shots)
