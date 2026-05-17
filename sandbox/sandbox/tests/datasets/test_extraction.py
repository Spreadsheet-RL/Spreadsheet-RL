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

from sandbox.utils.extraction import extract_code_from_freeform_completion, extract_code_from_freeform_completion_v2, default_extract_helper

completion1 = """
Some words before code...
```python
def main():
    pass
```
Some words after code...
"""

completion2 = """
Some words before code...
```python
def main():
    pass
Some words after code...
"""

completion3 = """
Some words here...
```bash
pip install xxx
```

Some words here...
```python
def main():
    pass
```
Some words here...
"""

completion4 = """
Some words here...
```bash
pip install xxx
```

Some words here...
```python
def main():
    pass

assert a == 1
```
Some words here...
"""

result1 = """
def main():
    pass
"""

result2 = """
def main():
    pass
Some words after code...
"""

result3 = """
def main():
    pass
"""

result4 = """
def main():
    pass
"""
result4_ = """
def main():
    pass

assert a == 1
"""


def test_extract_code_from_freeform_completion():
    r1, t1 = extract_code_from_freeform_completion(completion1)
    assert r1.strip() == result1.strip() and t1 == 'fenced'
    r2, t2 = extract_code_from_freeform_completion(completion2)
    assert r2.strip() == result2.strip() and t2 == 'incomplete_fenced'
    r3, _ = extract_code_from_freeform_completion(completion3, 'python', True, exactly_match=True)
    assert r3.strip() == result3.strip()
    r4, _ = extract_code_from_freeform_completion(completion4, 'python', True, exactly_match=True, remove_asserts=True)
    assert r4.strip() == result4.strip()
    r4_, _ = extract_code_from_freeform_completion(completion4, 'python', True, exactly_match=True)
    assert r4_.strip() == result4_.strip()


v2_completion1 = """
```scala
def strlen(string : String) : Long = {
    sys.exit(0)
}
```
"""

v2_result1 = """
def strlen(string : String) : Long = {
    sys.exit(0)
}
"""

v2_completion2 = """
```scala
object Main extends App {
    /**
     * You are an expert Scala programmer, and here is your task.
     * * Write a Scala function to find minimum sum of factors of a given number.
     *
     * >>> findMinSum(12)
     * 7
     * >>> findMinSum(105)
     * 15
     * >>> findMinSum(2)
     * 2
     */
    def findMinSum(num : Int) : Int = {
        var sum = num + 1
        for (i <- 2 to Math.sqrt(num).toInt) {
            if (num % i == 0) {
                val factorSum = i + num/i
                if (factorSum < sum) {
                    sum = factorSum
                }
            }
        }
        sum
    }
}
```
"""

v2_result2 = """
    /**
     * You are an expert Scala programmer, and here is your task.
     * * Write a Scala function to find minimum sum of factors of a given number.
     *
     * >>> findMinSum(12)
     * 7
     * >>> findMinSum(105)
     * 15
     * >>> findMinSum(2)
     * 2
     */
    def findMinSum(num : Int) : Int = {
        var sum = num + 1
        for (i <- 2 to Math.sqrt(num).toInt) {
            if (num % i == 0) {
                val factorSum = i + num/i
                if (factorSum < sum) {
                    sum = factorSum
                }
            }
        }
        sum
    }
"""

v2_completion3 = """
```go
package main
func Foo() int {
}
```
"""

v2_completion4 = """
```java
import java.util.HashMap;

public class Solution {
    public int AtMostKDistinct(int[] nums, int k) {
        return atMostKDistinct(nums, k) - atMostKDistinct(nums, k - 1);
    }
}
```
"""

v2_completion5 = """
```javascript
function isAnagram(str1, str2) {
    if (str1.length !== str2.length) {
      return false;
    }

    const charCount = {};

    for (let char of str1) {
      charCount[char] = (charCount[char] || 0) + 1;
    }

    for (let char of str2) {
      if (!charCount[char]) {
        return false;
      }
      charCount[char]--;
    }

    return true;
}
```
"""

v2_completion6 = """
```python
if __name__ == "__main__":
    pass
```
"""

