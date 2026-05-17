import { SelectProps } from "@arco-design/web-react";

export const statusColorMap = {
  Success: "green",
  Finished: "blue",
  Failed: "red",
};

export const sandboxLanguageList = [
  { key: "python", label: "Python" },
  { key: "nodejs", label: "NodeJS" },
  { key: "cpp", label: "C++" },
  { key: "go", label: "GoLang" },
  { key: "java", label: "Java" },
  { key: "scala", label: "Scala" },
  { key: "csharp", label: "C#" },
  { key: "typescript", label: "TypeScript" },
  { key: "rust", label: "Rust" },
  { key: "lean", label: "Lean" },
  { key: "bash", label: "Bash" },
  { key: "pytest", label: "pytest" },
  { key: "go_test", label: "go test" },
  { key: "junit", label: "JUnit" },
  { key: "php", label: "PHP" },
  { key: "verilog", label: "Verilog" },
  { key: "jest", label: "Jest" },
  { key: "cuda", label: "CUDA" },
  { key: "lua", label: "Lua" },
  { key: "R", label: "R" },
  { key: "perl", label: "Perl" },
  { key: "D_ut", label: "D ut" },
  { key: "ruby", label: "Ruby" },
  { key: "julia", label: "Julia" },
  { key: "kotlin_script", label: "Kotlin Script" },
  { key: "swift", label: "Swift" },
  { key: "racket", label: "Racket" },
  { key: "python_gpu", label: "Python GPU" },
];

export const SELECT_TRIGGER_PROPS: SelectProps["triggerProps"] = {
  autoAlignPopupWidth: false,
  autoAlignPopupMinWidth: true,
  position: "bl",
};

export const SELECT_SEARCH_PROPS: SelectProps = {
  showSearch: true,
  filterOption: (inputValue: string, option: any) => {
    if (typeof inputValue !== "string" || !inputValue) {
      return true;
    }
    const { children } = option?.props || {};
    const contains = (input: string) => {
      if (
        typeof input === "string" &&
        input.toLowerCase().includes(inputValue.toLowerCase())
      ) {
        return true;
      }
      return false;
    };
    return contains(children);
  },
};
