import * as Monaco from "monaco-editor/esm/vs/editor/editor.api";
import beautify from "json-beautify";
import { once } from "lodash";

export function base64Decode(str: string) {
  // 首先使用 atob 函数解码Base64字符串
  const decoded = atob(str);

  // 将解码的字符串转换为字符编码数组
  const charCodeArray = decoded
    .split("")
    .map(function (c) {
      return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
    })
    .join("");

  // 使用 decodeURIComponent 函数转换字符编码为原始字符串
  const result = decodeURIComponent(charCodeArray);

  return result;
}

const prefixArr = ["=", "/", "./", "../", ".../", "~/", "$/"];
export function processStr(str: string) {
  if (!str) {
    return "";
  }

  for (let i = 0; i < prefixArr.length; i++) {
    const pre = prefixArr[i];
    const _str = str.trim() ?? "";
    if (_str.startsWith(pre)) {
      return _str.substring(pre.length);
    }
  }

  return str.trim();
}
export function addLineNumbers(str: string) {
  // 将字符串按行分割为数组
  const lines = str.split("\n");

  // 为每行添加行号
  const linesWithNumbers = lines.map((line, index) => {
    return `${index + 1}  ${line}`;
  });

  // 将处理过的行合并回单个字符串
  return linesWithNumbers.join("\n");
}

// 判断position是否落在 ranges里，是的话就 popover对的内容
export function getFindRes(
  position: Monaco.Position,
  findMatches: Monaco.editor.FindMatch[] | null
) {
  const { column, lineNumber } = position ?? {};
  if (!findMatches?.length) {
    return null;
  }
  const findMatchRes = findMatches.find((findMatch, index) => {
    const { matches, range } = findMatch ?? {};
    const { startColumn, endColumn, startLineNumber, endLineNumber } = range;

    if (
      column >= startColumn &&
      column <= endColumn &&
      lineNumber >= startLineNumber &&
      lineNumber <= endLineNumber
    ) {
      return true;
    }
    return false;
  });
  return findMatchRes;
}

/**
 * readonly 格式化方法
 * @param editor
 */
const manualFormat = ({
  editor,
  onChange,
  lang,
}: Pick<FormatOnMountProps, "editor" | "onChange" | "lang">) => {
  const model = editor.getModel();
  if (!model || !onChange) {
    return;
  }
  // 获取整个模型范围
  const range = model.getFullModelRange();
  if (lang === "json") {
    try {
      const formatted = beautify(
        JSON.parse(model.getValueInRange(range)),
        null as any,
        4,
        10
      );
      setTimeout(() => onChange(formatted), 0);
    } catch (error: any) {
      console.error("json manualFormat error", error?.message);
    }
  }
};
interface FormatOnMountProps {
  editor: Monaco.editor.IStandaloneCodeEditor;
  lang: string;
  onChange?: (val: string) => void;
}

/**
 * 在编辑器 mount的时候格式化内容
 * 只读：手动格式化
 * 可编辑：使用 editProvider
 * 支持 json
 * @param param0
 */
export const formatOnMount = ({
  editor,
  lang,
  onChange,
}: FormatOnMountProps) => {
  manualFormat({ editor, onChange, lang });
  const disposeObj = editor?.onDidChangeModelContent(
    once(() => {
      manualFormat({ editor, onChange, lang });
    })
  );
  editor.getModel()?.onWillDispose(() => {
    disposeObj?.dispose();
  });
};
