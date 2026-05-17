import React from "react";
import OnlineJudgePage from "../../../components/playground/online_judge";
import PlaygroundLayout from "../../../components/playground/components/layout";
import BrowserOnly from '@docusaurus/BrowserOnly';

const OnlineJudge: React.FC = () => {
  return (
    <BrowserOnly>
      {() => <PlaygroundLayout Children={OnlineJudgePage} />}
    </BrowserOnly>
  );
};

OnlineJudge.displayName = "Datasets";

export default React.memo(OnlineJudge);