v2_completion7 = """
To solve this problem, we can use the Shapely library in Python, which provides geometric objects and operations. Specifically, we can use the `minimum_rotated_rectangle` method to find the smallest bounding rotated rectangle for a given polygon. Here's how you can implement the `solve` function:

1. Install the Shapely library if you haven't already:
   ```bash
   pip install shapely
   ```

2. Implement the `solve` function:

```python
from shapely.geometry import Polygon, MultiPolygon

def solve(polys):
    # Convert the list of polygons into Shapely Polygon objects
    shapely_polys = [Polygon(poly) for poly in polys]
    
    # Combine all polygons into a single MultiPolygon
    multi_poly = MultiPolygon(shapely_polys)
    
    # Get the minimum rotated rectangle that covers the MultiPolygon
    min_rotated_rect = multi_poly.minimum_rotated_rectangle
    
    # Calculate and return the area of the minimum rotated rectangle
    return min_rotated_rect.area

# Example usage:
polys = [
    [(0, 0), (1, 0), (1, 1), (0, 1)],
    [(2, 2), (3, 2), (3, 3), (2, 3)]
]
print(solve(polys))  # Output will be the area of the smallest bounding rotated rectangle
```

### Explanation:
1. **Convert Input to Shapely Polygons**: The input list of polygons is converted into Shapely `Polygon` objects.
2. **Combine Polygons**: All the individual polygons are combined into a single `MultiPolygon` object.
3. **Minimum Rotated Rectangle**: The `minimum_rotated_rectangle` method is used to find the smallest bounding rotated rectangle that can cover the `MultiPolygon`.
4. **Calculate Area**: The area of this minimum rotated rectangle is calculated and returned.

This function will give you the area of the smallest bounding rotated rectangle that can cover all the input polygons.
"""

v2_result7 = """
from shapely.geometry import Polygon, MultiPolygon

def solve(polys):
    # Convert the list of polygons into Shapely Polygon objects
    shapely_polys = [Polygon(poly) for poly in polys]
    
    # Combine all polygons into a single MultiPolygon
    multi_poly = MultiPolygon(shapely_polys)
    
    # Get the minimum rotated rectangle that covers the MultiPolygon
    min_rotated_rect = multi_poly.minimum_rotated_rectangle
    
    # Calculate and return the area of the minimum rotated rectangle
    return min_rotated_rect.area
"""

v2_completion7_ = """
To solve this problem, we can use the Shapely library in Python, which provides geometric objects and operations. Specifically, we can use the `minimum_rotated_rectangle` method to find the smallest bounding rotated rectangle for a given polygon. Here's how you can implement the `solve` function:

1. Install the Shapely library if you haven't already:
   ```bash
   pip install shapely
   ```

2. Implement the `solve` function:

```python
from shapely.geometry import Polygon, MultiPolygon

def solve(polys):
    # Convert the list of polygons into Shapely Polygon objects
    shapely_polys = [Polygon(poly) for poly in polys]
    
    # Combine all polygons into a single MultiPolygon
    multi_poly = MultiPolygon(shapely_polys)
    
    # Get the minimum rotated rectangle that covers the MultiPolygon
    min_rotated_rect = multi_poly.minimum_rotated_rectangle
    
    # Calculate and return the area of the minimum rotated rectangle
    return min_rotated_rect.area

assert a == 1

# Example usage:
polys = [
    [(0, 0), (1, 0), (1, 1), (0, 1)],
    [(2, 2), (3, 2), (3, 3), (2, 3)]
]
print(solve(polys))  # Output will be the area of the smallest bounding rotated rectangle
```

### Explanation:
1. **Convert Input to Shapely Polygons**: The input list of polygons is converted into Shapely `Polygon` objects.
2. **Combine Polygons**: All the individual polygons are combined into a single `MultiPolygon` object.
3. **Minimum Rotated Rectangle**: The `minimum_rotated_rectangle` method is used to find the smallest bounding rotated rectangle that can cover the `MultiPolygon`.
4. **Calculate Area**: The area of this minimum rotated rectangle is calculated and returned.

This function will give you the area of the smallest bounding rotated rectangle that can cover all the input polygons.
"""

