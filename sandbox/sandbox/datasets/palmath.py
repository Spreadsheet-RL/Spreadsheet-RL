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

import re
from typing import Any, Dict, List

from sandbox.database import get_row_by_id_in_table, get_rows_in_table
from sandbox.datasets.types import (
    CodingDataset,
    EvalResult,
    EvalTestCase,
    GetPromptByIdRequest,
    GetPromptsRequest,
    Prompt,
    RunCodeRequest,
    RunStatus,
    SubmitRequest,
    TestConfig,
)
from sandbox.utils.common import ensure_json
from sandbox.utils.sandbox_client import run_code_in_sandbox


# Note: adapted from https://github.com/deepseek-ai/DeepSeek-Coder/tree/main/Evaluation/PAL-Math
def extract_python_block_with_solution(text):
    """
    !!! This extract code logic comes from deepseek coder.

    Extract the code block from the text that contains the solution function.
    :param text: The text to search for the code block.
    :return: The extracted code block.
    """
    pattern = r'```python\n(.*?)def solution\(\):\n(.*?)```'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1) + 'def solution():\n' + match.group(2)
    else:
        return ""


class PalMathDataset(CodingDataset):
    TEST_CODE = "\"\"\"\nThis logic is largely copied from the Hendrycks' MATH release (math_equivalence), and borrowed from:\n- https://github.com/microsoft/ProphetNet/tree/master/CRITIC\n- https://github.com/openai/prm800k\n\"\"\"\nimport multiprocessing\nfrom math import isclose\nfrom typing import Union\n\nfrom sympy import simplify, N\nfrom sympy.parsing.sympy_parser import parse_expr\nfrom sympy.parsing.latex import parse_latex\n\n\ndef is_digit(s):\n    try:\n        float(str(s).replace(\",\", \"\"))\n        return True\n    except ValueError:\n        return False\n\ndef math_equal(prediction: Union[bool, float, str],\n                reference: Union[float, str],\n                include_percentage: bool = True,\n                is_close: bool = True,\n                timeout: bool = False,\n                ) -> bool:\n    \"\"\"\n    Exact match of math if and only if:\n    1. numerical equal: both can convert to float and are equal\n    2. symbolic equal: both can convert to sympy expression and are equal\n    \"\"\"\n    try: # 1. numerical equal\n        if is_digit(prediction) and is_digit(reference):\n            prediction = float(str(prediction).replace(\",\", \"\"))\n            reference = float(str(reference).replace(\",\", \"\"))\n            # number questions\n            if include_percentage:\n                gt_result = [reference / 100, reference, reference * 100]\n            else:\n                gt_result = [reference]\n            for item in gt_result:\n                try:\n                    if is_close:\n                        if isclose(item, prediction, rel_tol=1e-4):\n                            return True\n                    else:\n                        if item == prediction:\n                            return True\n                except Exception:\n                    continue\n            return False\n    except:\n        pass\n\n    if not prediction and prediction not in [0, False]:\n        return False\n\n    # 2. symbolic equal\n    reference = str(reference).strip()\n    prediction = str(prediction).strip()\n\n    ## deal with [], (), {}\n    pred_str, ref_str = prediction, reference\n    if (prediction.startswith(\"[\") and prediction.endswith(\"]\") and not reference.startswith(\"(\")) or \\\n        (prediction.startswith(\"(\") and prediction.endswith(\")\") and not reference.startswith(\"[\")):\n        pred_str = pred_str.strip(\"[]()\")\n        ref_str = ref_str.strip(\"[]()\")\n    for s in ['{', \"}\", \"(\", \")\"]:\n        ref_str = ref_str.replace(s, \"\")\n        pred_str = pred_str.replace(s, \"\")\n    if pred_str == ref_str:\n        return True\n\n    ## [a, b] vs. [c, d], return a==c and b==d\n    if (prediction.startswith(\"[\") and prediction.endswith(\"]\")) and (reference.startswith(\"[\") and reference.endswith(\"]\")) or \\\n        (prediction.startswith(\"(\") and prediction.endswith(\")\")) and (reference.startswith(\"(\") and reference.endswith(\")\")):\n        pred_parts = prediction[1:-1].split(\",\")\n        ref_parts = reference[1:-1].split(\",\")\n        if len(pred_parts) == len(ref_parts):\n            if all([math_equal(pred_parts[i], ref_parts[i], include_percentage, is_close) for i in range(len(pred_parts))]):\n                return True\n\n    # symbolic equal with sympy\n    if timeout:\n        if call_with_timeout(symbolic_equal_process, prediction, reference):\n            return True\n    else:\n        if symbolic_equal(prediction, reference):\n            return True\n\n    return False\n\n\ndef math_equal_process(param):\n    print(param[-2], param[-1],math_equal(param[-2], param[-1]))\n    return math_equal(param[-2], param[-1])\n\n\ndef symbolic_equal(a, b):\n    def _parse(s):\n        for f in [parse_latex, parse_expr]:\n            try:\n                return f(s)\n            except:\n                pass\n        return s\n    a = _parse(a)\n    b = _parse(b)\n\n    try:\n        if simplify(a-b) == 0:\n            return True\n    except:\n        pass\n\n    try:\n        if isclose(N(a), N(b), rel_tol=1e-3):\n            return True\n    except:\n        pass\n    return False\n\n\ndef symbolic_equal_process(a, b, output_queue):  \n    result = symbolic_equal(a, b)\n    output_queue.put(result)  \n\n\ndef call_with_timeout(func, *args, timeout=1, **kwargs):  \n    output_queue = multiprocessing.Queue()  \n    process_args = args + (output_queue,)  \n    process = multiprocessing.Process(target=func, args=process_args, kwargs=kwargs)  \n    process.start()  \n    process.join(timeout)  \n  \n    if process.is_alive():  \n        process.terminate()  \n        process.join()  \n        return False  \n  \n    return output_queue.get()\n\n\n\"\"\" function for postprocess execution result \"\"\"\n\nimport re\n\n# The functions below copied from `parser.py`.\ndef _fix_sqrt(string):\n    _string = re.sub(r\"\\\\sqrt(\\w+)\", r\"\\\\sqrt{\\1}\", string)\n    return _string\n\ndef _fix_fracs(string):\n    substrs = string.split(\"\\\\frac\")\n    new_str = substrs[0]\n    if len(substrs) > 1:\n        substrs = substrs[1:]\n        for substr in substrs:\n            new_str += \"\\\\frac\"\n            if len(substr) > 0 and substr[0] == \"{\":\n                new_str += substr\n            else:\n                try:\n                    assert len(substr) >= 2\n                except:\n                    return string\n                a = substr[0]\n                b = substr[1]\n                if b != \"{\":\n                    if len(substr) > 2:\n                        post_substr = substr[2:]\n                        new_str += \"{\" + a + \"}{\" + b + \"}\" + post_substr\n                    else:\n                        new_str += \"{\" + a + \"}{\" + b + \"}\"\n                else:\n                    if len(substr) > 2:\n                        post_substr = substr[2:]\n                        new_str += \"{\" + a + \"}\" + b + post_substr\n                    else:\n                        new_str += \"{\" + a + \"}\" + b\n    string = new_str\n    return string\n\ndef _fix_a_slash_b(string):\n    if len(string.split(\"/\")) != 2:\n        return string\n    a = string.split(\"/\")[0]\n    b = string.split(\"/\")[1]\n    try:\n        if \"sqrt\" not in a:\n            a = int(a)\n        if \"sqrt\" not in b:\n            b = int(b)\n        assert string == \"{}/{}\".format(a, b)\n        new_string = \"\\\\frac{\" + str(a) + \"}{\" + str(b) + \"}\"\n        return new_string\n    except:\n        return string\n\ndef strip_string(string):\n    string = str(string).strip()\n    # linebreaks\n    string = string.replace(\"\\n\", \"\")\n\n    # right \".\"\n    string = string.rstrip(\".\")\n\n    # remove inverse spaces\n    string = string.replace(\"\\\\!\", \"\")\n    string = string.replace(\"\\\\ \", \"\")\n\n    # replace \\\\ with \\\n    string = string.replace(\"\\\\\\\\\", \"\\\\\")\n    string = string.replace(\"\\\\\\\\\", \"\\\\\")\n\n    # replace tfrac and dfrac with frac\n    string = string.replace(\"tfrac\", \"frac\")\n    string = string.replace(\"dfrac\", \"frac\")\n\n    # remove \\left and \\right\n    string = string.replace(\"\\\\left\", \"\")\n    string = string.replace(\"\\\\right\", \"\")\n\n    # Remove unit: miles, dollars if after is not none\n    _string = re.sub(r\"\\\\text{.*?}$\", \"\", string).strip()\n    if _string != \"\" and _string != string:\n        # print(\"Warning: unit not removed: '{}' -> '{}'\".format(string, _string))\n        string = _string\n\n    # Remove circ (degrees)\n    string = string.replace(\"^{\\\\circ}\", \"\")\n    string = string.replace(\"^\\\\circ\", \"\")\n\n    # remove dollar signs\n    string = string.replace(\"\\\\$\", \"\")\n    string = string.replace(\"$\", \"\")\n\n    string = string.replace(\"\\\\text\", \"\")\n    string = string.replace(\"x\\\\in\", \"\")\n\n    # remove percentage\n    string = string.replace(\"\\\\%\", \"\")\n    string = string.replace(\"\\%\", \"\")\n    string = string.replace(\"%\", \"\")\n\n    # \" 0.\" equivalent to \" .\" and \"{0.\" equivalent to \"{.\" Alternatively, add \"0\" if \".\" is the start of the string\n    string = string.replace(\" .\", \" 0.\")\n    string = string.replace(\"{.\", \"{0.\")\n\n    # cdot\n    string = string.replace(\"\\\\cdot\", \"\")\n\n    # inf\n    string = string.replace(\"infinity\", \"\\\\infty\")\n    if \"\\\\infty\" not in string:\n        string = string.replace(\"inf\", \"\\\\infty\")\n    string = string.replace(\"+\\\\inity\", \"\\\\infty\")\n\n    # and \n    string = string.replace(\"and\", \"\")\n    string = string.replace(\"\\\\mathbf\", \"\")\n\n    # use regex to remove \\mbox{...}\n    string = re.sub(r\"\\\\mbox{.*?}\", \"\", string)\n\n    # quote\n    string.replace(\"'\", \"\")\n    string.replace(\"\\\"\", \"\")\n    \n    # i, j\n    if \"j\" in string and \"i\" not in string:\n        string = string.replace(\"j\", \"i\")\n\n    # replace a.000b where b is not number or b is end, with ab, use regex\n    string = re.sub(r\"(\\d+)\\.0+([^\\d])\", r\"\\1\\2\", string)\n    string = re.sub(r\"(\\d+)\\.0+$\", r\"\\1\", string)\n\n    # if empty, return empty string\n    if len(string) == 0:\n        return string\n    if string[0] == \".\":\n        string = \"0\" + string\n\n    # to consider: get rid of e.g. \"k = \" or \"q = \" at beginning\n    if len(string.split(\"=\")) == 2:\n        if len(string.split(\"=\")[0]) <= 2:\n            string = string.split(\"=\")[1]\n\n    string = _fix_sqrt(string)\n    string = string.replace(\" \", \"\")\n\n    # \\frac1b or \\frac12 --> \\frac{1}{b} and \\frac{1}{2}, etc. Even works with \\frac1{72} (but not \\frac{72}1). Also does a/b --> \\\\frac{a}{b}\n    string = _fix_fracs(string)\n\n    # NOTE: X/Y changed to \\frac{X}{Y} in dataset, but in simple cases fix in case the model output is X/Y\n    string = _fix_a_slash_b(string)\n\n    return string\n\nimport json\ngt = json.loads(open('answer.json').read())['gt']\n\n#<INSERT>\n\npred = solution()\npred = strip_string(pred)\n\nassert math_equal(pred, gt)\n"

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content'],
        )
        return [cls._generate_single_prompt(r, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'labels', 'content'])
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        return Prompt(id=row['id'], prompt=row['content'], labels=ensure_json(row, 'labels'))

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(request, cls.get_table_name(request.dataset), columns=['test'])
        code = extract_python_block_with_solution(request.completion)
        full_code = cls.TEST_CODE.replace('#<INSERT>', code)
        asset = ensure_json(row, 'test')['asset']
        result = await run_code_in_sandbox(
            RunCodeRequest(
                code=full_code,
                language='python',
                files=asset,
                compile_timeout=request.config.compile_timeout or 20,
                run_timeout=request.config.run_timeout or 20,
            ))
        accepted = result.status == RunStatus.Success

        return EvalResult(id=request.id,
                          accepted=accepted,
                          extracted_code=code,
                          full_code=full_code,
                          tests=[EvalTestCase(
                              passed=accepted,
                              exec_info=result,
                          )])
