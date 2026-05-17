import React, { CSSProperties, MutableRefObject } from "react";

import { Grid, Space, Tag, Tooltip, Typography } from "@arco-design/web-react";
import { IconQuestionCircle } from "@arco-design/web-react/icon";
import SmartCodeEditor from "../../components/SmartCodeEditor";

import codeCss from "../../style/index.module.css";
import { sandboxLanguageList, statusColorMap } from "../../constants";
import { RunCodeResponse, RunCodeResult } from "../../types";
import css from "./index.module.css";

const { Row, Col } = Grid;

const CodeRunResult: React.FC<CodeRunResultProps> = (props) => {
  const { data } = props;

  const renderResponse = (
    res: Partial<RunCodeResult> & {
      title: string;
      ref?: MutableRefObject<any>;
      refSize?: { height: number };
      wrapperStyle?: CSSProperties;
    }
  ) => {
    const {
      title,
      status,
      execution_time,
      return_code,
      stdout,
      stderr,
      wrapperStyle,
    } = res;

    const editorHeight = 240;

    return (
      <Space
        style={{ width: "100%", flex: 1, ...wrapperStyle }}
        direction="vertical"
      >
        <Row align="center">
          <Col flex={1} className={css.col}>
            <Space>
              <div className={codeCss.partTitle} />
              <Typography.Title heading={6} style={{ margin: 0, fontSize: 14 }}>
                {title}
              </Typography.Title>
            </Space>
          </Col>
          <Col flex="none">
            <Space>
              {status && (
                <Tag
                  color={statusColorMap[status as keyof typeof statusColorMap]}
                >
                  {status}
                </Tag>
              )}
              {execution_time && (
                <Typography.Text type="secondary">
                  {execution_time.toFixed(3)}s
                </Typography.Text>
              )}
            </Space>
          </Col>
        </Row>
        <Row>
          {[
            { content: stdout, title: "stdout", style: { paddingRight: 6 } },
            { content: stderr, title: "stderr", style: { paddingLeft: 6 } },
          ].map((item) => {
            return (
              <Col
                flex={1}
                key={item.title}
                className={css.col}
                style={item.style}
              >
                <Space style={{ width: "100%" }} direction="vertical">
                  <Typography.Text>{item.title}:</Typography.Text>
                  <SmartCodeEditor
                    readOnly
                    height={editorHeight}
                    value={item.content}
                    toolbarNoLabel
                    options={{
                      minimap: { enabled: false },
                      lineNumbers: "off",
                    }}
                    language="python"
                    languageSelectEnabled
                    allowLanguageList={sandboxLanguageList}
                  />
                </Space>
              </Col>
            );
          })}
        </Row>
      </Space>
    );
  };

  const runResPart = (
    <div
      style={{
        width: "100%",
        display: "flex",
        height: "100%",
        flexDirection: "column",
      }}
    >
      <Row style={{ width: "100%", paddingBottom: 12 }}>
        <Col flex={1} className={css.col}>
          <Space>
            <Tag
              color={
                statusColorMap[data?.status as keyof typeof statusColorMap]
              }
            >
              {data?.status}
            </Tag>
            {data?.message && (
              <Tooltip content={data.message}>
                <IconQuestionCircle />
              </Tooltip>
            )}
          </Space>
        </Col>
        <Col flex="none">
          {data?.executor_pod_name && (
            <Typography.Text copyable={{ text: data?.executor_pod_name }}>
              Execution Node:&nbsp;
              {data?.executor_pod_name}
            </Typography.Text>
          )}
        </Col>
      </Row>
      {renderResponse({
        title: "Compile",
        ...data?.compile_result,
        wrapperStyle: { marginBottom: 12 },
      })}
      {renderResponse({ title: `Run`, ...data?.run_result })}
    </div>
  );

  return runResPart;
};

interface CodeRunResultProps {
  data: Partial<RunCodeResponse>;
}

export default React.memo(CodeRunResult);
