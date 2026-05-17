import React, { useState } from "react";

import {
  Button,
  Divider,
  Empty,
  Form,
  Grid,
  InputNumber,
  Space,
  Spin,
  Typography,
  Upload,
} from "@arco-design/web-react";
import SmartCodeEditor from "../components/SmartCodeEditor";
import { useRequest } from "ahooks";
import { IconCloseCircleFill, IconUpload } from "@arco-design/web-react/icon";

import codeCss from "../style/index.module.css";
import css from "./index.module.css";
import { sandboxService } from "../service";
import CodeRunResult from "../components/CodeRunResult";
import { sandboxLanguageList } from "../constants";

const { Row, Col } = Grid;

const Sandbox: React.FC = () => {
  const [fileList, setFileList] = useState<File[]>([]);
  const [language, setLanguage] = useState("python");
  const [form] = Form.useForm();
  const [errorMsg, setErrorMsg] = useState("");

  function getFiles() {
    const tempFiles: Record<string, string> = {};
    fileList.forEach((file) => {
      const reader = new FileReader();
      reader.onload = (function (fileItem: File) {
        return function (e: any) {
          const base64Content = btoa(e.target.result);
          tempFiles[fileItem.name] = base64Content;
        };
      })(file);
      reader.readAsBinaryString(file);
    });
    return tempFiles;
  }

  const {
    data,
    loading,
    run: doSubmit,
  } = useRequest(
    async () => {
      setErrorMsg("");
      const formValue = await form.validate();
      return sandboxService
        .runCode({
          ...formValue,
          language,
          files: getFiles(),
        })
        .catch((err) => {
          setErrorMsg(JSON.stringify(err));
        });
    },
    { manual: true }
  );

  const submitPart = (
    <Row align="center">
      <Col flex={1} className={css.col}>
        <Typography.Title
          style={{ margin: 0 }}
          heading={6}
        >{`Code Sandbox`}</Typography.Title>
      </Col>
      <Col flex="none">
        <Button
          loading={loading}
          type="primary"
          onClick={doSubmit}
        >{`Submit`}</Button>
      </Col>
    </Row>
  );

  const codePart = (
    <Form
      form={form}
      labelCol={{ style: { width: 65, flex: "none" } }}
      wrapperCol={{ style: { minWidth: 0, flex: 1 } }}
    >
      <Form.Item label={`Code`} field={"code"}>
        <SmartCodeEditor
          allowLanguageList={sandboxLanguageList}
          languageSelectEnabled
          language="python"
          toolbarNoLabel
          onLanguageChange={setLanguage}
          wordWrap={false}
        />
      </Form.Item>
      <Form.Item
        label={`File`}
        extra={
          fileList.length ? (
            <Space direction="vertical" size={4}>
              {fileList.map((item) => {
                return item.name;
              })}
            </Space>
          ) : null
        }
      >
        <Upload
          multiple
          beforeUpload={(_: File, files: File[]) => {
            setFileList(files);
            return false;
          }}
        >
          <Button type="outline" icon={<IconUpload />}>{`Upload`}</Button>
        </Upload>
      </Form.Item>
      <Form.Item label={`Config`}>
        <Form.Item
          labelCol={{ style: { width: 140, flex: "none" } }}
          initialValue={60}
          field={"compile_timeout"}
          label={`Compile Timeout`}
        >
          <InputNumber min={1} suffix={`s`} />
        </Form.Item>
        <Form.Item
          labelCol={{ style: { width: 140, flex: "none" } }}
          initialValue={60}
          field={"run_timeout"}
          label={`Run Timeout`}
        >
          <InputNumber min={1} suffix={`s`} />
        </Form.Item>
      </Form.Item>
    </Form>
  );

  const loadingEmptyPart = loading ? (
    <Spin style={{ height: "100%" }} block loading />
  ) : (
    <Empty
      style={{ paddingTop: 100 }}
      icon={errorMsg ? <IconCloseCircleFill /> : undefined}
      description={errorMsg || `No Data`}
    />
  );

  return (
    <Row style={{ minWidth: 800, height: "100%" }}>
      <Col flex={"500px"} className={codeCss.sideWrap}>
        <Space style={{ width: "100%" }} direction="vertical">
          {submitPart}
          {codePart}
        </Space>
      </Col>
      <Col flex="none" className={codeCss.sideWrap}>
        <Divider type="vertical" style={{ height: "100%" }} />
      </Col>
      <Col flex={1} className={`${css.col} ${codeCss.sideWrap}`}>
        <Spin className={css.spin} block loading={loading}>
          {data ? <CodeRunResult data={data} /> : loadingEmptyPart}
        </Spin>
      </Col>
    </Row>
  );
};

export default React.memo(Sandbox);
