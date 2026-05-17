/* eslint-disable max-lines-per-function */
import React, {
  memo,
  CSSProperties,
  FC,
  useRef,
  useState,
  useEffect,
  useCallback,
  Suspense,
} from "react";
import ReactDOM from "react-dom";

import {
  Editor as MonacoEditor,
  EditorProps,
  OnMount,
  OnChange,
} from "@monaco-editor/react";
import * as Monaco from "monaco-editor/esm/vs/editor/editor.api";
import {
  Modal,
  Grid,
  Divider,
  Typography,
  Tooltip,
  Message,
  Spin,
} from "@arco-design/web-react";
import {
  IconFullscreen,
  IconFullscreenExit,
} from "@arco-design/web-react/icon";
import { debounce } from "lodash";

import { isDictValueValid } from "../../utils";

import { IconOrganization, IconWordWrap } from "./svg";
import { CopyIcon } from "./components/CopyIcon";
import { combineShellDef, themeDefinition } from "./theme";
import LanguageSelector, { TLanguage } from "./components/LanguageSelector";
import { formatOnMount } from "./utils";
import styles from "./styles.module.less";

const { Row } = Grid;

const defaultEditorOptions: EditorProps["options"] = {
  lineHeight: 20, // 行高
  scrollBeyondLastLine: true, // 启用滚动可以在最后一行后移动一个屏幕大小
  scrollBeyondLastColumn: 5, // 启用滚动可以超出最后一列的多少列
  wrappingIndent: "none", // 控制换行的缩进 'none' | 'same' | 'indent' | 'deepIndent'
  lineNumbers: "on", // 'on' | 'off' | 'relative' | 'interval' | ((lineNumber: number) => string);
  contextmenu: true, // 启用自定义上下文菜单 默认为true
  automaticLayout: true, // 是否自动调整编辑器布局
  // wordWrap: 'bounded', // 设置为'bounded'以启用限制最大列数
  // wordWrapColumn: 80, // 设置最大列数为 80
};

// 工具栏的高度（包括上边距4px）
const TOOLBAR_HEIGHT = 36;
// 编辑器底部留白高度
const BOTTOM_PADDING = 14;
// 编辑器整体防止坍缩高度
const PREVENT_COLLAPSE_HEIGHT = 250;
/**
 * 高度使用说明：
  1. 如果希望height决定整体绝对高度，不自动调整容器高度，height: number; [optional] autoSize: false
  2. 如果希望自动调整高度，
      2.1 autoSize: true 整体高度在 200px ~ 400px 之间根据输入内容自适应
      2.2 自定义autoSize: { minRows: number, maxRows: number } 整体高度在 20 * minRows ~ 20 * maxRows 之间根据输入内容自适应
  3. 如果啥都不传，有一个防止坍缩的高度 PREVENT_COLLAPSE_HEIGHT（250）计算逻辑是 10 行高度内容加工具和下边距
 */
export interface ISmartCodeEditorProps extends EditorProps {
  language?: string;
  value?: string;
  defaultValue?: string;
  height?: number;
  theme?: "vs" | "vs-dark" | "hc-black";
  autoSize?: boolean | { minRows?: number; maxRows?: number };
  onChange?: (value: string | undefined) => void;
  className?: string;
  style?: CSSProperties;
  readOnly?: boolean;
  minimap?: boolean;
  options?: EditorProps["options"];
  placeholder?: string;
  /**
   * 是否开启智能整理 默认关闭
   */
  canOrganize?: boolean;
  /**
   * 是否开启自动折行 默认关闭
   */
  wordWrap?: boolean;
  /**
   * 工具栏按钮不显示名称 默认显示
   */
  toolbarNoLabel?: boolean;
  /**
   * 工具栏插槽，会渲染在内置的按钮之后
   */
  toolbarExtra?: React.ReactNode;
  /**
   * 默认自动折行Columns
   */
  wordWrapColumn?: number;
  /**
   * 是否开启全屏 默认开启
   */
  fullScreenEnabled?: boolean;
  /**
   * 是否自动格式化文档 默认关闭
   */
  autoFormatDocument?: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  onFullscreenStateChange?: any;
  onModalClose?: () => void;
  /**
   * 是否开启复制 默认开启
   */
  copyEnabled?: boolean;
  /**
   * 开启语言选择器功能
   */
  languageSelectEnabled?: boolean;
  /**
   * 指定可选择的语言列表 默认为全部语言
   */
  allowLanguageList?: TLanguage[];
  /**
   * 当前语言变化时的回调
   */
  onLanguageChange?: (language: string) => void;
  /**
   * yaml json文件预览能力
   */
  filePreview?: {
    enable: boolean;
  };
  /**
   * seed mariana 等关键参数 hover 提醒能力
   */
  paramHint?: {
    enable: boolean;
    getEntryPointHint: () => Promise<any>;
  };
  /**
   * 是否展示下方错误信息 默认开启
   */
  showErrorMsg?: boolean;
}

