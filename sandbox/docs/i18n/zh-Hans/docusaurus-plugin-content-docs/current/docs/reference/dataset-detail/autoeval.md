# AutoEval

AutoEval 数据集通过将一段测试代码拼接到模型输出代码后面并执行来判断题目是否通过。 支持多语言。

## 数据格式

AutoEval 数据集接受的题目格式如下：

```json
{
    "id": str,                          # 唯一标识符
    "content": str,                     # 问题内容
    "labels": {
        "programming_language": str,    # 编程语言
        "execution_language": str       # 可选，执行语言，默认与编程语言相同
        "context": str,                 # 可选，上下文信息，如果存在，会被加入prompt
        "fewshot": str,                 # 可选，fewshot模式下的提示
        "prompt_template": str,         # 可选，用于拼接prompt的模板字符串
    },
    "test": {
        "code": str,                    # 测试代码
        "asset": {
            "filename": str             # 文件名 -> base64编码的文件内容
        }
    },
    "canonical_solution": str           # 用于在数据中存储正确答案
}
```

**programming_language** 是要从模型的输出中提取的语言。 出现在代码块开头 ``` 后面的位置。

**execution_language** 是执行测试时使用的语言，对应 [run_code](/docs/api/run-code-run-code-post) 接口的 language 参数。

举个例子，如果一道题目要求模型输出 SQL 语句，但是测试代码需要通过 Python 来检查它对不对，那么 `programming_language` = `sql` ， `execution_language` = `python` 。

**canonical_solution** 用于在数据中存储正确答案，沙盒不会读取这个字段。 因此可以是任意类型，建议的存储方式是单个字符串。


## Prompt 生成逻辑

数据中，与Prompt生成相关的字段有：

- content
- labels.context
- labels.fewshot
- labels.prompt_template

其中后三个可以在传入请求的 config.extra 中覆盖。

prompt_template 如果存在，在生成 prompt 时会使用 Template 进行字符串替换，可用的变量包括：

- question: content
- fewshot: labels.fewshot
- context: labels.context
- locale: the language used (en, zh, etc)

默认的拼接方式：

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
                {section.name === 'Context' && '// 上下文信息...'}
                {section.name === 'Few-shot Example' && '// Few-shot 示例...'}
                {section.name === 'Question' && '// 问题内容...'}
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

## 评估逻辑

### 代码提取

相关配置项：

- `config.extra.code_block_idx`: 可以指定提取第几个代码块
- `config.extra.autoeval_extract_code_mode`: 控制代码块提取模式，只在 `code_block_idx` 不存在时生效
  - `first`: 仅提取第一个代码块
  - `all`: 合并所有代码块

**提取流程**

1. 首先尝试提取完整的围栏代码块(` ``` `标记的代码块)
2. 如果没有找到完整代码块,尝试提取不完整的围栏代码块 
3. 如果仍未找到,使用启发式方法提取代码

为了方便测试，这里对部分语言提取后的代码做了改动：

- Python: 移除 `if __name__ == "__main__"` 后的代码
- C++: 移除 `int main()` 后的代码
- Go: 移除 `package main` 声明

### 代码拼接

- 如果测试代码包含 `#<INSERT>` 标记:
  - 将提取的代码插入到标记位置
- 否则:
  - 将测试代码直接附加到提取的代码后面
- `config.extra.repr_code`: 控制是否对提取出的代码进行 `repr()` 处理，方便测试代码引用字面量

针对 Go 语言有一些后处理：

- 合并所有 package 声明
- 整合所有 import 语句
- 重新组织代码结构确保正确性

### 代码执行

代码在执行时，存储在数据中 `test.asset` 里的文件会被[存放到沙盒运行环境所在的对应文件中](/docs/docs/reference/execution-detail/common)。 `test.asset` 是个字典，key是文件名，value是base64编码后的该文件内容。

执行代码的逻辑按照 `labels.programming_language` 分为两种情况： `java` 和 其它 。

