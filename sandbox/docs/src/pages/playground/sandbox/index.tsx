import React from "react";
import SandboxPage from "../../../components/playground/sandbox";
import PlaygroundLayout from "../../../components/playground/components/layout";
import BrowserOnly from '@docusaurus/BrowserOnly';

const Sandbox: React.FC = () => {
  return (
    <BrowserOnly>
      {() => <PlaygroundLayout Children={SandboxPage} />}
    </BrowserOnly>
  );
};

Sandbox.displayName = "Sandbox";

export default React.memo(Sandbox);
