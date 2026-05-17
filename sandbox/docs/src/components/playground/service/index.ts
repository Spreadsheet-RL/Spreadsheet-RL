import axios from "axios";

import {
  Dataset,
  HuggingFace,
  OnlineJudgeSubmitRequest,
  OnlineJudgeSubmitResponse,
  PromptByIdRequest,
  PromptRes,
  QuestionIdListRequest,
  RunCodeRequest,
  RunCodeResponse,
} from "../types";
import { datasets } from "@site/src/constants";

const getDomain = () => {
  if (window.location.origin.includes('bytedance.github.io')) {
    return 'https://bytedance-sandboxfusion.hf.space'
  }
  return window.location.origin;
}

export const onlineJudgeService = {
  // 获取数据集列表
  listDatasets(): Promise<Dataset[]> {
    return Promise.resolve(
      Object.values(datasets).flatMap((item) => {
        return item.datasets.map((dataset) => {
          return {
            ...dataset,
            dataset_type: item.datasetType,
          };
        });
      })
    );
  },
  // listDatasets(): Promise<string[]> {
  //   return axios
  //     .request({
  //       baseURL: getDomain(),
  //       url: '/list_datasets',
  //       method: 'GET',
  //     })
  //     .then(res => res.data);
  // },
  // huggingface api
  getQuestionFromHuggingFace(data: {
    provided_data: HuggingFace;
    limit?: number;
    offset?: number;
  }): Promise<{ row: { id?: string; task_id: string } }[]> {
    const { offset = 0, limit = 100, provided_data } = data;
    return axios
      .request({
        url: "https://datasets-server.huggingface.co/rows",
        method: "GET",
        params: {
          dataset: provided_data.id,
          config: provided_data.subset || provided_data.id?.split("/")?.[1],
          split: provided_data.split,
          offset,
          limit,
        },
      })
      .then((res) => {
        return res.data.rows;
      });
  },
  // 获取问题列表
  // getQuestionIdList(data: QuestionIdListRequest): Promise<string[]> {
  //   return axios
  //     .request({
  //       baseURL: getDomain(),
  //       url: "/list_ids",
  //       method: "POST",
  //       data,
  //     })
  //     .then((res) => res.data);
  // },
  // 根据问题id获取prompt
  getPromptById(data: PromptByIdRequest): Promise<PromptRes> {
    return axios
      .request({
        baseURL: getDomain(),
        url: "/get_prompts",
        method: "POST",
        data,
      })
      .then((res) => res.data?.[0]);
  },
  // 提交答案
  submit(data: OnlineJudgeSubmitRequest): Promise<OnlineJudgeSubmitResponse> {
    return axios
      .request({
        baseURL: getDomain(),
        url: "/submit",
        method: "POST",
        data,
      })
      .then((res) => res.data);
  },
};

export const sandboxService = {
  // 执行代码
  runCode(data: RunCodeRequest): Promise<RunCodeResponse> {
    return axios
      .request({
        baseURL: getDomain(),
        url: "/run_code",
        method: "POST",
        data,
      })
      .then((res) => res.data);
  },
};
