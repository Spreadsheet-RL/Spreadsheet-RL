# JavaScript & TypeScript

版本： NodeJS 20.11.0, jest 29.7.0, typescript 5.3.3

JavaScript 和 TypeScript 都基于同一个 NodeJS 环境运行。 项目环境参考 [这里](https://github.com/bytedance/SandboxFusion/tree/main/runtime/node) 。

沙盒环境中包含了一个无头浏览器，可以配合 puppeteer 库完成一些较为复杂的前端测试。

## JavaScript

对 `nodejs` 语言，上述项目环境安装后的 `node_modules` 会被符号链接到传入代码片段所在的临时目录下，之后执行 `node <filename>` 。

## TypeScript

对 `typescript` 语言，执行环境的处理与上述 JavaScript 相同，不过入口指令为 `tsx <filename>` 。

## Jest

对 `jest` 语言，沙盒会将完整的项目（包含 node_modules, package.json, babel.config.js）转移到临时目录，将传入代码片段写入 `tmpxxx.test.ts` ，然后执行 `npm run test` 。
