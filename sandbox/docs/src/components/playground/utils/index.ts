import yaml from "js-yaml";
import beautify from "json-beautify";

export function getFormatJson(json: string | Record<any, any>) {
  let res = json;
  try {
    if (typeof json === "string") {
      res = JSON.parse(json);
    }
    res = beautify(res, null as any, 4, 10);
  } catch (e) {
    console.log("trans json error from reckon", e);
  }
  return res;
}

export const isValidYamlStr = (str: string) => {
  try {
    const res = yaml.load(str);
    if (typeof res !== "object") {
      return false;
    }
    return true;
  } catch (err) {
    return false;
  }
};

export const isJsonFile = (value: string) => {
  if (!value) {
    return true;
  }
  return (
    value.trim().startsWith("/") ||
    value.trim().startsWith("{") ||
    value.trim().startsWith("[") ||
    value.trim() === ""
  );
};

export const isValidJSONStr = (str: string) => {
  try {
    JSON.parse(str);
    return true;
  } catch (err) {
    return false;
  }
};

export const isDictValueValid = (value = "") => {
  const json = value?.replace(/\/\*[\S\s]*\*\//g, "");
  if (!json.trim()) {
    return true;
  }

  if (isJsonFile(json)) {
    return isValidJSONStr(json);
  }

  return isValidYamlStr(json);
};
