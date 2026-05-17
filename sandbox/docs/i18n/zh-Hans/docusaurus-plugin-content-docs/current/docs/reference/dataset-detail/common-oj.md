# CommonOJ

The CommonOJ dataset aims to unify the evaluation of competitive programming problems. These problems comes with multiple formats:

- **Standard** (implemented): LLM outputs complete executable code and sandbox determines whether the problem is solved by checking if the standard output matches the expected output under given standard inputs.
- **Testlib** (under progress): When a solution requires advanced methods to test, e.g. has multiple correct outputs given an input, or a round-off error is permitted. Testlib programs can be used to check the correctness.
- **Code Interface** (under discussion): Problems in LeetCode and TopCoder do not follows the format of stdio, but only requires the solution class. The support of these problems is still under discussion.

## Data Format

The problem format accepted by the CommonOJ dataset is as follows:

```json
{
    "id": str,                          # Unique identifier
    "content": str,                     # Problem statement
    "labels": {
        "problem_format": str,          # Optional, "Standard" by default
    },
    "test": [                           # Test data format for "Standard" problems
        {
            "input": {                  # Input data, filename -> content, stdin is the standard stream
                "stdin": "xxx"
            },
            "output": {                 # Expected output, filename -> content 
                "stdout": "xxx"
            }
        },
        ...
    ],
    "canonical_solution": {             # Used to store the correct answer
        "cpp": ["#include..."],
        "python": ["import...", "import...", ...]
    }
}
```
