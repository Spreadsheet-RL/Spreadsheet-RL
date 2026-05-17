# JavaScript & TypeScript

Version: NodeJS 20.11.0, jest 29.7.0, typescript 5.3.3

Both JavaScript and TypeScript run on the same NodeJS environment. For project environment details, refer to [this link](https://github.com/bytedance/SandboxFusion/tree/main/runtime/node).

The sandbox environment includes a headless browser that can work with the puppeteer library to perform more complex frontend testing.

## JavaScript

For the `nodejs` language, after the project environment is installed, the `node_modules` will be symbolically linked to the temporary directory containing the input code snippet. Then it executes `node <filename>`.

## TypeScript 

For the `typescript` language, the execution environment is handled the same as JavaScript above, but the entry command is `tsx <filename>`.

## Jest

For the `jest` language, the sandbox will transfer the complete project (including node_modules, package.json, babel.config.js) to a temporary directory, write the input code snippet to `tmpxxx.test.ts`, and then execute `npm run test`.
