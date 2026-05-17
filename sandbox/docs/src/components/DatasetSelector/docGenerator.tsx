import { constants } from '@site/src/constants';
import CodeBlock from '@theme/CodeBlock';

export const docWithType = ({ selectedDataset, datasetType, config, generatePythonDict }) => {
  config.dataset_type = datasetType;
  const pythonConfig = generatePythonDict(config);
  let load_stmt = `load_dataset("${selectedDataset.huggingFace.id}", split="${selectedDataset.huggingFace.split}")`
  if (selectedDataset.huggingFace.subset) {
    load_stmt = `load_dataset("${selectedDataset.huggingFace.id}", "${selectedDataset.huggingFace.subset}", split="${selectedDataset.huggingFace.split}")`
  }
  return <>
    <CodeBlock language="python">
      {`from datasets import load_dataset
import requests

config = ${pythonConfig}

# Get dataset data in sandbox format
data = list(${load_stmt})

config['provided_data'] = data
prompts = requests.post('${constants.host}/get_prompts', json={
  'dataset': '${selectedDataset.dataset}',
  'config': config
}).json()

print('please perform model inference on these prompts:')
print('\\n'.join([p['prompt'] for p in prompts[:3]]))
print('...')

# your model inference code here
completions = ['' for _ in prompts]

for completion, sample in zip(completions, data):
    config['provided_data'] = sample
    res = requests.post('${constants.host}/submit', json={
        'dataset': '${selectedDataset.dataset}',
        'id': '',
        'completion': completion,
        'config': config
    })

    print(f'result: {res.json()}')
    break`}
    </CodeBlock>
    <p>
      Note: always put raw completion in the request, Sandbox will handle the extraction of code according to different modes.
    </p>
  </>
};