对于 `java` 语言，我们的测试逻辑和 [NaturalCodeBench](https://github.com/THUDM/NaturalCodeBench) 相同：

- 探测代码中所有的 Java class/enum/interface...
- 将每个对象放在以它们的名字命名的对应文件中
- [执行 junit](/docs/docs/reference/execution-detail/java)

对于所有其它语言，直接将拼接好的代码按照 `labels.execution_language` 中指定的语言执行。 每个语言对应的执行逻辑请参考[执行细节](/docs/category/execution-detail)部分。

### 结果判定

AutoEval 评估集通过判定代码执行结果中 `run_result.return_code` ，也就是对应进程的返回值，是否为 0 来判断题目是否通过。

如果返回值不为 0 ，说明有某种assert语句没有通过，或者测试工具发现某些test case失败了。

对于 Python 题目，为了防止模型输出 `exit(0)` 来hack一些不严谨的测试，我们提供了 `config.extra.append_flag` 选项，如果为真，那么会在 Python 代码最后拼接一个打印随机字符串的指令。 如果最后的输出中没有这个随机字符串，那么即使程序的返回值为 0 ，也会被判定为失败。

## 数据样例

### Python

```json
{
  "id": 600,
  "content": "\n请用python实现计算两篇文章的余弦相似度。\n要求：\n1、包含一个文章余弦相似度计算函数名为 `doc_cosine_similarity`，\n2、使用哈希表高效实现，避免调用其他现成方法\n3、每篇文章都分好词，使用list输入\n5、请按照python3的写法增加类型注解\n\n\n",
  "canonical_solution": "from typing import List\nimport math\n\ndef doc_cosine_similarity(doc1: List[str], doc2: List[str]) -> float:\n    # 创建词频字典\n    word_freq1 = {}\n    word_freq2 = {}\n    # 计算第一篇文章的词频\n    for word in doc1:\n        word_freq1[word] = word_freq1.get(word, 0) + 1\n\n    # 计算第二篇文章的词频\n    for word in doc2:\n        word_freq2[word] = word_freq2.get(word, 0) + 1\n\n    # 计算词频向量的点积\n    dot_product = 0\n    for word, freq in word_freq1.items():\n        if word in word_freq2:\n            dot_product += freq * word_freq2[word]\n\n    # 计算两个词频向量的模\n    magnitude1 = math.sqrt(sum([freq ** 2 for freq in word_freq1.values()]))\n    magnitude2 = math.sqrt(sum([freq ** 2 for freq in word_freq2.values()]))\n\n    # 避免除以零\n    if magnitude1 == 0 or magnitude2 == 0:\n        return 0.0\n\n    # 计算余弦相似度\n    cosine_similarity = dot_product / (magnitude1 * magnitude2)\n\n    return cosine_similarity",
  "labels": {
    "programming_language": "python",
    "execution_language": "python",
    "fewshot": "omitted..."
  },
  "test": {
    "code": "#<INSERT>\n\ndef check():\n    doc1 = [\"我\", \"喜欢\", \"看\", \"电影\", \"因为\", \"电影\", \"质量\", \"高\"]\n    doc2 = [\"电影\", \"是\", \"我\", \"的\", \"爱好\"]\n    doc3 = []\n\n    assert round(doc_cosine_similarity(doc1, doc2), 5) == 0.42426\n    assert round(doc_cosine_similarity(doc1, doc1), 5) == 1.0\n    assert doc_cosine_similarity(doc1, doc3) == 0.0\n\ncheck()"
  }
}
```

一个通过 asset 传递文件的例子：

```json
{
  "id": 3,
  "content": "\n有一个名为'Hitters_X_train.csv'的数据集，数据的前两行如下：\n| Unnamed: 0         |   AtBat |   Hits |   HmRun |   Runs |   RBI |   Walks |   Years |   CAtBat |   CHits |   CHmRun |   CRuns |   CRBI |   CWalks |   LeagueN |   DivisionW |   PutOuts |   Assists |   Errors |   NewLeagueN |\n|:-------------------|--------:|-------:|--------:|-------:|------:|--------:|--------:|---------:|--------:|---------:|--------:|-------:|---------:|----------:|------------:|----------:|----------:|---------:|-------------:|\n| -Darryl Strawberry |     475 |    123 |      27 |     76 |    93 |      72 |       4 |     1810 |     471 |      108 |     292 |    343 |      267 |         1 |           0 |       226 |        10 |        6 |            1 |\n| -Glenn Wilson      |     584 |    158 |      15 |     70 |    84 |      42 |       5 |     2358 |     636 |       58 |     265 |    316 |      134 |         1 |           0 |       331 |        20 |        4 |            1 |\n\n问题：\n对球员的辅助次数（Assists）和失误（Errors）进行关联性分析。\n\n回答需要满足以下要求：\n1. 数据文件为'Hitters_X_train.csv'，请严格参考数据的表头，使用python代码进行回答问题。\n2. 使用'pandas.read_csv'来读取数据，默认数据在代码的同一路径下。\n3. 分析代码写成叫'proc_data()'的函数，且该函数不需要任何输入参数，用问题中的相关性系数作为返回值。\n",
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
  "content": "请编写一个Java程序，实现一个简单的图书管理系统中的图书删除功能。该系统应该允许用户通过输入图书的ISBN号来删除图书。如果图书存在并且成功删除，则返回`true`；如果图书不存在，则返回`false`。请确保你的代码是自包含的，并且所有使用到的包都在代码片段的开头导入。\n\n请用Java实现不包含main函数的完整代码，并遵循如下类型定义：\n\n```java\nimport java.util.HashMap;\nimport java.util.Map;\n\npublic class BookManager {\n    private Map<String, String> books = new HashMap<>();\n\n    public BookManager() {\n        // 初始化一些样例数据\n        books.put(\"978-3-16-148410-0\", \"Book One\");\n        books.put(\"978-1-4028-9462-6\", \"Book Two\");\n        books.put(\"978-0-545-01022-1\", \"Book Three\");\n    }\n\n    public boolean removeBookByIsbn(String isbn) {\n        if (books.containsKey(isbn)) {\n            books.remove(isbn);\n            return true;\n        } else {\n            return false;\n        }\n    }\n}\n```",
  "canonical_solution": "import java.util.HashMap;\nimport java.util.Map;\npublic class BookManager {\n    private Map<String, String> books = new HashMap<>();\n    public BookManager() {\n        books.put(\"978-3-16-148410-0\", \"Java编程思想\");\n        books.put(\"978-4-16-148410-1\", \"Effective Java\");\n        books.put(\"978-5-16-148410-2\", \"深入理解Java虚拟机\");\n    }\n    public boolean removeBookByIsbn(String isbn) {\n        if (books.containsKey(isbn)) {\n            books.remove(isbn); \n            return true;\n        } else {\n            return false; \n        }\n    }\n    public static void checkFunction() {\n        BookManager manager = new BookManager();\n        System.out.println(\"删除存在的图书（预期返回true）：\" + manager.removeBookByIsbn(\"978-3-16-148410-0\"));\n        System.out.println(\"删除不存在的图书（预期返回false）：\" + manager.removeBookByIsbn(\"978-0-00-000000-0\"));\n    }\n    public static void main(String[] args) {\n        checkFunction();\n    }\n}",
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

这个题目要求模型输出 HTML ，并通过 jest 启动了一个浏览器，检查里面的元素是否符合预期。

```json
{
  "id": 261,
  "content": "\n请创建一个包含几个列表项(<li>)的无序列表(<ul>)。您需要使用 CSS 中的 display: block; 属性确保所有列表项在页面上垂直排列，并且每个列表项有足够的空间容纳其内容。\nCSS书写要求：\n1. 使用元素名称作为类名来定义选择器。\n2. 在HTML文档内 <head> 部分的 <style> 标签中写入 CSS。\n3. 使用两个空格进行缩进。\n4. 在选择器与左花括号之间、属性与冒号之后添加一个空格，属性的值以分号结束，每条属性单独一行。\n5. 每条规则后使用分号，并换行写下一条规则。\n6. 规则之间留有一个空行，以改善代码的可读性。\n要求在答案中返回在一起的 HTML 和 CSS 代码\n请将答案代码用 markdown code block 格式包裹，如\n```html\n[code]\n```\n",
  "canonical_solution": "<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"UTF-8\">\n<title>CSS display:block 练习</title>\n<style>\n  /* 无序列表样式 */\n  .ul {\n    list-style-type: none; /* 移除列表默认的项目符号 */\n    padding: 0; /* 移除默认的内填充 */\n    margin: 0; /* 移除默认外边距 */\n  }\n\n  /* 列表项作为块级元素 */\n  .ul .li {\n    display: block;\n    margin-bottom: 10px; /* 列表项之间的间隔 */\n    background-color: #f2f2f2; /* 每个列表项的背景色 */\n    padding: 8px; /* 列表项的内填充 */\n    border: 1px solid #ddd; /* 列表项的边框 */\n  }\n\n  /* 列表项在鼠标悬停时的样式变化 */\n  .ul .li:hover {\n    background-color: #e8e8e8;\n  }\n</style>\n</head>\n<body>\n  <ul class=\"ul\">\n    <li class=\"li\">列表项1</li>\n    <li class=\"li\">列表项2</li>\n    <li class=\"li\">列表项3</li>\n    <li class=\"li\">列表项4</li>\n    <li class=\"li\">列表项5</li>\n  </ul>\n</body>\n</html>",
  "labels": {
    "programming_language": "html",
    "execution_language": "jest"
  },
  "test": {
    "code": "import { describe, expect, it, beforeAll, afterAll } from '@jest/globals';\nimport puppeteer from 'puppeteer';\n\ndescribe('列表项 CSS display:block 属性测试', () => {\n  let browser;\n  let page;\n  \n  beforeAll(async () => {\n    browser = await puppeteer.launch({args: ['--no-sandbox']});\n    page = await browser.newPage();\n    await page.setContent(`#<INSERT>`);\n  });\n\n  afterAll(async () => {\n    await browser.close();\n  });\n\n  test('列表项应被设置为块级元素', async () => {\n    const items = await page.$$eval('.ul .li', elements => elements.map(el => getComputedStyle(el).display));\n    items.forEach(display => expect(display).toBe('block'));\n  });\n});"
  }
}
```

### SQL

这里模型输出 sql 由 python 检查。

```json
{
  "id": 182,
  "content": "\n \n现在有2张表：\n\n表1，表名为users，记录用户基础信息，共有2个字段\nuser_id：用户id\nregister_date: 注册日期（yyyy-MM-dd格式）\n\n表2，表名为orders，记录用户的购买信息，共有4个字段\nuser_id：用户id\norder_id：订单id\norder_date：订单的生成日期（yyyy-MM-dd格式）\norder_amount：订单支付金额\n\n写一个sql,输出不同注册年份-月份(维度名为reg_year_amount)用户最小的支付金额min_order_amount和最大的支付金额max_order_amount \n\n\n",
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
