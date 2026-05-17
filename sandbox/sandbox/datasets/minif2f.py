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

import structlog

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
from sandbox.utils.extraction import default_extract_helper
from sandbox.utils.sandbox_client import run_code_in_sandbox

logger = structlog.stdlib.get_logger()

HEADERS = '''
import Mathlib.Algebra.Algebra.Basic
import Mathlib.Algebra.Order.Floor
import Mathlib.Algebra.Associated
import Mathlib.Algebra.BigOperators.Pi
import Mathlib.Algebra.GeomSum
import Mathlib.Algebra.Group.Pi.Basic
import Mathlib.Algebra.Group.Commute.Basic
import Mathlib.Algebra.Order.Floor
import Mathlib.Algebra.QuadraticDiscriminant
import Mathlib.Algebra.Ring.Basic
import Mathlib.Analysis.Asymptotics.AsymptoticEquivalent
import Mathlib.Analysis.NormedSpace.Basic
import Mathlib.Analysis.SpecialFunctions.Log.Basic
import Mathlib.Analysis.SpecialFunctions.Log.Base
import Mathlib.Combinatorics.SimpleGraph.Basic
import Mathlib.Data.Complex.Basic
import Mathlib.Data.Complex.Exponential
import Mathlib.Data.Finset.Basic
import Mathlib.Data.Fintype.Card
import Mathlib.Data.Int.GCD
import Mathlib.Data.Int.ModEq
import Mathlib.Data.List.Intervals
import Mathlib.Data.List.Palindrome
import Mathlib.Data.Multiset.Basic
import Mathlib.Data.Nat.Choose.Basic
import Mathlib.Data.Nat.Digits
import Mathlib.Data.Nat.Factorial.Basic
import Mathlib.Data.Nat.ModEq
import Mathlib.Data.Nat.Multiplicity
import Mathlib.Data.PNat.Basic
import Mathlib.Data.PNat.Prime
import Mathlib.Data.Rat.Lemmas
import Mathlib.Data.Real.Basic
import Mathlib.Data.Real.Irrational
import Mathlib.Data.Real.Sqrt
import Mathlib.Data.Set.Finite
import Mathlib.Data.Sym.Sym2
import Mathlib.Data.ZMod.Basic
import Mathlib.Dynamics.FixedPoints.Basic
import Mathlib.LinearAlgebra.AffineSpace.AffineMap
import Mathlib.LinearAlgebra.AffineSpace.Independent
import Mathlib.LinearAlgebra.AffineSpace.Ordered
import Mathlib.LinearAlgebra.FiniteDimensional
import Mathlib.Logic.Equiv.Basic
import Mathlib.Order.Filter.Basic
import Mathlib.Order.WellFounded
import Mathlib.Topology.Basic
import Mathlib.Data.Complex.Basic
import Mathlib.Data.Nat.Log
import Mathlib.Data.Complex.Exponential
import Mathlib.NumberTheory.Divisors
import Mathlib.Data.ZMod.Defs
import Mathlib.Tactic
import Mathlib.Util.Delaborators
import Mathlib.Data.Real.Irrational
'''


def move_imports_and_opens_to_top(lean_code: str) -> str:
    lines = lean_code.split('\n')
    import_lines = []
    open_lines = []
    other_lines = []

    for line in lines:
        stripped_line = line.strip()
        if stripped_line.startswith('import '):
            if re.search(r'\bimport\s+Mathlib\b(?!\.)', stripped_line):
                logger.warning(f'whole import of Mathlib removed: {stripped_line}')
            else:
                import_lines.append(line)
        elif stripped_line.startswith('open '):
            open_lines.append(line)
        else:
            other_lines.append(line)

    # Combine import lines, open lines, and other lines with a newline in between
    result = import_lines + open_lines + [''] + other_lines
    return '\n'.join(result).strip()


