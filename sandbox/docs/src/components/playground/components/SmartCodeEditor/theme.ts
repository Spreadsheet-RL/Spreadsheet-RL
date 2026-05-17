export const ThemeConfig = {
  base: 'vs' as const,
  inherit: true,
  rules: [
    { token: 'info', foreground: '#4b71ca' },
    { token: 'error', foreground: '#ff0000', fontStyle: 'bold' },
    { token: 'warning', foreground: '#FFA500' },
    { token: 'date', foreground: '#008800' },
    { token: 'constant', foreground: '#165DFF' },
  ],
  colors: {
    // eslint-disable-next-line @typescript-eslint/naming-convention
    'editor.background': '#FAFBFC',
    // eslint-disable-next-line @typescript-eslint/naming-convention
    'editor.lineHighlightBorder': '#FAFBFC',
  },
};

// common/temp/node_modules/.pnpm/monaco-editor@0.43.0/node_modules/monaco-editor/esm/vs/basic-languages/shell/shell.js
const shellOriginalLanguageDefinition = {
  defaultToken: '',
  ignoreCase: true,
  tokenPostfix: '.shell',
  brackets: [
    { token: 'delimiter.bracket', open: '{', close: '}' },
    { token: 'delimiter.parenthesis', open: '(', close: ')' },
    { token: 'delimiter.square', open: '[', close: ']' },
  ],
  keywords: [
    'if',
    'then',
    'do',
    'else',
    'elif',
    'while',
    'until',
    'for',
    'in',
    'esac',
    'fi',
    'fin',
    'fil',
    'done',
    'exit',
    'set',
    'unset',
    'export',
    'function',
  ],
  builtins: [
    'ab',
    'awk',
    'bash',
    'beep',
    'cat',
    'cc',
    'cd',
    'chown',
    'chmod',
    'chroot',
    'clear',
    'cp',
    'curl',
    'cut',
    'diff',
    'echo',
    'find',
    'gawk',
    'gcc',
    'get',
    'git',
    'grep',
    'hg',
    'kill',
    'killall',
    'ln',
    'ls',
    'make',
    'mkdir',
    'openssl',
    'mv',
    'nc',
    'node',
    'npm',
    'ping',
    'ps',
    'restart',
    'rm',
    'rmdir',
    'sed',
    'service',
    'sh',
    'shopt',
    'shred',
    'source',
    'sort',
    'sleep',
    'ssh',
    'start',
    'stop',
    'su',
    'sudo',
    'svn',
    'tee',
    'telnet',
    'top',
    'touch',
    'vi',
    'vim',
    'wall',
    'wc',
    'wget',
    'who',
    'write',
    'yes',
    'zsh',
  ],
  // startingWithDash: /\-+\w+/,
  // identifiersWithDashes: /[a-zA-Z]\w+(?:@startingWithDash)+/,
  symbols: /[=><!~?&|+\-*\/\^;\.,]+/,
  tokenizer: {
    root: [
      // [/@identifiersWithDashes/, ''],
      // [/(\s)((?:@startingWithDash)+)/, ['white', 'attribute.name']],
      [
        /[a-zA-Z]\w*/,
        {
          cases: {
            '@keywords': 'keyword',
            '@builtins': 'type.identifier',
            '@default': '',
          },
        },
      ],
      { include: '@whitespace' },
      { include: '@strings' },
      { include: '@parameters' },
      { include: '@heredoc' },
      [/[{}\[\]()]/, '@brackets'],
      [/@symbols/, 'delimiter'],
      { include: '@numbers' },
      [/[,;]/, 'delimiter'],
    ],
    whitespace: [
      [/\s+/, 'white'],
      [/(^#!.*$)/, 'metatag'],
      [/(^#.*$)/, 'comment'],
    ],
    numbers: [
      [/\d*\.\d+([eE][\-+]?\d+)?/, 'number.float'],
      [/0[xX][0-9a-fA-F_]*[0-9a-fA-F]/, 'number.hex'],
      [/\d+/, 'number'],
    ],
    strings: [
      [/'/, 'string', '@stringBody'],
      [/"/, 'string', '@dblStringBody'],
    ],
    stringBody: [
      [/'/, 'string', '@popall'],
      [/./, 'string'],
    ],
    dblStringBody: [
      [/"/, 'string', '@popall'],
      [/./, 'string'],
    ],
    heredoc: [
      [
        /(<<[-<]?)(\s*)(['"`]?)([\w\-]+)(['"`]?)/,
        ['constants', 'white', 'string.heredoc.delimiter', 'string.heredoc', 'string.heredoc.delimiter'],
      ],
    ],
    parameters: [
      [/\$\d+/, 'variable.predefined'],
      [/\$\w+/, 'variable'],
      [/\$[*@#?\-$!0_]/, 'variable'],
      [/\$'/, 'variable', '@parameterBodyQuote'],
      [/\$"/, 'variable', '@parameterBodyDoubleQuote'],
      [/\$\(/, 'variable', '@parameterBodyParen'],
      [/\$\{/, 'variable', '@parameterBodyCurlyBrace'],
    ],
    parameterBodyQuote: [
      [/[^#:%*@\-!_']+/, 'variable'],
      [/[#:%*@\-!_]/, 'delimiter'],
      [/[']/, 'variable', '@pop'],
    ],
    parameterBodyDoubleQuote: [
      [/[^#:%*@\-!_"]+/, 'variable'],
      [/[#:%*@\-!_]/, 'delimiter'],
      [/["]/, 'variable', '@pop'],
    ],
    parameterBodyParen: [
      [/[^#:%*@\-!_)]+/, 'variable'],
      [/[#:%*@\-!_]/, 'delimiter'],
      [/[)]/, 'variable', '@pop'],
    ],
    parameterBodyCurlyBrace: [
      [/[^#:%*@\-!_}]+/, 'variable'],
      [/[#:%*@\-!_]/, 'delimiter'],
      [/[}]/, 'variable', '@pop'],
    ],
  },
};

export const customShellLanguageTokenDefinition = {
  // token definitions
  tokenizer: {
    root: [
      // 追加关键字
      [
        /(?:\s|^)(pip3|python3|grep|find|touch|mkdir|mv|cp|ls|pwd|cd|sudo|echo|bash|hash|printf|read|source)(?:\s|$)/,
        {
          token: 'custom-shell-green',
        },
      ],

      // 定义 pattern: multiLineString, work for 换行高亮
      // [/"([^"\\]|\\.)*$/, 'string', '@multiLineString'], // match string start and go to multiLineString state
      // --{key}={value}
      [
        /(--[\w.@-]+)(?==)/,
        {
          token: 'custom-shell-yellow',
        },
      ],
      // --{key} {value}
      [
        /(--[\w.@-]+)\s+/,
        {
          token: 'custom-shell-yellow',
        },
      ],
      [
        /(?<=(?:--|\+\+)[^\s=]*=)[^\s]+/,
        {
          token: 'custom-shell-purple',
        },
      ],
      // ++{key}={value}
      [
        /(\+\+[\w.@-]+)(?==)/,
        {
          token: 'custom-shell-red',
        },
      ],
      // ++{key} {value}
      [
        /(\+\+[\w.@-]+)\s+/,
        {
          token: 'custom-shell-red',
        },
      ],
      // seed.{db}.{table}@xxx 特殊识别高亮
      [
        /seed\.\w+\.\w+(@\w+)?/,
        {
          token: 'custom-shell-purple-bold',
        },
      ],
      // hdfs 路径匹配
      [
        /hdfs:\/\/[\S]+/,
        {
          token: 'custom-shell-blue',
        },
      ],
    ],
    // multiLineString state
    // multiLineString: [
    //   [/[^\\"]+/, 'string'], // eat all but not quote chars
    //   [/\\./, 'string.escape'], // eat escapes
    //   [/"/, 'string', '@pop'], // when " is found go back to root state
    // ],
  },
} as any;

const combineShellDef: any = shellOriginalLanguageDefinition;
combineShellDef.tokenizer.root = customShellLanguageTokenDefinition.tokenizer.root.concat(
  shellOriginalLanguageDefinition.tokenizer.root,
);
// combineShellDef.tokenizer['multiLineString'] = customShellLanguageTokenDefinition.tokenizer.multiLineString;

export { combineShellDef };

export const themeDefinition = {
  base: 'vs',
  inherit: true,
  rules: [
    { token: 'custom-shell-blue', foreground: '4599EE' },
    { token: 'custom-shell-yellow', foreground: 'FF7F03' },
    { token: 'custom-shell-red', foreground: 'EE413A' },
    { token: 'custom-shell-purple', foreground: '4D53E8' },
    { token: 'custom-shell-purple-bold', foreground: '4D53E8', fontStyle: 'bold' },
    { token: 'custom-shell-green', foreground: '1D8E8E' },
  ],
  colors: '', // 内部可能是 required, 得加这个否则报错reading 'editor.foreground' undefined
} as any;