const SmartCodeEditor: FC<ISmartCodeEditorProps> = (props) => {
  const {
    language = "plaintext",
    value = "",
    defaultValue = "",
    height,
    theme = "vs",
    autoSize = false,
    onChange,
    className,
    style,
    readOnly = false,
    minimap = true,
    options,
    placeholder,
    canOrganize = false,
    wordWrap = false,
    wordWrapColumn = 150,
    toolbarNoLabel,
    fullScreenEnabled = true,
    autoFormatDocument = false,
    onFullscreenStateChange = null,
    onModalClose = null,
    copyEnabled = true,
    languageSelectEnabled = false,
    allowLanguageList,
    onLanguageChange,
    filePreview,
    paramHint,
    showErrorMsg = true,
    toolbarExtra,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } = props;
  const [wordWrapValue, setWordWrapValue] = useState<boolean>(wordWrap);
  const [copyText, setCopyText] = useState<string>(defaultValue || value || "");
  const [currentValue, setCurrentValue] = useState<string>(
    defaultValue || value || ""
  );
  const [fullScreenVisible, setFullScreenVisible] = useState(false);
  const [historyValue, setHistoryValue] = useState<string>(
    defaultValue || value || ""
  );
  const [showPlaceHolder, setShowPlaceHolder] = useState(false);
  const { minRows = 10, maxRows = 20 } =
    typeof autoSize === "object"
      ? autoSize
      : ({} as Exclude<ISmartCodeEditorProps["autoSize"], boolean | undefined>);
  const lineHeight: number = options?.lineHeight ?? 20;
  const [containerHeight, setContainerHeight] = useState(
    height ?? PREVENT_COLLAPSE_HEIGHT
  );
  const [inOrganize, setInOrganize] = useState<boolean>(false);
  const divRef = useRef<HTMLDivElement>(null);
  const [isFullScreen, setIsFullScreen] = useState(false);
  const [currentLanguage, setCurrentLanguage] = useState(
    language ?? "plaintext"
  );
  const [isJsonOrYamlError, setIsJsonOrYamlError] = useState<boolean>(false);

  const defaultIconStyle = { fontSize: 14, color: "#0c0d0e" };

  // 外层容器样式
  const divStyle: CSSProperties = {
    ...style,
    position: "relative",
    height: containerHeight,
    borderRadius: "4px",
    border: "1px solid #DDE2E9",
    overflow: "visible",
  };

  const fullScreenStyle: CSSProperties = {
    ...style,
    position: "relative",
    height: (window.screen.height / 5) * 3 + (TOOLBAR_HEIGHT + BOTTOM_PADDING),
    borderRadius: "4px",
    border: "1px solid #DDE2E9",
    overflow: "visible", // 解决全屏时前几行 hoverPopover 的遮盖问题，不影响editor内部content滚动
  };

  const lastRowNum = useRef(1);

  // 根据内容更新容器高度
  const updateContainerHeight = useCallback(
    (text?: string) => {
      let rowNum = text?.split("\n").length ?? 0;
      const isRowNumChanged = rowNum !== lastRowNum.current;
      if (isRowNumChanged) {
        lastRowNum.current = rowNum;
        if (rowNum < minRows) {
          rowNum = minRows;
        } else if (maxRows && rowNum > maxRows) {
          rowNum = maxRows;
        }
        setContainerHeight(rowNum * lineHeight);
      }
    },
    [lineHeight, maxRows, minRows]
  );

  // autoSize能力开启时，根据内容更新容器高度
  useEffect(() => {
    if (!autoSize) {
      return;
    }
    updateContainerHeight(currentValue);
  }, [updateContainerHeight, currentValue, autoSize]);

  /**
   * 验证JSON or Yaml语法
   * @param v
   */
  const validateJsonOrYaml = useCallback(
    debounce((v: string) => {
      const showError = !isDictValueValid(v);
      setIsJsonOrYamlError(showError);
    }, 500),
    []
  );

  useEffect(() => {
    setCurrentValue(value);
    if (currentLanguage === "json" || currentLanguage === "yaml") {
      validateJsonOrYaml(value);
    } else {
      setIsJsonOrYamlError(false);
    }
  }, [value, currentLanguage]);

  useEffect(() => {
    onLanguageChange?.(currentLanguage);
  }, [currentLanguage]);

  // Editor配置
  const editorOptions: EditorProps["options"] = {
    ...defaultEditorOptions,
    wordWrap: wordWrapValue ? "bounded" : "off",
    wordWrapColumn: wordWrapColumn || 150,
    readOnly: readOnly || inOrganize,
    minimap: { enabled: minimap },
    theme,
    formatOnPaste: true,
    formatOnType: true,
    insertSpaces: true, // 是否使用空格代替实际的Tab字符
    renderWhitespace: "all", // 显示所有空格和Tab字符
    renderControlCharacters: true, // 显示控制字符
    ...options,
  };

  // did mount
  const handleEditorDidMount: OnMount = (editor, monaco) => {
    const model = editor.getModel();
    setShowPlaceHolder(Boolean(!(value && defaultValue) && placeholder));
    model?.setEOL(monaco.editor.EndOfLineSequence.LF);
    monaco.editor.setTheme("custom-shell-hight-theme");
    // 初始化时执行格式化
    autoFormatDocument &&
      formatOnMount({
        editor,
        onChange: handleEditorChange as (val: string) => void,
        lang: currentLanguage,
      });
  };

  const handleBeforeMount = (monaco: typeof Monaco) => {
    monaco.editor.defineTheme("custom-shell-hight-theme", themeDefinition);
    monaco.languages.setMonarchTokensProvider("shell", combineShellDef);
  };

  useEffect(() => {
    const container = divRef.current?.querySelector(".view-lines");
    const placeHolderElem = divRef.current?.querySelector(
      ".monaco-placeholder"
    );
    if (container) {
      if (showPlaceHolder) {
        const placeHolderElement = document.createElement("div");
        placeHolderElement.className = "monaco-placeholder";
        placeHolderElement.innerHTML = placeholder || "";
        container.append(placeHolderElement);
      } else if (placeHolderElem) {
        ReactDOM.unmountComponentAtNode(placeHolderElem);
        placeHolderElem.remove();
      }
    }
  }, [placeholder, showPlaceHolder]);

  // eslint-disable-next-line @typescript-eslint/no-shadow
  const handleEditorChange: OnChange = (value) => {
    // console.log('here is the current model value:', value);
    setShowPlaceHolder(Boolean(!value && placeholder));
    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-ignore
    onChange?.(value);
    setCopyText(String(value));
    setCurrentValue(String(value));
  };
  /**
   * handleFormatConfirm 确认应用智能整理结果
   */
  const handleFormatConfirm = () => {
    if (inOrganize) {
      setInOrganize(!inOrganize);
    }
  };
  /**
   * 智能整理 只支持shell/bash语法 按正则匹配 整理每个参数进行换行和按字母顺序排列
   */
  const handleFormatCode = () => {
    if (currentLanguage !== "shell") {
      Message.info(`非shell脚本不支持智能整理`);
      return;
    }

    const regex = /(--|\+\+)(.*?)(?=\s*--|\+\+|$)/g;
    const inputString = currentValue;
    setHistoryValue(currentValue);

    const lines = inputString.split("\n");

    // 自定义格式化逻辑
    const formattedLines = lines.map((line: string) => {
      // 匹配参数值
      const matches = line.replace(/[\r\n\t\v\f]/g, "").match(regex);
      // 替换参数值为空
      line = line.replace(regex, "");
      // 提取参数值
      const parameterValues = matches?.map((match: string) => match.trim());
      // 按字母顺序排序 去掉字母排序
      // const sortedParameterValues = parameterValues?.sort((a, b) =>
      //   a.localeCompare(b),
      // );
      // 替换参数值
      let result = line.trim();
      if (parameterValues) {
        const lineLastChar: string = result.charAt(result.length - 1);
        const lineHasTrailingBackslash: boolean = lineLastChar === "\\";
        // 命令字符串 -> 最后一个字符不是\ && 非最后一项 && 非空 加\
        result =
          !lineHasTrailingBackslash && result !== ""
            ? result.concat(" \\")
            : result;
        parameterValues.forEach((sValue: string, index: number) => {
          const paramLastChar: string = sValue.charAt(sValue.length - 1);
          const paramHasTrailingBackslash: boolean = paramLastChar === "\\";
          // 参数字符串 -> 最后一个字符不是\ && 非最后一项 && 非空 加\
          const backSlash =
            !paramHasTrailingBackslash &&
            index !== parameterValues.length - 1 &&
            sValue !== ""
              ? " \\"
              : "";
          // 拼接处理好的参数
          result = result.concat(`\n${sValue.trim()}${backSlash}`);
        });
      }
      // 这里只是简单地去除每行前后的空白字符
      return result.trim();
    });

    const formattedText = formattedLines.join("\n");

    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-ignore
    handleEditorChange(formattedText);

    console.log(
      "here is the current format value:",
      currentValue,
      formattedText
    );
  };

  /**
   * 恢复默认 只支持shell/bash语法 恢复智能整理前的 入口命令
   */
  const handleRecoverCode = () => {
    if (inOrganize) {
      setInOrganize(!inOrganize);
      // eslint-disable-next-line @typescript-eslint/ban-ts-comment
      // @ts-ignore
      handleEditorChange(historyValue);
      console.log("here is the current history value:", historyValue);
    }
  };

  /**
   * handleFormat 智能整理 只支持shell/bash语法 入口命令
   */
  const handleFormat = () => {
    if (!inOrganize) {
      setInOrganize(!inOrganize);
      handleFormatCode();
    }
  };

  /**
   * render 可智能整理 操作项
   * @returns
   */
  const renderCanOrganizeAction = () => (
    <Row align="center">
      <Tooltip content={`智能整理`} style={{ zIndex: 1200 }}>
        <div>
          <Row
            align="center"
            style={{ cursor: "pointer" }}
            onClick={handleFormat}
          >
            <IconOrganization />
            <Typography.Text
              style={{ marginLeft: 8 }}
            >{`智能整理`}</Typography.Text>
          </Row>
        </div>
      </Tooltip>
      <div>
        <Divider type="vertical" style={{ marginLeft: 8, marginRight: 8 }} />
      </div>
    </Row>
  );

  /**
   * render 正在智能整理(Edit状态下 PM需求 在智能整理状态下 如果有输入变更 需要确认结果) 操作项
   * @returns
   */
  const renderConfirmOrganizeAction = () => {
    if (readOnly) {
      return (
        <Row align="center">
          <Tooltip content={`恢复默认`} style={{ zIndex: 1200 }}>
            <div>
              <Row
                align="center"
                style={{ cursor: "pointer" }}
                onClick={handleRecoverCode}
              >
                <IconOrganization style={defaultIconStyle} />
                <Typography.Text
                  style={{ marginLeft: 8 }}
                >{`恢复默认`}</Typography.Text>
              </Row>
            </div>
          </Tooltip>
          <div>
            <Divider
              type="vertical"
              style={{ marginLeft: 8, marginRight: 8 }}
            />
          </div>
        </Row>
      );
    } else {
      return (
        <Row align="center">
          <Tooltip content={`确认使用智能整理结果`} style={{ zIndex: 1200 }}>
            <div>
              <Row
                align="center"
                style={{ cursor: "pointer" }}
                onClick={handleFormatConfirm}
              >
                <IconOrganization />
                <Typography.Text
                  style={{ marginLeft: 8 }}
                >{`确认使用智能整理结果`}</Typography.Text>
              </Row>
            </div>
          </Tooltip>
          <div>
            <Divider
              type="vertical"
              style={{ marginLeft: 8, marginRight: 8 }}
            />
          </div>
        </Row>
      );
    }
  };

  /**
   * 自动折行
   * @returns
   */
  const renderCanWordWrap = () => {
    const text = wordWrapValue ? `Default` : `Word Wrap`;
    return (
      <Row align="center">
        <div>
          <Tooltip content={text} style={{ zIndex: 1200 }}>
            <Row
              align="center"
              style={{ cursor: "pointer" }}
              onClick={() => setWordWrapValue(!wordWrapValue)}
            >
              <IconWordWrap style={defaultIconStyle} />
              {!toolbarNoLabel && (
                <Typography.Text style={{ marginLeft: 8 }}>
                  {text}
                </Typography.Text>
              )}
            </Row>
          </Tooltip>
        </div>
        <div>
          <Divider type="vertical" style={{ marginLeft: 8, marginRight: 8 }} />
        </div>
      </Row>
    );
  };

  /**
   * redner 只自动折行/复制功能 操作项
   * @returns
   */
  const renderCanCopyAction = () => {
    return (
      <Row align="center">
        <div>
          <Row align="center" style={{ cursor: "pointer" }}>
            <CopyIcon
              text={copyText}
              showCopyText={!toolbarNoLabel}
              iconStyle={defaultIconStyle}
            />
          </Row>
        </div>
      </Row>
    );
  };

  const toggleFullScreen = (_isFullScreen: boolean) => {
    setIsFullScreen(_isFullScreen);
    if (onFullscreenStateChange) {
      onFullscreenStateChange(_isFullScreen);
    }
  };

  /**
   * render 全屏功能操作
   * @returns
   */
  const renderFullScreenIcon = (_inFullScreen?: boolean) => {
    const FullScreenIcon = isFullScreen ? IconFullscreenExit : IconFullscreen;
    return (
      <Tooltip
        popupVisible={
          !_inFullScreen && isFullScreen ? false : fullScreenVisible
        }
        onVisibleChange={setFullScreenVisible}
        content={isFullScreen ? `Close` : `Fullscreen`}
        style={{ zIndex: 1200 }}
      >
        <div>
          <Row
            align="center"
            style={{ cursor: "pointer" }}
            onClick={() => {
              setFullScreenVisible(false);
              toggleFullScreen(!isFullScreen);
            }}
          >
            <div>
              <Divider
                type="vertical"
                style={{ marginLeft: 8, marginRight: 8 }}
              />
            </div>
            <FullScreenIcon style={defaultIconStyle} />
            {!toolbarNoLabel && (
              <Typography.Text style={{ marginLeft: 8, marginRight: 8 }}>
                {isFullScreen ? `Close` : `Fullscreen`}
              </Typography.Text>
            )}
          </Row>
        </div>
      </Tooltip>
    );
  };

  /**
   * render 校验报错
   * @returns
   */
  const renderErrMsg = () => {
    if (!isJsonOrYamlError) {
      return null;
    }
    return (
      <Typography.Text type="error">{`${`format error, please input valid ${currentLanguage}`}`}</Typography.Text>
    );
  };

  const renderLanguageSelector = () => {
    return (
      <Row>
        <LanguageSelector
          setLanguage={setCurrentLanguage}
          language={currentLanguage}
          disabled={readOnly || !languageSelectEnabled}
          allowLanguageList={allowLanguageList}
        />
      </Row>
    );
  };

  const renderContent = (_isFullScreen?: boolean) => {
    const _op = Object.assign({}, editorOptions, {
      theme: "custom-shell-hight-theme",
    });
    return (
      <>
        <Suspense fallback={<Spin />}>
          <div
            ref={divRef}
            className={styles.editorWrapper}
            style={_isFullScreen ? fullScreenStyle : divStyle}
          >
            <Row
              justify="space-between"
              align="center"
              style={{
                position: "relative",
                paddingRight: 8,
                paddingTop: 4,
                zIndex: 900,
              }}
            >
              <Row
                justify="start"
                align="center"
                style={{ height: 28, paddingLeft: 4 }}
              >
                {renderLanguageSelector()}
              </Row>

              <Row
                justify="end"
                align="center"
                style={{ height: 28, padding: 4 }}
              >
                {canOrganize && !inOrganize && renderCanOrganizeAction()}
                {canOrganize && inOrganize && renderConfirmOrganizeAction()}
                {renderCanWordWrap()}
                {copyEnabled && renderCanCopyAction()}
                {fullScreenEnabled && renderFullScreenIcon(_isFullScreen)}
                {toolbarExtra}
              </Row>
            </Row>
            <MonacoEditor
              className={className}
              language={currentLanguage}
              height={
                _isFullScreen
                  ? (window.screen.height / 5) * 3
                  : Math.max(
                      containerHeight - (TOOLBAR_HEIGHT + BOTTOM_PADDING),
                      0
                    )
              }
              defaultValue={defaultValue}
              value={currentValue}
              options={_op}
              onMount={handleEditorDidMount}
              onChange={handleEditorChange}
              beforeMount={handleBeforeMount}
            />
          </div>
        </Suspense>
      </>
    );
  };

  const renderFullScreen = () => (
    <Modal
      visible={isFullScreen}
      footer={null}
      mountOnEnter={false}
      alignCenter
      onCancel={() => {
        onModalClose?.();
        toggleFullScreen(false);
      }}
      style={{ width: `${(window.screen.width / 3) * 2}px`, paddingTop: 20 }}
      closable={false} // 小叉叉和「关闭全屏」保留一个就行
    >
      {renderContent(true)}
      {renderErrMsg()}
    </Modal>
  );
  return (
    <>
      {renderContent()}
      {showErrorMsg && renderErrMsg()}
      {isFullScreen && renderFullScreen()}
    </>
  );
};

export default memo(SmartCodeEditor);
