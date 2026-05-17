# AutoEval

The AutoEval dataset determines whether a problem is passed by appending a piece of test code to the model output code and executing it. It supports multiple languages.

## Data Format

The problem format accepted by the AutoEval dataset is as follows:

```json
{
    "id": str,                          # Unique identifier
    "content": str,                     # Problem content
    "labels": {
        "programming_language": str,    # Programming language
        "execution_language": str       # Optional, execution language, defaults to the same as programming language
        "context": str,                 # Optional, context information, if it exists, it will be added to the prompt
        "fewshot": str,                 # Optional, prompt for fewshot mode
        "prompt_template": str,         # Optional, template string used to concat the prompt
    },
    "test": {
        "code": str,                    # Test code
        "asset": {
            "filename": str             # Filename -> base64 encoded file content
        }
    },
    "canonical_solution": str           # Used to store the correct answer in the data
}
```

**programming_language** is the language to extract from the model's output. It appears after the ``` at the beginning of the code block.

**execution_language** is the language used when executing the test, corresponding to the language parameter of the [run_code](/docs/api/run-code-run-code-post) interface.  

For example, if a problem requires the model to output SQL statements, but the test code needs to check if it is correct through Python, then `programming_language` = `sql` and `execution_language` = `python`.

**canonical_solution** is used to store the correct answer in the data. The sandbox will not read this field. Therefore, it can be of any type, and it is recommended to store it as a single string.

## Prompt Generation Logic

In the data, the fields related to Prompt generation are:

- content
- labels.context 
- labels.fewshot
- labels.prompt_template

The last three can be overridden in the config.extra of the incoming request.

If prompt_template exists, Template will be used for string substitution when generating the prompt. Available variables include:

- question: content
- fewshot: labels.fewshot 
- context: labels.context
- locale: the language used (en, zh, etc)

Default concatenation method:

