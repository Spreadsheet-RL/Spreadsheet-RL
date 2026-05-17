---
sidebar_position: 1
---

# 数据集的类型

为了最大程度的复用代码，沙盒服务的很多数据集实现会使用同一个python class，例如：

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

这里， `humaneval_python` 是一个具体的**数据集**，而 `HumanEvalDataset` 是它的**数据集类型**。
数据集类型决定了其数据集对应的数据格式，Prompt生成和代码抽取、题目判断的逻辑。

下面介绍一些常用的数据集类型。

## AutoEvalDataset

让模型输出指定函数或部分代码，通过在模型输出代码后面追加测试断言的方式进行测试，支持fewshot、自定义提取和执行语言，支持文件依赖。

数据格式

- id: 数据集内唯一id，string或int类型
- content: prompt
- test: json
  - code: 测试代码，append到模型输出代码后面用于测试
  - asset: json，可选。 文件名到文件base64内容的dict
- labels: json
  - fewshot: 自定义fewshot
  - programming_language: 需要模型输出的代码类型
  - execution_language: 最终测试代码用什么语言执行

## CommonOJDataset

让模型输出完整可执行代码，通过判断给定stdin下的stdout是否符合预期来进行测试，支持任意语言。

大部分竞赛题用的是这个格式。

数据格式

- id: 数据集内唯一id，string或int类型
- content: 题目内容。 给模型的prompt会在此基础上说明编程语言要求
- test: json列表，每个列表元素的格式为
  - input: json
    - stdin: 标准输入
  - output: json
    - stdout: 标准输出
- labels: json
