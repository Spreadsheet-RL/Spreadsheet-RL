// sandbox
export type RunCodeResult = {
  status: string;
  execution_time: number;
  return_code: number;
  stdout: string;
  stderr: string;
};

export type RunCodeRequest = {
  language: string;
  code: string;
  files: Record<string, Blob>;
  compile_timeout: number;
  run_timeout: number;
};

export type RunCodeResponse = {
  status: string;
  message?: string;
  compile_result: RunCodeResult;
  run_result: RunCodeResult;
  executor_pod_name: string;
  files: any;
};

// online judge

// const configSample = {
//   "dataset_type": null,
//   "language": "python",
//   "locale": "string",
//   "is_fewshot": true,
//   "compile_timeout": 0,
//   "run_timeout": 0,
//   "custom_extract_logic": "string",
//   "provided_data": [
//     {
//       "additionalProp1": "string",
//       "additionalProp2": "string",
//       "additionalProp3": "string"
//     }
//   ],
//   "extra": {}
// }
export type QuestionIdListRequest = {
  dataset?: string;
  config?: Record<string, any>;
  offset?: number;
  limit?: number;
};

export type PromptByIdRequest = {
  dataset?: string;
  config?: {
    dataset_type?: string;
    provided_data: HuggingFace[];
  };
};

export type PromptRes = {
  id: string;
  prompt: string;
  labels: {
    task_id: string;
    programming_language: string;
    execution_language: string;
    category: string;
    difficulty: string;
    fewshot: boolean;
    reference: string;
  };
};

export interface OnlineJudgeSubmitRequest extends PromptByIdRequest {
  completion?: string;
  id?: string;
}

export type OnlineJudgeSubmitResponseTestItem = {
  passed: boolean;
  exec_info: {
    status: string;
    message: string;
    compile_result: any;
    run_result: {
      status: string;
      execution_time: number;
      return_code: number;
      stdout: string;
      stderr: string;
    };
    executor_pod_name: string;
    files: Record<string, any>;
  };
  test_info: any;
};

export type OnlineJudgeSubmitResponse = {
  id: string;
  accepted: boolean;
  extracted_code: string;
  full_code: string;
  test_code: string;
  tests: OnlineJudgeSubmitResponseTestItem[];
  extracted_type: string;
  extra: any;
};

export enum RenderType {
  PLAINTEXT = "plaintext",
  MARKDOWN = "markdown",
  IMAGE = "image",
  AUDIO = "audio",
  JSON = "json",
  DIALOGUE = "dialogue",
  CODE = "code",
  TAG = "Tag",
  VIDEO = "Video",
}

export type HuggingFace = {
  id: string;
  subset?: string;
  split?: string;
};

export type Dataset = {
  id: string;
  huggingFace: HuggingFace;
  dataset_type?: string;
};