v2_result7_ = """
from shapely.geometry import Polygon, MultiPolygon

def solve(polys):
    # Convert the list of polygons into Shapely Polygon objects
    shapely_polys = [Polygon(poly) for poly in polys]
    
    # Combine all polygons into a single MultiPolygon
    multi_poly = MultiPolygon(shapely_polys)
    
    # Get the minimum rotated rectangle that covers the MultiPolygon
    min_rotated_rect = multi_poly.minimum_rotated_rectangle
    
    # Calculate and return the area of the minimum rotated rectangle
    return min_rotated_rect.area

assert a == 1
"""

v2_completion8 = """
```csharp
using System;
using System.Numerics;
using System.Diagnostics;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Security.Cryptography;

class Problem {
    // Check if in given list of numbers, are any two numbers closer to each other than
    // given threshold.
    // >>> HasCloseElements((new List<float>(new float[]{(float)1.0f, (float)2.0f, (float)3.0f})), (0.5f))
    // (false)
    // >>> HasCloseElements((new List<float>(new float[]{(float)1.0f, (float)2.8f, (float)3.0f, (float)4.0f, (float)5.0f, (float)2.0f})), (0.3f))
    // (true)
    public static bool HasCloseElements(List<float> numbers, float threshold) {
        for (int i = 0; i < numbers.Count; i++) {
            for (int j = i + 1; j < numbers.Count; j++) {
                if (Math.Abs(numbers[i] - numbers[j]) < threshold) {
                    return true;
                }
            }
        }
        return false;
    }
}
```
"""

v2_completion9 = """
```java
import java.util.*;
import java.lang.reflect.*;
import org.javatuples.*;
import java.security.*;
import java.math.*;
import java.io.*;
import java.util.stream.*;

class Problem {
    // Check if in given array list of numbers, are any two numbers closer to each other than
    // given threshold.
    // >>> hasCloseElements((new ArrayList<Float>(Arrays.asList((float)1.0f, (float)2.0f, (float)3.0f))), (0.5f))
    // (false)
    // >>> hasCloseElements((new ArrayList<Float>(Arrays.asList((float)1.0f, (float)2.8f, (float)3.0f, (float)4.0f, (float)5.0f, (float)2.0f))), (0.3f))
    // (true)
    public static boolean hasCloseElements(ArrayList<Float> numbers, float threshold) {
        for (int i = 0; i < numbers.size(); i++) {
            for (int j = i + 1; j < numbers.size(); j++) {
                if (Math.abs(numbers.get(i) - numbers.get(j)) <= threshold) {
                    return true;
                }
            }
        }
        return false;
    }

    public static void main(String[] args) {
        ArrayList<Float> numbers1 = new ArrayList<>(Arrays.asList(1.0f, 2.0f, 3.0f));
        ArrayList<Float> numbers2 = new ArrayList<>(Arrays.asList(1.0f, 2.8f, 3.0f, 4.0f, 5.0f, 2.0f));
        
        System.out.println(hasCloseElements(numbers1, 0.5f)); // false
        System.out.println(hasCloseElements(numbers2, 0.3f)); // true
    }
}
```
"""

v2_completion10 = '''
```racket
#lang racket

(define (has_close_elements numbers threshold)
  """
  Check if in the given list of numbers, are any two numbers closer to each other than the given threshold.

  Args:
    numbers (list of float): A list of numbers.
    threshold (float): The maximum allowed distance between any two numbers.

  Returns:
    boolean: True if there are any two numbers closer than the threshold, False otherwise.
  """
  (define (check-pairs i)
    (cond
      [(>= i (length numbers)) #f]
      [else
       (or
        (any (λ (j) (< (abs (- (list-ref numbers i) (list-ref numbers j))) threshold))
             (range (+ i 1) (length numbers)))
        (check-pairs (+ i 1)))]))
  (check-pairs 0))
```
'''

