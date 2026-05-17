export const datasets = {
  FullStackBench: {
    datasetType: "AutoEvalDataset",
    datasets: [
      {
        id: "full_stack_bench_zh",
        dataset: "full_stack_bench_zh",
        huggingFace: {
          id: "ByteDance/FullStackBench",
          subset: "zh",
          split: "test",
        },
      },
      {
        id: "full_stack_bench_en",
        dataset: "full_stack_bench_en",
        huggingFace: {
          id: "ByteDance/FullStackBench",
          subset: "en",
          split: "test",
        },
      },
    ],
  },
  AutoEval: {
    datasetType: "AutoEvalDataset",
    datasets: [
      {
        id: "leetcode_sample_python",
        dataset: "leetcode_sample_python",
        huggingFace: {
          id: "sine/LeetCodeSample",
          subset: "python",
          split: "test",
        },
      },
    ],
  },
  HumanEval: {
    datasetType: "HumanEvalDataset",
    datasets: [
      {
        id: "humaneval_python",
        dataset: "humaneval_python",
        huggingFace: {
          id: "openai/openai_humaneval",
          split: "test",
        },
      },
    ],
  },
  MBPP: {
    datasetType: "MBPPDataset",
    datasets: [
      {
        id: "mbpp",
        dataset: "mbpp",
        huggingFace: {
          id: "laylarsssss/FusedMBPP",
          split: "test",
        },
      },
    ],
  },
  MHPP: {
    datasetType: "MHPPDataset",
    datasets: [
      {
        id: "mhpp_140",
        dataset: "mhpp",
        huggingFace: {
          id: "laylarsssss/FusedMHPP",
          subset: "mhpp",
          split: "test",
        },
      },
      {
        id: "mhpp_210",
        dataset: "mhpp",
        huggingFace: {
          id: "laylarsssss/FusedMHPP",
          subset: "mhpp_210",
          split: "test",
        },
      },
    ],
  },
  MiniF2F: {
    datasetType: "MiniF2FLean4Dataset",
    datasets: [
      {
        id: "minif2f_lean4_test",
        dataset: "minif2f_lean4_test",
        huggingFace: {
          id: "laylarsssss/FusedMHPP",
          split: "test",
        },
      },
      {
        id: "minif2f_lean4_valid",
        dataset: "minif2f_lean4_valid",
        huggingFace: {
          id: "laylarsssss/FusedMHPP",
          split: "test",
        },
      },
    ],
  },
  MultipleHumanEval: {
    datasetType: "MultiPLEDataset",
    datasets: [
      {
        id: "humaneval_cpp",
        dataset: "multiple_cpp",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-cpp",
          split: "test",
        },
      },
      {
        id: "humaneval_typescript",
        dataset: "multiple_typescript",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-ts",
          split: "test",
        },
      },
      {
        id: "humaneval_bash",
        dataset: "multiple_bash",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-sh",
          split: "test",
        },
      },
      {
        id: "humaneval_csharp",
        dataset: "multiple_csharp",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-cs",
          split: "test",
        },
      },
      {
        id: "humaneval_go",
        dataset: "multiple_go",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-go",
          split: "test",
        },
      },
      {
        id: "humaneval_java",
        dataset: "multiple_java",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-java",
          split: "test",
        },
      },
      {
        id: "humaneval_lua",
        dataset: "multiple_lua",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-lua",
          split: "test",
        },
      },
      {
        id: "humaneval_javascript",
        dataset: "multiple_javascript",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-js",
          split: "test",
        },
      },
      {
        id: "humaneval_php",
        dataset: "multiple_php",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-php",
          split: "test",
        },
      },
      {
        id: "humaneval_perl",
        dataset: "multiple_perl",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-pl",
          split: "test",
        },
      },
      {
        id: "humaneval_racket",
        dataset: "multiple_racket",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-rkt",
          split: "test",
        },
      },
      {
        id: "humaneval_r",
        dataset: "multiple_r",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-r",
          split: "test",
        },
      },
      {
        id: "humaneval_rust",
        dataset: "multiple_rust",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-rs",
          split: "test",
        },
      },
      {
        id: "humaneval_scala",
        dataset: "multiple_scala",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-scala",
          split: "test",
        },
      },
      {
        id: "humaneval_swift",
        dataset: "multiple_swift",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-swift",
          split: "test",
        },
      },
      {
        id: "humaneval_ruby",
        dataset: "multiple_ruby",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-ruby",
          split: "test",
        },
      },
      {
        id: "humaneval_d",
        dataset: "multiple_d",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-d",
          split: "test",
        },
      },
      {
        id: "humaneval_julia",
        dataset: "multiple_julia",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "humaneval-jl",
          split: "test",
        },
      },
    ],
  },
  MultipleMBPP: {
    datasetType: "MultiPLEDataset",
    datasets: [
      {
        id: "mbpp_cpp",
        dataset: "multiple_cpp",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-cpp",
          split: "test",
        },
      },
      {
        id: "mbpp_typescript",
        dataset: "multiple_typescript",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-ts",
          split: "test",
        },
      },
      {
        id: "mbpp_bash",
        dataset: "multiple_bash",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-sh",
          split: "test",
        },
      },
      {
        id: "mbpp_csharp",
        dataset: "multiple_csharp",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-cs",
          split: "test",
        },
      },
      {
        id: "mbpp_go",
        dataset: "multiple_go",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-go",
          split: "test",
        },
      },
      {
        id: "mbpp_java",
        dataset: "multiple_java",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-java",
          split: "test",
        },
      },
      {
        id: "mbpp_lua",
        dataset: "multiple_lua",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-lua",
          split: "test",
        },
      },
      {
        id: "mbpp_javascript",
        dataset: "multiple_javascript",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-js",
          split: "test",
        },
      },
      {
        id: "mbpp_php",
        dataset: "multiple_php",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-php",
          split: "test",
        },
      },
      {
        id: "mbpp_perl",
        dataset: "multiple_perl",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-pl",
          split: "test",
        },
      },
      {
        id: "mbpp_racket",
        dataset: "multiple_racket",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-rkt",
          split: "test",
        },
      },
      {
        id: "mbpp_r",
        dataset: "multiple_r",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-r",
          split: "test",
        },
      },
      {
        id: "mbpp_rust",
        dataset: "multiple_rust",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-rs",
          split: "test",
        },
      },
      {
        id: "mbpp_scala",
        dataset: "multiple_scala",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-scala",
          split: "test",
        },
      },
      {
        id: "mbpp_swift",
        dataset: "multiple_swift",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-swift",
          split: "test",
        },
      },
      {
        id: "mbpp_ruby",
        dataset: "multiple_ruby",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-ruby",
          split: "test",
        },
      },
      {
        id: "mbpp_d",
        dataset: "multiple_d",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-d",
          split: "test",
        },
      },
      {
        id: "mbpp_julia",
        dataset: "multiple_julia",
        huggingFace: {
          id: "nuprl/MultiPL-E",
          subset: "mbpp-jl",
          split: "test",
        },
      },
    ],
  },
  CommonOJ: {
    datasetType: "CommonOJDataset",
    datasets: [
      {
        id: "code_contests_train",
        dataset: "code_contests_train",
        huggingFace: {
          id: "sine/FusedCodeContests",
          split: "train",
        },
      },
      {
        id: "code_contests_valid",
        dataset: "code_contests_valid",
        huggingFace: {
          id: "sine/FusedCodeContests",
          split: "valid",
        },
      },
      {
        id: "code_contests_test",
        dataset: "code_contests_test",
        huggingFace: {
          id: "sine/FusedCodeContests",
          split: "test",
        },
      },
    ],
  },
  CRUXEval: {
    datasetType: "CruxEvalDataset",
    datasets: [
      {
        id: "cruxeval",
        dataset: "cruxeval",
        huggingFace: {
          id: "cruxeval-org/cruxeval",
          split: "test",
        },
      },
    ],
  },
  CRUXEvalX: {
    datasetType: "CruxEvalDataset",
    datasets: [
      {
        id: "cruxeval_x_D_ut",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "D_ut",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_R",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "R",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_bash",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "bash",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_cpp",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "cpp",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_csharp",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "csharp",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_go_test",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "go_test",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_java",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "java",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_julia",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "julia",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_lua",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "lua",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_nodejs",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "nodejs",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_perl",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "perl",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_php",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "php",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_python",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "python",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_racket",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "racket",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_ruby",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "ruby",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_rust",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "rust",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_scala",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "scala",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_swift",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "swift",
          split: "test",
        },
      },
      {
        id: "cruxeval_x_typescript",
        dataset: "cruxeval_x",
        huggingFace: {
          id: "sine/FusedCRUXEvalX",
          subset: "typescript",
          split: "test",
        },
      },
    ],
  },
  NaturalCodeBench: {
    datasetType: "NaturalCodeBenchDataset",
    datasets: [
      {
        id: "ncb_python_zh",
        dataset: "ncb_python_zh",
        huggingFace: {
          id: "sine/FusedNaturalCodeBench",
          subset: "python_zh",
          split: "test",
        },
      },
      {
        id: "ncb_python_en",
        dataset: "ncb_python_en",
        huggingFace: {
          id: "sine/FusedNaturalCodeBench",
          subset: "python_en",
          split: "test",
        },
      },
      {
        id: "ncb_java_zh",
        dataset: "ncb_java_zh",
        huggingFace: {
          id: "sine/FusedNaturalCodeBench",
          subset: "java_zh",
          split: "test",
        },
      },
      {
        id: "ncb_java_en",
        dataset: "ncb_java_en",
        huggingFace: {
          id: "sine/FusedNaturalCodeBench",
          subset: "java_en",
          split: "test",
        },
      },
    ],
  },
  PALMath: {
    datasetType: "PalMathDataset",
    datasets: [
      {
        id: "asdiv",
        dataset: "palmath",
        huggingFace: {
          id: "sine/FusedPALMath",
          subset: "asdiv",
          split: "test",
        },
      },
      {
        id: "gsm8k",
        dataset: "palmath",
        huggingFace: {
          id: "sine/FusedPALMath",
          subset: "gsm8k",
          split: "test",
        },
      },
      {
        id: "hard",
        dataset: "palmath",
        huggingFace: {
          id: "sine/FusedPALMath",
          subset: "hard",
          split: "test",
        },
      },
      {
        id: "math",
        dataset: "palmath",
        huggingFace: {
          id: "sine/FusedPALMath",
          subset: "math",
          split: "test",
        },
      },
      {
        id: "mawps",
        dataset: "palmath",
        huggingFace: {
          id: "sine/FusedPALMath",
          subset: "mawps",
          split: "test",
        },
      },
      {
        id: "svamp",
        dataset: "palmath",
        huggingFace: {
          id: "sine/FusedPALMath",
          subset: "svamp",
          split: "test",
        },
      },
      {
        id: "tabmwp",
        dataset: "palmath",
        huggingFace: {
          id: "sine/FusedPALMath",
          subset: "tabmwp",
          split: "test",
        },
      },
    ],
  },
  VerilogEval: {
    datasetType: "PalMathDataset",
    datasets: [
      {
        id: "verilogeval_human",
        dataset: "verilogeval_human",
        huggingFace: {
          id: "sine/FusedVerilogEval",
          subset: "human",
          split: "test",
        },
      },
      {
        id: "verilogeval_machine",
        dataset: "verilogeval_machine",
        huggingFace: {
          id: "sine/FusedVerilogEval",
          subset: "machine",
          split: "test",
        },
      },
    ],
  },
};

export const constants = {
  image: "volcengine/sandbox-fusion:server-20250609",
  cnImage: "vemlp-cn-beijing.cr.volces.com/preset-images/code-sandbox:server-20250609",
  host: "http://localhost:8080",
  datasets,
};
