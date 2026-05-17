import React, { memo } from 'react';

import { Select, Empty } from '@arco-design/web-react';

const { Option } = Select;

export type TLanguage = {
  key: string;
  label: string;
};

export interface LanguageSelectProp {
  language: string;
  setLanguage: (language: string) => void;
  allowLanguageList?: TLanguage[];
  disabled?: boolean;
}

const defaultAllowLanguageList: TLanguage[] = [
  { key: 'python', label: 'Python' },
  { key: 'ini', label: 'Ini' },
  { key: 'csharp', label: 'Csharp' },
  { key: 'css', label: 'Css' },
  { key: 'dockerfile', label: 'Dockerfile' },
  { key: 'go', label: 'Go' },
  { key: 'graphql', label: 'Graphql' },
  { key: 'html', label: 'Html' },
  { key: 'java', label: 'Java' },
  { key: 'javascript', label: 'Javascript' },
  { key: 'json', label: 'Json' },
  { key: 'kotlin', label: 'Kotlin' },
  { key: 'less', label: 'Less' },
  { key: 'mysql', label: 'Mysql' },
  { key: 'objective-c', label: 'Objective-c' },
  { key: 'r', label: 'R' },
  { key: 'shell', label: 'Shell' },
  { key: 'sql', label: 'Sql' },
  { key: 'swift', label: 'Swift' },
  { key: 'typescript', label: 'Typescript' },
  { key: 'xml', label: 'Xml' },
  { key: 'yaml', label: 'Yaml' },
  { key: 'proto', label: 'Proto' },
  { key: 'plaintext', label: 'Plaintext' },
];

const LanguageSelector = ({
  language,
  setLanguage,
  allowLanguageList = defaultAllowLanguageList,
  disabled = false,
}: LanguageSelectProp) => {
  return (
    <div>
      <Select
        size="small"
        defaultValue={language}
        value={language}
        bordered={false}
        // placeholder="Search by language"
        filterOption={false}
        notFoundContent={<Empty />}
        disabled={disabled}
        // showSearch={{
        //   retainInputValue: true,
        // }}
        triggerProps={{
          autoAlignPopupWidth: false,
          autoAlignPopupMinWidth: true,
          position: 'bl',
        }}
        onChange={(v: string) => {
          setLanguage(v);
        }}
      >
        {allowLanguageList.map((option: { key: string; label: string }) => (
          <Option key={option.key} value={option.key}>
            {option.label}
          </Option>
        ))}
      </Select>
    </div>
  );
};

export default memo(LanguageSelector);