export const PromptTemplate = () => {
  const styles = {
    container: {
      fontFamily: 'monospace',
      maxWidth: '600px',
      margin: '20px auto',
      padding: '20px',
      border: '1px solid #ddd',
      borderRadius: '8px',
      backgroundColor: '#f8f9fa',
    },
    section: {
      margin: '10px 0',
      padding: '12px',
      borderRadius: '6px',
      position: 'relative',
    },
    optional: {
      position: 'absolute',
      top: '-10px',
      right: '10px',
      fontSize: '12px',
      padding: '2px 6px',
      backgroundColor: '#fff',
      border: '1px solid #ddd',
      borderRadius: '4px',
    },
    separator: {
      borderBottom: '2px dashed #666',
      margin: '10px 0',
      width: '100%',
    },
    legend: {
      display: 'flex',
      gap: '20px',
      marginTop: '20px',
      flexWrap: 'wrap',
    },
    legendItem: {
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
      fontSize: '14px',
    },
    colorBox: {
      width: '16px',
      height: '16px',
      borderRadius: '4px',
    },
  };

  const sections = [
    { name: 'Context', color: '#e9f5e9', optional: true },
    { name: 'Few-shot Example', color: '#fff3e0', optional: true },
    { name: 'Question', color: '#e3f2fd', optional: false },
    { name: 'Answer', color: '#fce4ec', optional: false },
  ];

  return (
    <div style={styles.container}>
      <div>
        {sections.map((section, index) => (
          <div key={index}>
            <div
              style={{
                ...styles.section,
                backgroundColor: section.color,
              }}
            >
              {section.optional && (
                <span style={styles.optional}>
                  Optional
                  {section.name === 'Context' && '(labels.context exists)'}
                  {section.name === 'Few-shot Example' && '(config.is_fewshot is true)'}
                </span>
              )}
              <div style={{ marginBottom: '8px', fontWeight: 'bold' }}>
                {section.name === 'Question' && section.name + ':'}
                {section.name === 'Answer' && section.name + ':'}
              </div>
              <div style={{ color: '#666' }}>
                {section.name === 'Context' && '// context info...'}
                {section.name === 'Few-shot Example' && '// Few-shot examples...'}
                {section.name === 'Question' && '// question content...'}
              </div>
            </div>
            {[0].includes(index) && <div style={styles.separator} />}
          </div>
        ))}
      </div>

      <div style={styles.legend}>
        {sections.map((section, index) => (
          <div key={index} style={styles.legendItem}>
            <div
              style={{
                ...styles.colorBox,
                backgroundColor: section.color,
              }}
            />
            <span>{section.name}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

<PromptTemplate />

## Evaluation Logic

### Code Extraction 

Related configuration items:

- `config.extra.code_block_idx`: Can specify which code block to extract
- `config.extra.autoeval_extract_code_mode`: Controls the code block extraction mode, only takes effect when `code_block_idx` does not exist
  - `first`: Only extract the first code block
  - `all`: Merge all code blocks

**Extraction Process**

1. First try to extract complete fenced code blocks (code blocks marked with ` ``` `) 
2. If no complete code block is found, try to extract incomplete fenced code blocks
3. If still not found, use heuristics to extract code

To facilitate testing, modifications are made to the extracted code for some languages:

- Python: Remove the code after `if __name__ == "__main__"`
- C++: Remove the code after `int main()`  
- Go: Remove the `package main` declaration

### Code Concatenation

- If the test code contains the `#<INSERT>` tag:
  - Insert the extracted code at the marked location  
- Otherwise:
  - Directly append the test code to the extracted code
- `config.extra.repr_code`: Controls whether to perform `repr()` processing on the extracted code, making it convenient for the test code to reference literals

There are some post-processing for the Go language:

- Merge all package declarations
- Integrate all import statements  
- Reorganize the code structure to ensure correctness

### Code Execution

When the code is executed, the files stored in `test.asset` in the data will be [placed in the corresponding files in the sandbox runtime environment](/docs/docs/reference/execution-detail/common). `test.asset` is a dictionary, where the key is the filename and the value is the base64-encoded content of that file.

The logic for executing code is divided into two cases according to `labels.programming_language`: `java` and others.

For the `java` language, our testing logic is the same as [NaturalCodeBench](https://github.com/THUDM/NaturalCodeBench):

- Detect all Java class/enum/interface... in the code  
- Place each object in the corresponding file named after their name
- [Execute junit](/docs/docs/reference/execution-detail/java)

For all other languages, directly execute the concatenated code according to the language specified in `labels.execution_language`. Please refer to the [Execution Details](/docs/category/execution-detail) section for the execution logic corresponding to each language.

### Result Judgment

The AutoEval evaluation set determines whether a problem is passed by judging whether `run_result.return_code` in the code execution result, that is, the return value of the corresponding process, is 0.

If the return value is not 0, it means that some kind of assert statement did not pass, or the test tool found that some test cases failed.

For Python problems, in order to prevent the model from outputting `exit(0)` to hack some non-rigorous tests, we provide the `config.extra.append_flag` option. If true, a command to print a random string will be appended to the end of the Python code. If this random string is not in the final output, even if the program's return value is 0, it will be judged as a failure.

## Data Examples


### Python

```json
{
  "id": 600,
  "content": "\nPlease use Python to implement the calculation of the cosine similarity of two articles. \nRequirements:\n1. Include a function named `doc_cosine_similarity` to calculate the cosine similarity of articles,\n2. Implement it efficiently using a hash table, avoid calling other ready-made methods\n3. Each article is already tokenized and is input as a list\n5. Please add type annotations according to Python 3 syntax\n\n\n",
  "canonical_solution": "from typing import List\nimport math\n\ndef doc_cosine_similarity(doc1: List[str], doc2: List[str]) -> float:\n    # Create word frequency dictionaries\n    word_freq1 = {}\n    word_freq2 = {}\n    # Calculate word frequency of the first article\n    for word in doc1:\n        word_freq1[word] = word_freq1.get(word, 0) + 1\n\n    # Calculate word frequency of the second article\n    for word in doc2:\n        word_freq2[word] = word_freq2.get(word, 0) + 1\n\n    # Calculate the dot product of word frequency vectors\n    dot_product = 0\n    for word, freq in word_freq1.items():\n        if word in word_freq2:\n            dot_product += freq * word_freq2[word]\n\n    # Calculate the modulus of the two word frequency vectors\n    magnitude1 = math.sqrt(sum([freq ** 2 for freq in word_freq1.values()]))\n    magnitude2 = math.sqrt(sum([freq ** 2 for freq in word_freq2.values()]))\n\n    # Avoid dividing by zero\n    if magnitude1 == 0 or magnitude2 == 0:\n        return 0.0\n\n    # Calculate cosine similarity\n    cosine_similarity = dot_product / (magnitude1 * magnitude2)\n\n    return cosine_similarity",
  "labels": {
    "programming_language": "python",
    "execution_language": "python",  
    "fewshot": "omitted..."
  },
  "test": {
    "code": "#<INSERT>\n\ndef check():\n    doc1 = [\"I\", \"like\", \"watching\", \"movies\", \"because\", \"movies\", \"are\", \"high\", \"quality\"]\n    doc2 = [\"Movies\", \"are\", \"my\", \"hobby\"]\n    doc3 = []\n\n    assert round(doc_cosine_similarity(doc1, doc2), 5) == 0.42426\n    assert round(doc_cosine_similarity(doc1, doc1), 5) == 1.0\n    assert doc_cosine_similarity(doc1, doc3) == 0.0\n\ncheck()"
  }
}
```

An example of passing files via asset:

```json
{
  "id": 3,
  "content": "\nThere is a dataset named 'Hitters_X_train.csv', the first two rows of data are as follows:\n| Unnamed: 0         |   AtBat |   Hits |   HmRun |   Runs |   RBI |   Walks |   Years |   CAtBat |   CHits |   CHmRun |   CRuns |   CRBI |   CWalks |   LeagueN |   DivisionW |   PutOuts |   Assists |   Errors |   NewLeagueN |\n|:-------------------|--------:|-------:|--------:|-------:|------:|--------:|--------:|---------:|--------:|---------:|--------:|-------:|---------:|----------:|------------:|----------:|----------:|---------:|-------------:|\n| -Darryl Strawberry |     475 |    123 |      27 |     76 |    93 |      72 |       4 |     1810 |     471 |      108 |     292 |    343 |      267 |         1 |           0 |       226 |        10 |        6 |            1 |\n| -Glenn Wilson      |     584 |    158 |      15 |     70 |    84 |      42 |       5 |     2358 |     636 |       58 |     265 |    316 |      134 |         1 |           0 |       331 |        20 |        4 |            1 |\n\nQuestion:\nPerform a correlation analysis of the number of assists (Assists) and errors (Errors) for players.\n\nThe answer needs to meet the following requirements:\n1. The data file is 'Hitters_X_train.csv', please strictly refer to the header of the data and use Python code to answer the question.\n2. Use 'pandas.read_csv' to read the data, assuming the data is in the same path as the code.\n3. Write the analysis code as a function called 'proc_data()' that does not require any input parameters, and use the correlation coefficient from the question as the return value.\n",
  "labels": {
    "execution_language": "python",
    "programming_language": "python", 
  },
  "test": {
    "asset": "{\"Hitters_X_train.csv\": \"IiIsIkF0QmF0IiwiSGl0cyIsIkhtUnVuIiwiUnVucyIsIlJCSSIsIldhbGtzIiwiWWVhcnMiLCJDQXRCYXQiLCJDSGl0cyIsIkNIbVJ1biIsIkNSdW5zIiwiQ1JCSSIsIkNXYWxrcyIsIkxlYWd1ZU4iLCJEaXZpc2lvblciLCJQdXRPdXRzIiwiQXNzaXN0cyIsIkVycm9ycyIsIk5ld0xlYWd1ZU4iCiItRGFycnlsIFN0cmF3YmVycnkiLDQ3NSwxMjMsMjcsNzYsOTMsNzIsNCwxODEwLDQ3MSwxMDgsMjkyLDM0MywyNjcsMSwwLDIyNiwxMCw2LDEKIi1HbGVubiBXaWxzb24iLDU4NCwxNTgsMTUsNzAsODQsNDIsNSwyMzU4LDYzNiw1OCwyNjUsMzE2LDEzNCwxLDAsMzMxLDIwLDQsMQoiLUxlb24gRHVyaGFtIiw0ODQsMTI3LDIwLDY2LDY1LDY3LDcsMzAwNiw4NDQsMTE2LDQzNiw0NTgsMzc3LDEsMCwxMjMxLDgwLDcsMQoiLVRvbnkgR3d5bm4iLDY0MiwyMTEsMTQsMTA3LDU5LDUyLDUsMjM2NCw3NzAsMjcsMzUyLDIzMCwxOTMsMSwxLDMzNywxOSw0LDEKIi1EYXZlIENvbmNlcGNpb24iLDMxMSw4MSwzLDQyLDMwLDI2LDE3LDgyNDcsMjE5OCwxMDAsOTUwLDkwOSw2OTAsMSwxLDE1MywyMjMsMTAsMQoiLVRvbSBCcm9va2VucyIsMjgxLDc2LDMsNDIsMjUsMjAsOCwyNjU4LDY1Nyw0OCwzMjQsMzAwLDE3OSwwLDAsMTA2LDE0NCw3LDAKIi1UaW0gTGF1ZG5lciIsMTkzLDQ3LDEwLDIxLDI5LDI0LDYsMTEzNiwyNTYsNDIsMTI5LDEzOSwxMDYsMCwxLDI5OSwxMyw1LDAKIi1NaWtlIE1hcnNoYWxsIiwzMzAsNzcsMTksNDcsNTMsMjcsNiwxOTI4LDUxNiw5MCwyNDcsMjg4LDE2MSwxLDEsMTQ5LDgsNiwxCiItTWFydHkgQmFycmV0dCIsNjI1LDE3OSw0LDk0LDYwLDY1LDUsMTY5Niw0NzYsMTIsMjE2LDE2MywxNjYsMCwwLDMwMyw0NTAsMTQsMAoiLUJ1ZGR5IEJpYW5jYWxhbmEiLDE5MCw0NiwyLDI0LDgsMTUsNSw0NzksMTAyLDUsNjUsMjMsMzksMCwxLDEwMiwxNzcsMTYsMAoiLVdpbGxpZSBNY0dlZSIsNDk3LDEyNyw3LDY1LDQ4LDM3LDUsMjcwMyw4MDYsMzIsMzc5LDMxMSwxMzgsMSwwLDMyNSw5LDMsMQoiLUNhbCBSaXBrZW4iLDYyNywxNzcsMjUsOTgsODEsNzAsNiwzMjEwLDkyNywxMzMsNTI5LDQ3MiwzMTMsMCwwLDI0MCw0ODIsMTMsMAoiLU1pa2UgU2NobWlkdCIsMjAsMSwwLDAsMCwwLDIsNDEsOSwyLDYsNyw0LDEsMCw3OCwyMjAsNiwxCiItR2FyeSBXYXJkIiwzODAsMTIwLDUsNTQsNTEsMzEsOCwzMTE4LDkwMCw5Miw0NDQsNDE5LDI0MCwwLDEsMjM3LDgsMSwwCiItUmFmYWVsIEJlbGxpYXJkIiwzMDksNzIsMCwzMywzMSwyNiw1LDM1NCw4MiwwLDQxLDMyLDI2LDEsMCwxMTcsMjY5LDEyLDEKIi1KaW0gUHJlc2xleSIsNjE2LDE2MywyNyw4MywxMDcsMzIsMywxNDM3LDM3Nyw2NSwxODEsMjI3LDgyLDAsMSwxMTAsMzA4LDE1LDAKIi1Nb29raWUgV2lsc29uIiwzODEsMTEwLDksNjEsNDUsMzIsNywzMDE1LDgzNCw0MCw0NTEsMjQ5LDE2OCwxLDAsMjI4LDcsNSwxCiItVG9ueSBQZW5hIiw1MTAsMTQ3LDEwLDU2LDUyLDUzLDcsMjg3Miw4MjEsNjMsMzA3LDM0MCwxNzQsMSwwLDgxMCw5OSwxOCwxCiItR2FyeSBSZWR1cyIsMzQwLDg0LDExLDYyLDMzLDQ3LDUsMTUxNiwzNzYsNDIsMjg0LDE0MSwyMTksMSwwLDE4NSw4LDQsMAoiLVBhdCBTaGVyaWRhbiIsMjM2LDU2LDYsNDEsMTksMjEsNSwxMjU3LDMyOSwyNCwxNjYsMTI1LDEwNSwwLDAsMTcyLDEsNCwwCiItU3RldmUgTG9tYmFyZG96emkiLDQ1MywxMDMsOCw1MywzMyw1MiwyLDUwNywxMjMsOCw2MywzOSw1OCwwLDEsMjg5LDQwNyw2LDAKIi1EYXJuZWxsIENvbGVzIiw1MjEsMTQyLDIwLDY3LDg2LDQ1LDQsODE1LDIwNSwyMiw5OSwxMDMsNzgsMCwwLDEwNywyNDIsMjMsMAoiLUxhcnJ5IFNoZWV0cyIsMzM4LDkyLDE4LDQyLDYwLDIxLDMsNjgyLDE4NSwzNiw4OCwxMTIsNTAsMCwwLDAsMCwwLDAKIi1Cb2IgTWVsdmluIiwyNjgsNjAsNSwyNCwyNSwxNSwyLDM1MCw3OCw1LDM0LDI5LDE4LDEsMSw0NDIsNTksNiwxCiItRHdheW5lIE11cnBoeSIsMzI5LDgzLDksNTAsMzksNTYsOSwzODI4LDk0OCwxNDUsNTc1LDUyOCw2MzUsMCwxLDI3Niw2LDIsMAoiLUdyYWlnIE5ldHRsZXMiLDM1NCw3NywxNiwzNiw1NSw0MSwyMCw4NzE2LDIxNzIsMzg0LDExNzIsMTI2NywxMDU3LDEsMSw4MywxNzQsMTYsMQoiLUFuZHJlcyBHYWxhcnJhZ2EiLDMyMSw4NywxMCwzOSw0MiwzMCwyLDM5NiwxMDEsMTIsNDgsNDYsMzMsMSwwLDgwNSw0MCw0LDEKIi1HYXJ5IE1hdHRoZXdzIiwzNzAsOTYsMjEsNDksNDYsNjAsMTUsNjk4NiwxOTcyLDIzMSwxMDcwLDk1NSw5MjEsMSwwLDEzNyw1LDksMQoiLVJpY2sgTWFubmluZyIsMjA1LDUyLDgsMzEsMjcsMTcsMTIsNTEzNCwxMzIzLDU2LDY0Myw0NDUsNDU5LDAsMCwxNTUsMywyLDAKIi1HZW9yZ2UgQmVsbCIsNjQxLDE5OCwzMSwxMDEsMTA4LDQxLDUsMjEyOSw2MTAsOTIsMjk3LDMxOSwxMTcsMCwwLDI2OSwxNywxMCwwCiItSm9keSBEYXZpcyIsNTI4LDEzMiwyMSw2MSw3NCw0MSw2LDI2NDEsNjcxLDk3LDI3MywzODMsMjI2LDEsMCw4ODUsMTA1LDgsMQoiLUtlaXRoIEhlcm5hbmRleiIsNTUxLDE3MSwxMyw5NCw4Myw5NCwxMyw2MDkwLDE4NDAsMTI4LDk2OSw5MDAsOTE3LDEsMCwxMTk5LDE0OSw1LDEKIi1KdWxpbyBGcmFuY28iLDU5OSwxODMsMTAsODAsNzQsMzIsNSwyNDgyLDcxNSwyNywzMzAsMzI2LDE1OCwwLDAsMjMxLDM3NCwxOCwwCiItQ2FybWVsbyBNYXJ0aW5leiIsMjQ0LDU4LDksMjgsMjUsMzUsNCwxMzM1LDMzMyw0OSwxNjQsMTc5LDE5NCwxLDEsMTQyLDE0LDIsMQoiLVRvbSBQYWNpb3JlayIsMjEzLDYxLDQsMTcsMjIsMywxNyw0MDYxLDExNDUsODMsNDg4LDQ5MSwyNDQsMCwxLDE3OCw0NSw0LDAKIi1MZWUgTGFjeSIsNDkxLDE0MSwxMSw3Nyw0NywzNywxNSw0MjkxLDEyNDAsODQsNjE1LDQzMCwzNDAsMCwwLDIzOSw4LDIsMAoiLU96emllIEd1aWxsZW4iLDU0NywxMzcsMiw1OCw0NywxMiwyLDEwMzgsMjcxLDMsMTI5LDgwLDI0LDAsMSwyNjEsNDU5LDIyLDAKIi1CaWxsIERvcmFuIiw1NTAsMTUyLDYsOTIsMzcsODEsNSwyMzA4LDYzMywzMiwzNDksMTgyLDMwOCwxLDEsMjYyLDMyOSwxNiwxCiItTWlrZSBEaWF6IiwyMDksNTYsMTIsMjIsMzYsMTksMiwyMTYsNTgsMTIsMjQsMzcsMTksMSwwLDIwMSw2LDMsMQoiLUdhcnkgUGV0dGlzIiw1MzksMTM5LDUsOTMsNTgsNjksNSwxNDY5LDM2OSwxMiwyNDcsMTI2LDE5OCwwLDEsNDYyLDksNywwCiItT3p6aWUgVmlyZ2lsIiwzNTksODAsMTUsNDUsNDgsNjMsNywxNDkzLDM1OSw2MSwxNzYsMjAyLDE3NSwxLDEsNjgyLDkzLDEzLDEKIi1LZXZpbiBNaXRjaGVsbCIsMzI4LDkxLDEyLDUxLDQzLDMzLDIsMzQyLDk0LDEyLDUxLDQ0LDMzLDEsMCwxNDUsNTksOCwxCiItTWlrZSBTY2lvc2NpYSIsMzc0LDk0LDUsMzYsMjYsNjIsNywxOTY4LDUxOSwyNiwxODEsMTk5LDI4OCwxLDEsNzU2LDY0LDE1LDEKIi1Kb2huIE1vc2VzIiwzOTksMTAyLDMsNTYsMzQsMzQsNSw2NzAsMTY3LDQsODksNDgsNTQsMCwxLDIxMSw5LDMsMAoiLUpvaG5ueSBHcnViYiIsMjEwLDcwLDEzLDMyLDUxLDI4LDE1LDQwNDAsMTEzMCw5Nyw1NDQsNDYyLDU1MSwwLDAsMCwwLDAsMAoiLVRpbSBXYWxsYWNoIiw0ODAsMTEyLDE4LDUwLDcxLDQ0LDcsMzAzMSw3NzEsMTEwLDMzOCw0MDYsMjM5LDEsMCw5NCwyNzAsMTYsMQoiLUFsIE5ld21hbiIsMTg1LDM3LDEsMjMsOCwyMSwyLDIxNCw0MiwxLDMwLDksMjQsMSwwLDc2LDEyNyw3LDAKIi1IYXJyeSBTcGlsbWFuIiwxNDMsMzksNSwxOCwzMCwxNSw5LDYzOSwxNTEsMTYsODAsOTcsNjEsMSwxLDEzOCwxNSwxLDEKIi1UZXJyeSBLZW5uZWR5IiwxOSw0LDEsMiwzLDEsMSwxOSw0LDEsMiwzLDEsMSwxLDY5Miw3MCw4LDAKIi1LdXJ0IFN0aWxsd2VsbCIsMjc5LDY0LDAsMzEsMjYsMzAsMSwyNzksNjQsMCwzMSwyNiwzMCwxLDEsMTA3LDIwNSwxNiwxCiItSGFsIE1jUmFlIiwyNzgsNzAsNywyMiwzNywxOCwxOCw3MTg2LDIwODEsMTkwLDkzNSwxMDg4LDY0MywwLDEsMCwwLDAsMAoiLU96emllIFNtaXRoIiw1MTQsMTQ0LDAsNjcsNTQsNzksOSw0NzM5LDExNjksMTMsNTgzLDM3NCw1MjgsMSwwLDIyOSw0NTMsMTUsMQoiLVNoYXdvbiBEdW5zdG9uIiw1ODEsMTQ1LDE3LDY2LDY4LDIxLDIsODMxLDIxMCwyMSwxMDYsODYsNDAsMSwwLDMyMCw0NjUsMzIsMQoiLVRpdG8gTGFuZHJ1bSIsMjA1LDQzLDIsMjQsMTcsMjAsNyw4NTQsMjE5LDEyLDEwNSw5OSw3MSwxLDAsMTMxLDYsMSwxCiItQnVkZHkgQmVsbCIsNTY4LDE1OCwyMCw4OSw3NSw3MywxNSw4MDY4LDIyNzMsMTc3LDEwNDUsOTkzLDczMiwxLDEsMTA1LDI5MCwxMCwxCiItQmlsbCBCdWNrbmVyIiw2MjksMTY4LDE4LDczLDEwMiw0MCwxOCw4NDI0LDI0NjQsMTY0LDEwMDgsMTA3Miw0MDIsMCwwLDEwNjcsMTU3LDE0LDAKIi1EYW4gUGFzcXVhIiwyODAsODIsMTYsNDQsNDUsNDcsMiw0MjgsMTEzLDI1LDYxLDcwLDYzLDAsMCwxNDgsNCwyLDAKIi1KdWFuIEJlbmlxdWV6IiwzNDMsMTAzLDYsNDgsMzYsNDAsMTUsNDMzOCwxMTkzLDcwLDU4MSw0MjEsMzI1LDAsMCwyMTEsNTYsMTMsMAoiLUtldmluIEJhc3MiLDU5MSwxODQsMjAsODMsNzksMzgsNSwxNjg5LDQ2Miw0MCwyMTksMTk1LDgyLDEsMSwzMDMsMTIsNSwxCiItR3JlZyBCcm9jayIsMzI1LDc2LDE2LDMzLDUyLDM3LDUsMTUwNiwzNTEsNzEsMTk1LDIxOSwyMTQsMSwxLDcyNiw4NywzLDAKIi1QaGlsIEdhcm5lciIsMzEzLDgzLDksNDMsNDEsMzAsMTQsNTg4NSwxNTQzLDEwNCw3NTEsNzE0LDUzNSwxLDEsNTgsMTQxLDIzLDEKIi1Eb25uaWUgSGlsbCIsMzM5LDk2LDQsMzcsMjksMjMsNCwxMDY0LDI5MCwxMSwxMjMsMTA4LDU1LDAsMSwxMDQsMjEzLDksMAoiLVJvbiBSb2VuaWNrZSIsMjc1LDY4LDUsNDIsNDIsNjEsNiw5NjEsMjM4LDE2LDEyOCwxMDQsMTcyLDEsMCwxODEsMywyLDEKIi1EYXJyZWxsIFBvcnRlciIsMTU1LDQxLDEyLDIxLDI5LDIyLDE2LDU0MDksMTMzOCwxODEsNzQ2LDgwNSw4NzUsMCwxLDE2NSw5LDEsMAoiLUp1YW4gU2FtdWVsIiw1OTEsMTU3LDE2LDkwLDc4LDI2LDQsMjAyMCw1NDEsNTIsMzEwLDIyNiw5MSwxLDAsMjkwLDQ0MCwyNSwxCiItUm9ubiBSZXlub2xkcyIsMTI2LDI3LDMsOCwxMCw1LDQsMjM5LDQ5LDMsMTYsMTMsMTQsMSwwLDE5MCwyLDksMQoiLUdhcnJ5IFRlbXBsZXRvbiIsNTEwLDEyNiwyLDQyLDQ0LDM1LDExLDU1NjIsMTU3OCw0NCw3MDMsNTE5LDI1NiwxLDEsMjA3LDM1OCwyMCwxCiItTGVuIER5a3N0cmEiLDQzMSwxMjcsOCw3Nyw0NSw1OCwyLDY2NywxODcsOSwxMTcsNjQsODgsMSwwLDI4Myw4LDMsMQoiLUJydWNlIEJvY2h5IiwxMjcsMzIsOCwxNiwyMiwxNCw4LDcyNywxODAsMjQsNjcsODIsNTYsMSwxLDIwMiwyMiwyLDEKIi1XYWRlIEJvZ2dzIiw1ODAsMjA3LDgsMTA3LDcxLDEwNSw1LDI3NzgsOTc4LDMyLDQ3NCwzMjIsNDE3LDAsMCwxMjEsMjY3LDE5LDAKIi1Sb24gT2VzdGVyIiw1MjMsMTM1LDgsNTIsNDQsNTIsOSwzMzY4LDg5NSwzOSwzNzcsMjg0LDI5NiwxLDEsMzY3LDQ3NSwxOSwxCiItTWlrZSBEYXZpcyIsNDg5LDEzMSwxOSw3Nyw1NSwzNCw3LDIwNTEsNTQ5LDYyLDMwMCwyNjMsMTUzLDAsMSwzMTAsOSw5LDAKIi1SaWNrZXkgSGVuZGVyc29uIiw2MDgsMTYwLDI4LDEzMCw3NCw4OSw4LDQwNzEsMTE4MiwxMDMsODYyLDQxNyw3MDgsMCwwLDQyNiw0LDYsMAoiLVRvbW15IEhlcnIiLDU1OSwxNDEsMiw0OCw2MSw3Myw4LDMxNjIsODc0LDE2LDQyMSwzNDksMzU5LDEsMCwzNTIsNDE0LDksMQoiLVRvbSBGb2xleSIsMjYzLDcwLDEsMjYsMjMsMzAsNCw4ODgsMjIwLDksODMsODIsODYsMSwwLDgxLDE0Nyw0LDEKIi1NaWtlIEtpbmdlcnkiLDIwOSw1NCwzLDI1LDE0LDEyLDEsMjA5LDU0LDMsMjUsMTQsMTIsMCwxLDEwMiw2LDMsMAoiLVRlZCBTaW1tb25zIiwxMjcsMzIsNCwxNCwyNSwxMiwxOSw4Mzk2LDI0MDIsMjQyLDEwNDgsMTM0OCw4MTksMSwxLDE2NywxOCw2LDEKIi1EZW5ueSBXYWxsaW5nIiwzODIsMTE5LDEzLDU0LDU4LDM2LDEyLDIxMzMsNTk0LDQxLDI4NywyOTQsMjI3LDEsMSw1OSwxNTYsOSwxCiItU2lkIEJyZWFtIiw1MjIsMTQwLDE2LDczLDc3LDYwLDQsNzMwLDE4NSwyMiw5MywxMDYsODYsMSwwLDEzMjAsMTY2LDE3LDEKIi1NaXRjaCBXZWJzdGVyIiw1NzYsMTY3LDgsODksNDksNTcsNCw4MjIsMjMyLDE5LDEzMiw4Myw3OSwxLDAsMzI1LDEyLDgsMQoiLVRvbnkgRmVybmFuZGV6Iiw2ODcsMjEzLDEwLDkxLDY1LDI3LDQsMTUxOCw0NDgsMTUsMTk2LDEzNyw4OSwwLDAsMjk0LDQ0NSwxMywwCiItUm9uIEhhc3NleSIsMzQxLDExMCw5LDQ1LDQ5LDQ2LDksMjMzMSw2NTgsNTAsMjQ5LDMyMiwyNzQsMCwwLDI1MSw5LDQsMAoiLVJheSBLbmlnaHQiLDQ4NiwxNDUsMTEsNTEsNzYsNDAsMTEsMzk2NywxMTAyLDY3LDQxMCw0OTcsMjg0LDEsMCw4OCwyMDQsMTYsMAoiLURhdmUgSGVuZGVyc29uIiwzODgsMTAzLDE1LDU5LDQ3LDM5LDYsMjE3NCw1NTUsODAsMjg1LDI3NCwxODYsMCwxLDE4Miw5LDQsMAoiLVRpbSBGbGFubmVyeSIsMzY4LDEwMywzLDQ4LDI4LDU0LDgsMTg5Nyw0OTMsOSwyMDcsMTYyLDE5OCwxLDEsMjA5LDI0NiwzLDEKIi1DaGlsaSBEYXZpcyIsNTI2LDE0NiwxMyw3MSw3MCw4NCw2LDI2NDgsNzE1LDc3LDM1MiwzNDIsMjg5LDEsMSwzMDMsOSw5LDEKIi1KZWZmIFJlZWQiLDE2NSwzOSwyLDEzLDksMTYsMywxOTYsNDQsMiwxOCwxMCwxOCwwLDEsMzMyLDE5LDIsMQoiLUJyZXR0IEJ1dGxlciIsNTg3LDE2Myw0LDkyLDUxLDcwLDYsMjY5NSw3NDcsMTcsNDQyLDE5OCwzMTcsMCwwLDQzNCw5LDMsMAoiLVN0ZXZlIFNheCIsNjMzLDIxMCw2LDkxLDU2LDU5LDYsMzA3MCw4NzIsMTksNDIwLDIzMCwyNzQsMSwxLDM2Nyw0MzIsMTYsMQoiLVN0ZXZlIEdhcnZleSIsNTU3LDE0MiwyMSw1OCw4MSwyMywxOCw4NzU5LDI1ODMsMjcxLDExMzgsMTI5OSw0NzgsMSwxLDExNjAsNTMsNywxCiItQ2FuZHkgTWFsZG9uYWRvIiw0MDUsMTAyLDE4LDQ5LDg1LDIwLDYsOTUwLDIzMSwyOSw5OSwxMzgsNjQsMSwxLDE2MSwxMCwzLDEKIi1BbGV4IFRyZXZpbm8iLDIwMiw1Myw0LDMxLDI2LDI3LDksMTg3Niw0NjcsMTUsMTkyLDE4NiwxNjEsMSwxLDMwNCw0NSwxMSwxCiItSm9lIENhcnRlciIsNjYzLDIwMCwyOSwxMDgsMTIxLDMyLDQsMTQ0Nyw0MDQsNTcsMjEwLDIyMiw2OCwwLDAsMjQxLDgsNiwwCiItUmljayBTY2h1IiwyMDgsNTcsOCwzMiwyNSwxOCwzLDY1MywxNzAsMTcsOTgsNTQsNjIsMSwwLDQyLDk0LDEzLDEKIi1Kb2VsIFNraW5uZXIiLDMxNSw3Myw1LDIzLDM3LDE2LDQsNDUwLDEwOCw2LDM4LDQ2LDI4LDAsMSwyMjcsMTUsMywwCiItSm9zZSBVcmliZSIsNDUzLDEwMSwzLDQ2LDQzLDYxLDMsOTQ4LDIxOCw2LDk2LDcyLDkxLDEsMSwyNDksNDQ0LDE2LDEKIi1FZGRpZSBNdXJyYXkiLDQ5NSwxNTEsMTcsNjEsODQsNzgsMTAsNTYyNCwxNjc5LDI3NSw4ODQsMTAxNSw3MDksMCwwLDEwNDUsODgsMTMsMAoiLURvbiBTbGF1Z2h0IiwzMTQsODMsMTMsMzksNDYsMTYsNSwxNDU3LDQwNSwyOCwxNTYsMTU5LDc2LDAsMSw1MzMsNDAsNCwwCiItUGF1bCBNb2xpdG9yIiw0MzcsMTIzLDksNjIsNTUsNDAsOSw0MTM5LDEyMDMsNzksNjc2LDM5MCwzNjQsMCwwLDgyLDE3MCwxNSwwCiItSHViaWUgQnJvb2tzIiwzMDYsMTA0LDE0LDUwLDU4LDI1LDcsMjk1NCw4MjIsNTUsMzEzLDM3NywxODcsMSwwLDExNiwyMjIsMTUsMQoiLVJhbmNlIE11bGxpbmlrcyIsMzQ4LDkwLDExLDUwLDQ1LDQzLDEwLDIyODgsNjE0LDQzLDI5NSwyNzMsMjY5LDAsMCw2MCwxNzYsNiwwCiItRGFuIEdsYWRkZW4iLDM1MSw5Nyw0LDU1LDI5LDM5LDQsMTI1OCwzNTMsMTYsMTk2LDExMCwxMTcsMSwxLDIyNiw3LDMsMAoiLUNyYWlnIFJleW5vbGRzIiwzMTMsNzgsNiwzMiw0MSwxMiwxMiwzNzQyLDk2OCwzNSw0MDksMzIxLDE3MCwxLDEsMTA2LDIwNiw3LDEKIi1Mb3UgV2hpdGFrZXIiLDU4NCwxNTcsMjAsOTUsNzMsNjMsMTAsNDcwNCwxMzIwLDkzLDcyNCw1MjIsNTc2LDAsMCwyNzYsNDIxLDExLDAKIi1Ib3dhcmQgSm9obnNvbiIsMjIwLDU0LDEwLDMwLDM5LDMxLDUsMTE4NSwyOTksNDAsMTQ1LDE1NCwxMjgsMSwwLDUwLDEzNiwyMCwxCiItQ2hyaXMgQmFuZG8iLDI1NCw2OCwyLDI4LDI2LDIyLDYsOTk5LDIzNiwyMSwxMDgsMTE3LDExOCwwLDAsMzU5LDMwLDQsMAoiLVJleSBRdWlub25lcyIsMzEyLDY4LDIsMzIsMjIsMjQsMSwzMTIsNjgsMiwzMiwyMiwyNCwwLDAsODYsMTUwLDE1LDAKIi1FcmljIERhdmlzIiw0MTUsMTE1LDI3LDk3LDcxLDY4LDMsNzExLDE4NCw0NSwxNTYsMTE5LDk5LDEsMSwyNzQsMiw3LDEKIi1QaGlsIEJyYWRsZXkiLDUyNiwxNjMsMTIsODgsNTAsNzcsNCwxNTU2LDQ3MCwzOCwyNDUsMTY3LDE3NCwwLDEsMjUwLDExLDEsMAoiLVJlZ2dpZSBKYWNrc29uIiw0MTksMTAxLDE4LDY1LDU4LDkyLDIwLDk1MjgsMjUxMCw1NDgsMTUwOSwxNjU5LDEzNDIsMCwxLDAsMCwwLDAKIi1XYXluZSBUb2xsZXNvbiIsNDc1LDEyNiwzLDYxLDQzLDUyLDYsMTcwMCw0MzMsNywyMTcsOTMsMTQ2LDAsMSwzNywxMTMsNywwCiItSm9zZSBDcnV6Iiw0NzksMTMzLDEwLDQ4LDcyLDU1LDE3LDc0NzIsMjE0NywxNTMsOTgwLDEwMzIsODU0LDEsMSwyMzcsNSw0LDEKIi1Eb3VnIERlQ2luY2VzIiw1MTIsMTMxLDI2LDY5LDk2LDUyLDE0LDUzNDcsMTM5NywyMjEsNzEyLDgxNSw1NDgsMCwxLDExOSwyMTYsMTIsMAoiLURhdmUgUGFya2VyIiw2MzcsMTc0LDMxLDg5LDExNiw1NiwxNCw2NzI3LDIwMjQsMjQ3LDk3OCwxMDkzLDQ5NSwxLDEsMjc4LDksOSwxCiItQm9iIERlcm5pZXIiLDMyNCw3Myw0LDMyLDE4LDIyLDcsMTkzMSw0OTEsMTMsMjkxLDEwOCwxODAsMSwwLDIyMiwzLDMsMQoiLUFsdmluIERhdmlzIiw0NzksMTMwLDE4LDY2LDcyLDc2LDMsMTYyNCw0NTcsNjMsMjI0LDI2NiwyNjMsMCwxLDg4MCw4MiwxNCwwCiItSmVzc2UgQmFyZmllbGQiLDU4OSwxNzAsNDAsMTA3LDEwOCw2OSw2LDIzMjUsNjM0LDEyOCwzNzEsMzc2LDIzOCwwLDAsMzY4LDIwLDMsMAoiLVZhbmNlIExhdyIsMzYwLDgxLDUsMzcsNDQsMzcsNywyMjY4LDU2Niw0MSwyNzksMjU3LDI0NiwxLDAsMTcwLDI4NCwzLDEKIi1XaWxsIENsYXJrIiw0MDgsMTE3LDExLDY2LDQxLDM0LDEsNDA4LDExNywxMSw2Niw0MSwzNCwxLDEsOTQyLDcyLDExLDEKIi1MZW4gTWF0dXN6ZWsiLDE5OSw1Miw5LDI2LDI4LDIxLDYsODA1LDE5MSwzMCwxMTMsMTE5LDg3LDEsMSwyMzUsMjIsNSwxCiItS2VuIExhbmRyZWF1eCIsMjgzLDc0LDQsMzQsMjksMjIsMTAsMzkxOSwxMDYyLDg1LDUwNSw0NTYsMjgzLDEsMSwxNDUsNSw3LDEKIi1EYWxlIFN2ZXVtIiwzMTcsNzgsNywzNSwzNSwzMiwxLDMxNyw3OCw3LDM1LDM1LDMyLDAsMCw0NSwxMjIsMjYsMAoiLU1lbCBIYWxsIiw0NDIsMTMxLDE4LDY4LDc3LDMzLDYsMTQxNiwzOTgsNDcsMjEwLDIwMywxMzYsMCwwLDIzMyw3LDcsMAoiLVNjb3R0IEJyYWRsZXkiLDIyMCw2Niw1LDIwLDI4LDEzLDMsMjkwLDgwLDUsMjcsMzEsMTUsMCwxLDI4MSwyMSwzLDAKIi1IZXJtIFdpbm5pbmdoYW0iLDE4NSw0MCw0LDIzLDExLDE4LDMsNTI0LDEyNSw3LDU4LDM3LDQ3LDEsMCw5NywyLDIsMQoiLURhbGUgTXVycGh5Iiw2MTQsMTYzLDI5LDg5LDgzLDc1LDExLDUwMTcsMTM4OCwyNjYsODEzLDgyMiw2MTcsMSwxLDMwMyw2LDYsMQoiLUtldmluIE1jUmV5bm9sZHMiLDU2MCwxNjEsMjYsODksOTYsNjYsNCwxNzg5LDQ3MCw2NSwyMzMsMjYwLDE1NSwxLDEsMzMyLDksOCwxCiItQm9iIEtlYXJuZXkiLDIwNCw0OSw2LDIzLDI1LDEyLDcsMTMwOSwzMDgsMjcsMTI2LDEzMiw2NiwwLDEsNDE5LDQ2LDUsMAoiLVRpbSBIdWxldHQiLDUyMCwxMjAsMTcsNTMsNDQsMjEsNCw5MjcsMjI3LDIyLDEwNiw4MCw1MiwwLDEsNzAsMTQ0LDExLDAKIi1SeW5lIFNhbmRiZXJnIiw2MjcsMTc4LDE0LDY4LDc2LDQ2LDYsMzE0Niw5MDIsNzQsNDk0LDM0NSwyNDIsMSwwLDMwOSw0OTIsNSwxCiItTWlrZSBIZWF0aCIsMjg4LDY1LDgsMzAsMzYsMjcsOSwyODE1LDY5OCw1NSwzMTUsMzI1LDE4OSwxLDAsMjU5LDMwLDEwLDAK\"}",
    "code": "#<INSERT>\n\nanswer = proc_data()\nassert round(answer, 3) == 0.642\n    "
  },
  "canonical_solution": "\nimport pandas as pd\n\ndef proc_data():\n    # Load the data from the uploaded CSV file\n    hitters_data = pd.read_csv('Hitters_X_train.csv')\n\n    # Calculate the Pearson correlation coefficient between \"Assists\" and \"Errors\"\n    correlation = hitters_data[\"Assists\"].corr(hitters_data[\"Errors\"])\n    \n    return correlation"
}
```

### Java

```json
{
  "id": 3163,
  "content": "Please write a Java program to implement the book deletion function in a simple book management system. The system should allow users to delete books by entering the book's ISBN number. If the book exists and is successfully deleted, return `true`; if the book does not exist, return `false`. Please ensure your code is self-contained and all packages used are imported at the beginning of the code snippet.\n\nPlease implement the complete code in Java without main function, and follow the type definition below:\n\n```java\nimport java.util.HashMap;\nimport java.util.Map;\n\npublic class BookManager {\n    private Map<String, String> books = new HashMap<>();\n\n    public BookManager() {\n        // Initialize some sample data\n        books.put(\"978-3-16-148410-0\", \"Book One\");\n        books.put(\"978-1-4028-9462-6\", \"Book Two\");\n        books.put(\"978-0-545-01022-1\", \"Book Three\");\n    }\n\n    public boolean removeBookByIsbn(String isbn) {\n        if (books.containsKey(isbn)) {\n            books.remove(isbn);\n            return true;\n        } else {\n            return false;\n        }\n    }\n}\n```",
  "canonical_solution": "import java.util.HashMap;\nimport java.util.Map;\npublic class BookManager {\n    private Map<String, String> books = new HashMap<>();\n    public BookManager() {\n        books.put(\"978-3-16-148410-0\", \"Think in Java\");\n        books.put(\"978-4-16-148410-1\", \"Effective Java\");\n        books.put(\"978-5-16-148410-2\", \"Understanding the JVM: Advanced Features and Best Practices\");\n    }\n    public boolean removeBookByIsbn(String isbn) {\n        if (books.containsKey(isbn)) {\n            books.remove(isbn); \n            return true;\n        } else {\n            return false; \n        }\n    }\n    public static void checkFunction() {\n        BookManager manager = new BookManager();\n        System.out.println(\"Delete existing book (expect to return true): \" + manager.removeBookByIsbn(\"978-3-16-148410-0\"));\n        System.out.println(\"Delete non-existing book (expect to return false): \" + manager.removeBookByIsbn(\"978-0-00-000000-0\"));\n    }\n    public static void main(String[] args) {\n        checkFunction();\n    }\n}",
  "labels": {
    "programming_language": "java",
    "execution_language": "junit"
  },
  "test": {
    "code": "import org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.*;\npublic class BookManagerTest {\n    @Test\n    public void testRemoveBookByIsbn_BookExists() {\n        BookManager manager = new BookManager();\n        assertTrue(manager.removeBookByIsbn(\"978-3-16-148410-0\"));\n    }\n    @Test\n    public void testRemoveBookByIsbn_BookDoesNotExist() {\n        BookManager manager = new BookManager();\n        assertFalse(manager.removeBookByIsbn(\"978-0-00-000000-0\"));\n    }\n    @Test\n    public void testRemoveBookByIsbn_RemoveTwice() {\n        BookManager manager = new BookManager();\n        manager.removeBookByIsbn(\"978-3-16-148410-0\");\n        assertFalse(manager.removeBookByIsbn(\"978-3-16-148410-0\"));\n    }\n    @Test\n    public void testRemoveBookByIsbn_NullIsbn() {\n        BookManager manager = new BookManager();\n        assertFalse(manager.removeBookByIsbn(null));\n    }\n    @Test\n    public void testRemoveBookByIsbn_EmptyIsbn() {\n        BookManager manager = new BookManager();\n        assertFalse(manager.removeBookByIsbn(\"\"));\n    }\n}"
  }
}
```

### JavaScript (Jest)

This question requires the model to output HTML, and jest launches a browser to check if the elements inside meet the expectations.

```json
{
  "id": 261, 
  "content": "\nPlease create an unordered list (<ul>) containing several list items (<li>). You need to use the display: block; property in CSS to ensure that all list items are vertically arranged on the page and each list item has enough space to accommodate its content.\nCSS writing requirements:\n1. Use element names as class names to define selectors.\n2. Write the CSS in the <style> tag in the <head> section of the HTML document.\n3. Use two spaces for indentation.\n4. Add a space between selector and left curly brace, after property and colon, end property value with a semicolon, each property on a separate line.\n5. Use semicolons after each rule and write the next rule on a new line.\n6. Leave a blank line between rules to improve code readability.\nPlease return the HTML and CSS code together in the answer\nPlease wrap the answer code with markdown code block format, such as\n```html\n[code]\n```\n",
  "canonical_solution": "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\">\n<title>CSS display:block practice</title>\n<style>\n  /* Unordered list style */\n  .ul {\n    list-style-type: none; /* Remove default item markers */\n    padding: 0; /* Remove default padding */\n    margin: 0; /* Remove default margin */\n  }\n\n  /* List items as block-level elements */\n  .ul .li {\n    display: block;\n    margin-bottom: 10px; /* Spacing between list items */\n    background-color: #f2f2f2; /* Background color of each list item */\n    padding: 8px; /* Padding of list items */\n    border: 1px solid #ddd; /* Border of list items */\n  }\n\n  /* Style change of list items on hover */\n  .ul .li:hover {\n    background-color: #e8e8e8;\n  }\n</style>\n</head>\n<body>\n  <ul class=\"ul\">\n    <li class=\"li\">List item 1</li>\n    <li class=\"li\">List item 2</li>\n    <li class=\"li\">List item 3</li>\n    <li class=\"li\">List item 4</li>\n    <li class=\"li\">List item 5</li>\n  </ul>\n</body>\n</html>",
  "labels": {
    "programming_language": "html",
    "execution_language": "jest"
  },
  "test": {
    "code": "import { describe, expect, it, beforeAll, afterAll } from '@jest/globals';\nimport puppeteer from 'puppeteer';\n\ndescribe('List item CSS display:block attribute test', () => {\n  let browser;\n  let page;\n  \n  beforeAll(async () => {\n    browser = await puppeteer.launch({args: ['--no-sandbox']});\n    page = await browser.newPage();\n    await page.setContent(`#<INSERT>`);\n  });\n\n  afterAll(async () => {\n    await browser.close();\n  });\n\n  test('List items should be set as block-level elements', async () => {\n    const items = await page.$$eval('.ul .li', elements => elements.map(el => getComputedStyle(el).display));\n    items.forEach(display => expect(display).toBe('block'));\n  });\n});"
  }
}
```

### SQL 

Here the model outputs sql which is checked by python.

```json
{
  "id": 182,
  "content": "\n \nNow there are 2 tables:\n\nTable 1, table name is users, recording user basic information, has 2 fields in total\nuser_id: user id\nregister_date: registration date (yyyy-MM-dd format)\n\nTable 2, table name is orders, recording user purchase information, has 4 fields in total\nuser_id: user id\norder_id: order id  \norder_date: order generation date (yyyy-MM-dd format)\norder_amount: order payment amount\n\nWrite a sql to output the minimum payment amount min_order_amount and maximum payment amount max_order_amount of users in different registration year-month (dimension name is reg_year_amount)\n\n\n",
  "canonical_solution": "select strftime('%Y-%m',t2.register_date) as reg_year_amount\n     , min(t1.order_amount) as min_order_amount \n     , max(t1.order_amount) as max_order_amount\n\n  from orders as t1 \n\n       inner join users as t2 \n       on t1.user_id = t2.user_id \n\ngroup by \n      strftime('%Y-%m',t2.register_date) ",
  "labels": {
    "programming_language": "sql",  
    "execution_language": "python",
    "fewshot": "omitted..."
  },
  "test": {
    "code": "import pandas as pd\nimport pandasql as ps\nimport io\n\nusers_data = '''\nuser_id,register_date\n1,\"2020-07-01\"\n2,\"2020-07-12\"\n3,\"2020-08-18\"\n4,\"2020-08-20\"\n5,\"2020-09-21\"\n6,\"2020-09-25\"\n7,\"2020-12-06\"\n8,\"2021-01-15\"\n9,\"2021-02-25\"\n10,\"2021-03-25\"\n11,\"2021-04-10\"\n12,\"2021-05-20\"\n13,\"2022-01-15\"\n14,\"2022-05-08\"\n15,\"2023-02-18\"\n16,\"2023-05-07\"\n17,\"2023-07-20\"\n'''\n\norder_data = '''\nuser_id,order_id,order_date,order_amount\n2,\"a1\",\"2020-07-13\",188.5\n3,\"a2\",\"2020-08-20\",58.9\n2,\"a3\",\"2020-08-21\",36.5 \n4,\"a4\",\"2020-08-25\",560.0\n3,\"a5\",\"2020-09-20\",35.9 \n5,\"a6\",\"2020-09-25\",66.6\n6,\"a7\",\"2020-09-27\",380.0\n9,\"a8\",\"2021-03-28\",1090.0\n5,\"a9\",\"2021-03-29\",108.5\n10,\"a10\",\"2021-04-20\",66.4\n12,\"a11\",\"2021-06-06\",788.5\n14,\"a12\",\"2022-06-10\",46.5\n15,\"a13\",\"2023-03-30\",188.5\n16,\"a14\",\"2023-06-29\",78.9\n17,\"a15\",\"2023-08-10\",166.5\n'''\n\n\nusers = pd.read_csv(io.StringIO(users_data))\norders = pd.read_csv(io.StringIO(order_data))\ndf = ps.sqldf(\"\"\"#<INSERT>\"\"\")\n\nexpected_dates = {'2020-07', '2020-08', '2020-09', '2021-02', '2021-03', '2021-05', '2022-05', '2023-02', '2023-05', '2023-07'}\nassert(set(df['reg_year_amount'].values).issubset(expected_dates))\nassert(abs(df.loc[df['reg_year_amount'] == '2020-07']['min_order_amount'].iloc[0] - 36.5) < 1e-5)\nassert(abs(df.loc[df['reg_year_amount'] == '2020-08']['max_order_amount'].iloc[0] - 560.0) < 1e-5)\nassert(abs(df.loc[df['reg_year_amount'] == '2020-09']['max_order_amount'].iloc[0] - 380.0) < 1e-5)"
  }
}
```
