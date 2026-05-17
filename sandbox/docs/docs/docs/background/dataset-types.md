---
sidebar_position: 1
---

# Types of Datasets

To maximize code reuse, many dataset implementations in the sandbox service use the same Python class. For example:

- **HumanEvalDataset**
  - humaneval_python
  - humaneval_cpp
  - humaneval_typescript
  - shadow_humaneval_python
  - bigcodebench
  - ...
- **CommonOJDataset**
  - code_contests
  - ...

Here, `humaneval_python` is a specific **dataset**, while `HumanEvalDataset` is its **dataset type**.
The dataset type determines the data format of its corresponding datasets, as well as the logic for prompt generation, code extraction, and problem evaluation.

Below are some commonly used dataset types.

## AutoEvalDataset

This type prompts the model to output a specified function or partial code. It performs testing by appending test assertions after the model's output code. It supports few-shot learning, custom extraction, and execution languages, as well as file dependencies.

Data format:

- id: Unique identifier within the dataset, string or int type
- content: prompt
- test: json
  - code: Test code, appended to the model's output code for testing
  - asset: json, optional. A dictionary mapping file names to their base64-encoded content
- labels: json
  - fewshot: Custom few-shot examples
  - programming_language: The type of code the model should output
  - execution_language: The language used to execute the final test code

## CommonOJDataset

This type prompts the model to output complete, executable code. It tests by checking whether the stdout for a given stdin matches the expected output. It supports any programming language.

This format is used for most competitive programming problems.

Data format:

- id: Unique identifier within the dataset, string or int type
- content: Problem content. The prompt given to the model will include programming language requirements based on this
- test: json list, where each list element has the format:
  - input: json
    - stdin: Standard input
  - output: json
    - stdout: Standard output
- labels: json