v2_completion11 = '''
```swift
func has_close_elements(numbers: [Double], threshold: Double) -> Bool {
    """
    Check if in the given array of numbers, are any two numbers closer to each other than the given threshold.

    Parameters:
        numbers: An array of Double values.
        threshold: A Double value representing the maximum allowed distance between any two numbers.

    Returns:
        A Boolean value indicating whether there are any two numbers in the array that are closer than the threshold.
    """
    for i in 0..<numbers.count {
        for j in (i+1)..<numbers.count {
            if abs(numbers[i] - numbers[j]) < threshold {
                return true
            }
        }
    }
    return false
}
```
'''

v2_completion12 = '''
```kotlin
/**
 * You are an expert Kotlin programmer, and here is your task.
 * Write a Kotlin function to find minimum sum of factors of a given number.
 *
 * >>> findMinSum(12)
 * 7
 * >>> findMinSum(105)
 * 15
 * >>> findMinSum(2)
 * 2
 */
fun findMinSum(num : Int) : Int {
    var n = num
    var minSum = Int.MAX_VALUE

    // Function to calculate the sum of digits of a number
    fun sumOfDigits(n: Int): Int {
        var sum = 0
        var number = n
        while (number > 0) {
            sum += number % 10
            number /= 10
        }
        return sum
    }

    // Iterate through all possible factors
    for (i in 1..Math.sqrt(n.toDouble()).toInt()) {
        if (n % i == 0) {
            val factor1 = i
            val factor2 = n / i
            val sumFactors = sumOfDigits(factor1) + sumOfDigits(factor2)
            if (sumFactors < minSum) {
                minSum = sumFactors
            }
        }
    }

    return minSum
}

// Test cases
fun main() {
    println(findMinSum(12))  // Output: 7
    println(findMinSum(105)) // Output: 15
    println(findMinSum(2))   // Output: 2
}
```
'''

v2_completion13 = '''
```go
package main

import (
	"encoding/json"
	"fmt"
)

// Code should be written in Go/Golang
// Write a function to flatten the given tuple matrix into the tuple list with each tuple representing each column.
// Examples:
// >>> matrix_to_list([[(4, 5), (7, 8)], [(10, 13), (18, 17)], [(0, 4), (10, 1)]])
// >>> '[(4, 7, 10, 18, 0, 10), (5, 8, 13, 17, 4, 1)]'
// >>> matrix_to_list([[(5, 6), (8, 9)], [(11, 14), (19, 18)], [(1, 5), (11, 2)]])
// >>> '[(5, 8, 11, 19, 1, 11), (6, 9, 14, 18, 5, 2)]'
// >>> matrix_to_list([[(6, 7), (9, 10)], [(12, 15), (20, 21)], [(23, 7), (15, 8)]])
// >>> '[(6, 9, 12, 20, 23, 15), (7, 10, 15, 21, 7, 8)]'
func matrix_to_list(test_list [][][]int) string {
	if len(test_list) == 0 || len(test_list[0]) == 0 {
		return "[]"
	}

	// Determine the number of columns
	numCols := len(test_list[0])
	// Determine the number of rows
	numRows := len(test_list)

	// Initialize a slice to hold the result
	result := make([][]int, len(test_list[0][0]))

	for i := 0; i < len(test_list[0][0]); i++ {
		result[i] = make([]int, numCols*numRows)
	}

	// Flatten the matrix
	for i := 0; i < numRows; i++ {
		for j := 0; j < numCols; j++ {
			for k := 0; k < len(test_list[i][j]); k++ {
				result[k][i*numCols+j] = test_list[i][j][k]
			}
		}
	}

	// Convert the result to JSON string
	jsonResult, _ := json.Marshal(result)
	return string(jsonResult)
}

func main() {
	// Test cases
	fmt.Println(matrix_to_list([][][]int{{{4, 5}, {7, 8}}, {{10, 13}, {18, 17}}, {{0, 4}, {10, 1}}}))
	fmt.Println(matrix_to_list([][][]int{{{5, 6}, {8, 9}}, {{11, 14}, {19, 18}}, {{1, 5}, {11, 2}}}))
	fmt.Println(matrix_to_list([][][]int{{{6, 7}, {9, 10}}, {{12, 15}, {20, 21}}, {{23, 7}, {15, 8}}}))
}
```
'''