def remove_imports(lean_code: str) -> str:
    lines = lean_code.split('\n')
    other_lines = []

    for line in lines:
        stripped_line = line.strip()
        if not stripped_line.startswith('import '):
            other_lines.append(line)

    return '\n'.join(other_lines).strip()


class MiniF2FLean4Dataset(CodingDataset):

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        columns = cls._get_dataset_columns(request.config)
        rows = await get_rows_in_table(request, cls.get_table_name(request.dataset), columns=columns)
        return [cls._generate_single_prompt(r, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        columns = cls._get_dataset_columns(request.config)
        row = await get_row_by_id_in_table(request, cls.get_table_name(request.dataset), columns=columns)
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _get_dataset_columns(cls, config: TestConfig):
        if 'prompt_template' in config.extra:
            prompt_template = config.extra['prompt_template']
            fields = re.findall(r'{(\w+)}', prompt_template)
            columns = ['id', 'labels'] + fields
        else:
            columns = ['id', 'labels', 'content']
        return columns

    @classmethod
    def _format_prompt_template(cls, row: Dict[str, Any], config: TestConfig):
        prompt_template = config.extra['prompt_template']
        fields = re.findall(r'{(\w+)}', prompt_template)
        mapping = {field: row[field] for field in fields}
        prompt = prompt_template.format_map(mapping)
        return prompt

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        if 'prompt_template' in config.extra:
            prompt = cls._format_prompt_template(row, config)
        else:
            prefix = '请使用 Lean4 完成下面的证明：'
            locale = config.locale or 'zh-CN'
            if locale == 'en':
                prefix = 'Please finish the following theorem in Lean4:'
            prompt = f"{prefix}\n\n```lean\n{row['content']}\n```"

        return Prompt(id=row['id'], prompt=prompt, labels=ensure_json(row, 'labels'))

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        columns = cls._get_dataset_columns(request.config)
        row = await get_row_by_id_in_table(request, cls.get_table_name(request.dataset), columns=columns)
        ensure_json(row, 'labels')
        completion = request.completion

        # if question_id is in the completion, we assume the response contains a complete lean code.
        # if not, but the response is a code block(startswith or endswith ```),
        # we build a complete lean code by concatenating prompt and response.
        if f'theorem {row["id"]}' in completion:
            code = default_extract_helper(completion, 'lean', request.config.custom_extract_logic)
        elif completion.strip().startswith('```') or completion.strip().endswith('```'):
            if 'prompt_template' in request.config.extra:
                prompt = cls._format_prompt_template(row, request.config)
            else:
                prompt = f"```lean\n{row['content']}\n"
            prompt = prompt.replace('sorry', '')
            response = completion
            for prefix in ['```lean4', '```lean', '```']:
                if completion.strip().startswith(prefix):
                    pos = completion.find(prefix)
                    response = completion[pos + len(prefix):]
                    break

            code = default_extract_helper(prompt + response, 'lean', request.config.custom_extract_logic)
        else:
            code = default_extract_helper(completion, 'lean', request.config.custom_extract_logic)

        full_code = f'{HEADERS}\n\n{remove_imports(code)}'
        full_code = move_imports_and_opens_to_top(full_code)

        result = await run_code_in_sandbox(
            RunCodeRequest(
                code=full_code,
                language='lean',
                # leave some time for lean to build new dependencies
                run_timeout=request.config.run_timeout or 300,
            ))
        accepted = result.status == RunStatus.Success
        if accepted:
            # make sure the theorem is still inside the code
            if f'theorem {row["id"]}' not in full_code:
                accepted = False
            # We do not allow sorry in the model generated proof.
            if result.run_result.return_code == 0 and "declaration uses 'sorry'" in result.run_result.stdout:
                accepted = False

        return EvalResult(id=request.id,
                          accepted=accepted,
                          extracted_code=code,
                          full_code=full_code,
                          tests=[EvalTestCase(
                              passed=accepted,
                              exec_info=result,
                          )])
