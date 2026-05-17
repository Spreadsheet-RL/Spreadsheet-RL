import type { SidebarsConfig } from "@docusaurus/plugin-content-docs";

const sidebar: SidebarsConfig = {
  apisidebar: [
    {
      type: "doc",
      id: "api/fastapi",
    },
    {
      type: "category",
      label: "sandbox",
      items: [
        {
          type: "doc",
          id: "api/run-code-run-code-post",
          label: "Run Code",
          className: "api-method post",
        },
        {
          type: "doc",
          id: "api/run-code-in-jupyter-run-jupyter-post",
          label: "Run Code In Jupyter",
          className: "api-method post",
        },
      ],
    },
    {
      type: "category",
      label: "datasets",
      items: [
        {
          type: "doc",
          id: "api/list-datasets-list-datasets-get",
          label: "List Datasets",
          className: "api-method get",
        },
        {
          type: "doc",
          id: "api/list-ids-list-ids-post",
          label: "List Ids",
          className: "api-method post",
        },
        {
          type: "doc",
          id: "api/get-prompt-get-prompts-post",
          label: "Get Prompt",
          className: "api-method post",
        },
        {
          type: "doc",
          id: "api/get-prompt-by-id-get-prompt-by-id-post",
          label: "Get Prompt By Id",
          className: "api-method post",
        },
        {
          type: "doc",
          id: "api/submit-submit-post",
          label: "Submit",
          className: "api-method post",
        },
        {
          type: "doc",
          id: "api/get-metrics-get-metrics-post",
          label: "Get Metrics",
          className: "api-method post",
        },
        {
          type: "doc",
          id: "api/get-metrics-function-get-metrics-function-post",
          label: "Get Metrics Function",
          className: "api-method post",
        },
      ],
    },
    {
      type: "category",
      label: "UNTAGGED",
      items: [
        {
          type: "doc",
          id: "api/root-get",
          label: "Root",
          className: "api-method get",
        },
        {
          type: "doc",
          id: "api/index-v-1-ping-get",
          label: "Index",
          className: "api-method get",
        },
      ],
    },
  ],
};

export default sidebar.apisidebar;
