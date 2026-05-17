import React, { useState } from "react";
import { Layout as ArcoLayout, Button, Menu } from "@arco-design/web-react";
import Layout from "@theme/Layout";
import ErrorBoundary from "@docusaurus/ErrorBoundary";
import { useHistory } from "@docusaurus/router";
import { IconCaretLeft, IconCaretRight } from "@arco-design/web-react/icon";

const baseUrl = "/SandboxFusion";
const menuCollapseKey = "menuCollapseKey";

function getRouteSuffix() {
  const pathname = window.location.pathname;
  const pathnameArr = pathname.split("/");
  return pathnameArr.pop();
}

export default function PlaygroundLayout(props: {
  Children: React.FC;
}): JSX.Element {
  const { Children } = props;
  const [collapsed, setCollapsed] = useState(
    window.localStorage.getItem(menuCollapseKey) === "true"
  );
  const [currentMenuKey, setCurrentMenuKey] = useState([getRouteSuffix()]);
  const history = useHistory();

  return (
    <ErrorBoundary>
      <Layout title={`Playground`} description="Sandbox & OnlineJudge ">
        <ArcoLayout>
          <ArcoLayout.Sider
            collapsed={collapsed}
            style={{ position: "relative" }}
            onCollapse={(val) => {
              setCollapsed(val);
              if (val) {
                window.localStorage.setItem(menuCollapseKey, "true");
              } else {
                window.localStorage.removeItem(menuCollapseKey);
              }
            }}
            collapsible
            trigger={
              <Button
                style={{
                  position: "absolute",
                  right: 0,
                  top: 200,
                  zIndex: 10,
                  transform: "translateX(50%)",
                }}
                shape="circle"
              >
                {collapsed ? <IconCaretRight /> : <IconCaretLeft />}
              </Button>
            }
          >
            <Menu selectedKeys={currentMenuKey}>
              <Menu.Item
                key="sandbox"
                onClick={() => history.push(`${baseUrl}/playground/sandbox`)}
              >
                Sandbox
              </Menu.Item>
              <Menu.Item
                key="datasets"
                onClick={() =>
                  history.push(`${baseUrl}/playground/datasets`)
                }
              >
                Datasets
              </Menu.Item>
            </Menu>
          </ArcoLayout.Sider>
          <ArcoLayout.Content style={{ padding: 16, overflow: "auto" }}>
            <Children />
          </ArcoLayout.Content>
        </ArcoLayout>
      </Layout>
    </ErrorBoundary>
  );
}
