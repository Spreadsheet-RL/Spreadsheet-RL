import React, { useEffect, useState, CSSProperties } from "react";

import { Popover, Tooltip, Typography, Grid } from "@arco-design/web-react";
import { IconCopy, IconCheckCircleFill } from "@arco-design/web-react/icon";

import css from "./index.module.less";

const { Row } = Grid;
type Props = {
  iconText?: string; // 自定义icon按钮显示文案
  text?: string | (() => string);
  type?: "tooltip" | "popover";
  afterOperate?: () => void; // 细化操作
  children?: React.ReactNode;
  style?: CSSProperties;
  className?: string;
  extraProps?: any;
  copyTip?: string;
  copyDoneTip?: string;
  showCopyText?: boolean;
  iconStyle?: CSSProperties;
};

export const CopyIcon: React.FC<Props> = ({
  iconText,
  text,
  style,
  className,
  type = "tooltip",
  afterOperate,
  children,
  extraProps = {},
  copyTip = `Copy`,
  copyDoneTip = `Copied`,
  showCopyText = true,
  iconStyle,
}) => {
  const [copied, setCopied] = useState(false);
  const copy = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  const onCopy = () => {
    const copyText = typeof text === "function" ? text() : text;
    copy(copyText || (children as string));
    setCopied(true);
    afterOperate?.();
  };

  const TypeMap = {
    tooltip: (content: string, _copied: boolean) => (
      <Tooltip content={content} style={{ zIndex: 1200 }} {...extraProps}>
        {_copied ? (
          <Row justify="center" align="center">
            <IconCheckCircleFill
              style={{ color: "#00aa2a", fontSize: "14px" }}
            />{" "}
            {showCopyText && (
              <Typography className={css.copyText}>
                {iconText || `Copy`}
              </Typography>
            )}
          </Row>
        ) : (
          <Row justify="center" align="center" onClick={onCopy}>
            <IconCopy className={css.copyIcon} style={iconStyle} />
            {showCopyText && (
              <Typography className={css.copyText}>
                {iconText || `Copy`}
              </Typography>
            )}
          </Row>
        )}
      </Tooltip>
    ),

    popover: (content: string, _copied: boolean) => (
      <Popover content={content} style={{ zIndex: 1200 }} {...extraProps}>
        {_copied ? (
          <Row justify="center" align="center">
            <IconCheckCircleFill
              style={{ color: "#00aa2a", fontSize: "14px" }}
            />{" "}
            {showCopyText && (
              <Typography className={css.copyText}>
                {iconText || `Copy`}
              </Typography>
            )}
          </Row>
        ) : (
          <Row justify="center" align="center" onClick={onCopy}>
            <IconCopy className={css.copyIcon} style={iconStyle} />
            {showCopyText && (
              <Typography className={css.copyText}>
                {iconText || `Copy`}
              </Typography>
            )}
          </Row>
        )}
      </Popover>
    ),
  };

  useEffect(() => {
    if (copied) {
      setTimeout(() => {
        setCopied(false);
      }, 2000);
    }
  }, [copied]);

  return (
    <span style={style} className={className}>
      {copied
        ? TypeMap[type](copyDoneTip, true)
        : TypeMap[type](copyTip, false)}
    </span>
  );
};