def test_extract_code_from_freeform_completion_v2():
    r1, t1 = extract_code_from_freeform_completion_v2(v2_completion1, 'scala')
    assert r1.strip() == v2_result1.strip() and t1 == 'fenced'
    r2, t2 = extract_code_from_freeform_completion_v2(v2_completion2, 'scala')
    assert r2.strip() == v2_result2.strip() and t2 == 'fenced'
    r3, _ = extract_code_from_freeform_completion_v2(v2_completion3, 'go')
    assert "package main" not in r3
    r3_2, _ = extract_code_from_freeform_completion_v2(v2_completion3, 'go', no_removal=True)
    assert "package main" in r3_2
    r4, _ = extract_code_from_freeform_completion_v2(v2_completion4, 'java')
    assert "public class Solution" not in r4
    r5, tp = extract_code_from_freeform_completion_v2(v2_completion5, 'javascript', exactly_match=True)
    assert tp == 'fenced'
    r6_1, _ = extract_code_from_freeform_completion_v2(v2_completion6, 'python')
    assert '__main__' not in r6_1 and 'pass' not in r6_1
    r6_2, _ = extract_code_from_freeform_completion_v2(v2_completion6, 'python', is_ut=True)
    assert '__main__' in r6_2 and 'pass' in r6_2
    r7, _ = extract_code_from_freeform_completion_v2(v2_completion7,
                                                     'python',
                                                     first_block_only=True,
                                                     exactly_match=True)
    assert r7.strip() == v2_result7.strip()

    r7_, _ = extract_code_from_freeform_completion_v2(v2_completion7_,
                                                      'python',
                                                      first_block_only=True,
                                                      exactly_match=True,
                                                      remove_asserts=True)
    assert r7_.strip() == v2_result7.strip()

    r7_, _ = extract_code_from_freeform_completion_v2(v2_completion7_,
                                                      'python',
                                                      first_block_only=True,
                                                      exactly_match=True)
    assert r7_.strip() == v2_result7_.strip()

    r8, _ = extract_code_from_freeform_completion_v2(v2_completion8,
                                                     'csharp',
                                                     first_block_only=True,
                                                     exactly_match=True,
                                                     inner_function_only=True)
    assert r8.startswith('public static bool HasCloseElements')
    r9, _ = extract_code_from_freeform_completion_v2(v2_completion9,
                                                     'java',
                                                     first_block_only=True,
                                                     exactly_match=True,
                                                     inner_function_only=True)
    assert r9.startswith('public static boolean hasCloseElements')

    _, tp = extract_code_from_freeform_completion_v2(v2_completion10,
                                                     'racket',
                                                     first_block_only=True,
                                                     exactly_match=True)
    assert tp == 'fenced'
    _, tp = extract_code_from_freeform_completion_v2(v2_completion11,
                                                     'swift',
                                                     first_block_only=True,
                                                     exactly_match=True)
    assert tp == 'fenced'

    r12, _ = extract_code_from_freeform_completion_v2(v2_completion12,
                                                      'kotlin',
                                                      first_block_only=True,
                                                      no_removal=True,
                                                      exactly_match=True)
    assert 'fun main' not in r12
    r13, _ = extract_code_from_freeform_completion_v2(v2_completion13,
                                                      'go',
                                                      first_block_only=True,
                                                      no_removal=True,
                                                      exactly_match=True)
    assert 'func main' not in r13 and 'package main' in r13


default_completion_1 = '''
To solve the problem of determining the minimum cost to make two binary strings equal through specified flip operations, we can break down the solution into a series of steps. The key is to identify the mismatches between the two strings and then decide the most cost-effective way to resolve those mismatches.

Here's the implementation of the function `get_min_flip_cost_to_match`:

```python
def get_min_flip_cost_to_match(s1: str, s2: str, x: int) -> int:
    """
    You are given two binary strings s1 and s2 of length n, and a positive integer x.

    You can perform the following operations on s1 any number of times:

    - Choose two indices i and j, and flip s1[i] and s1[j]. The cost is x.

    - Choose an index i < n - 1, and flip s1[i] and s1[i+1]. The cost is 1.

    Return the minimum total cost to make s1 equal to s2, or -1 if it's impossible.

    Flipping a character means changing 0 to 1 or 1 to 0.
    e.g. assert get_min_flip_cost_to_match("1100","0011",3) == 2
    """
    
    n = len(s1)
    
    # Count the number of mismatches
    count_01 = 0  # Count of positions where s1 has '0' and s2 has '1'
    count_10 = 0  # Count of positions where s1 has '1' and s2 has '0'
    
    for i in range(n):
        if s1[i] != s2[i]:
            if s1[i] == '0':
                count_01 += 1
            else:
                count_10 += 1
    
    # If the number of mismatches is odd, it's impossible to make them equal
    if (count_01 + count_10) % 2 != 0:
        return -1
    
    # Calculate the minimum cost
    # We can pair up mismatches (01 with 10) using the cost of 1
    pairs = min(count_01, count_10)
    remaining_01 = count_01 - pairs
    remaining_10 = count_10 - pairs
    
    # Each pair can be resolved with cost 1
    cost = pairs
    
    # Remaining mismatches can only be resolved using the x cost operation
    # Each remaining mismatch requires a flip of two indices
    cost += (remaining_01 + remaining_10) // 2 * x
    
    return cost

# Example usage
assert get_min_flip_cost_to_match("1100", "0011", 3) == 2
```

### Explanation:
1. **Mismatch Counting**: We iterate through both strings and count how many positions have mismatches of type `01` (where `s1` has `0` and `s2` has `1`) and `10` (where `s1` has `1` and `s2` has `0`).

2. **Odd Mismatches Check**: If the total number of mismatches is odd, it's impossible to make the strings equal, so we return `-1`.

3. **Cost Calculation**:
   - We can resolve pairs of mismatches (one `01` and one `10`) at a cost of `1` each.
   - Any remaining mismatches (if they exist) will need to be resolved using the more expensive operation, which costs `x`. Each remaining mismatch requires two flips, so we calculate the cost accordingly.

This approach ensures that we find the minimum cost efficiently.
'''


def test_custom_code_block():
    res = default_extract_helper(completion=default_completion_1,
                                 language='python',
                                 custom_extract_logic='''
assert_token = '\\nassert'
code_blocks = extract_fenced_code(completion)
completion = code_blocks[0].code
index = completion.find(assert_token)
if index != -1:
    completion = completion[:index]
submit_code_blocks([CodeBlock(priority=40, code=completion, language='python')])
''')
    assert res == '''def get_min_flip_cost_to_match(s1: str, s2: str, x: int) -> int:
    """
    You are given two binary strings s1 and s2 of length n, and a positive integer x.

    You can perform the following operations on s1 any number of times:

    - Choose two indices i and j, and flip s1[i] and s1[j]. The cost is x.

    - Choose an index i < n - 1, and flip s1[i] and s1[i+1]. The cost is 1.

    Return the minimum total cost to make s1 equal to s2, or -1 if it's impossible.

    Flipping a character means changing 0 to 1 or 1 to 0.
    e.g. assert get_min_flip_cost_to_match("1100","0011",3) == 2
    """
    
    n = len(s1)
    
    # Count the number of mismatches
    count_01 = 0  # Count of positions where s1 has '0' and s2 has '1'
    count_10 = 0  # Count of positions where s1 has '1' and s2 has '0'
    
    for i in range(n):
        if s1[i] != s2[i]:
            if s1[i] == '0':
                count_01 += 1
            else:
                count_10 += 1
    
    # If the number of mismatches is odd, it's impossible to make them equal
    if (count_01 + count_10) % 2 != 0:
        return -1
    
    # Calculate the minimum cost
    # We can pair up mismatches (01 with 10) using the cost of 1
    pairs = min(count_01, count_10)
    remaining_01 = count_01 - pairs
    remaining_10 = count_10 - pairs
    
    # Each pair can be resolved with cost 1
    cost = pairs
    
    # Remaining mismatches can only be resolved using the x cost operation
    # Each remaining mismatch requires a flip of two indices
    cost += (remaining_01 + remaining_10) // 2 * x
    
    return cost

# Example usage'''
