import React, { useState } from 'react';
import { constants } from '@site/src/constants';
import Heading from '@theme/Heading';
import {
  FormControl,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  Switch,
  TextField,
  RadioGroup,
  FormControlLabel,
  Radio,
  Select,
  MenuItem,
} from '@mui/material';

interface Dataset {
  id: string;
  huggingFace: {
    id: string;
    subset?: string;
    split: string;
  }
}

interface ConfigField {
  name: string;
  type: string;
  description: string;
}

interface GenerateContext {
  selectedDataset: Dataset,
  datasetType: string,
  config: Record<string, any>,
  pythonConfig: string,
  generatePythonDict: any,
}

interface DatasetSelectorProps {
  datasetKey: keyof typeof constants.datasets;
  generateDocFunc?: (context: GenerateContext) => string;
  configFields: ConfigField[];
  initialConfig: Record<string, any>;
}

const generatePythonDict = (obj: Record<string, any>, indent: number = 2): string => {
  const spaces = ' '.repeat(indent);
  const entries = Object.entries(obj).map(([key, value]) => {
    if (typeof value === 'object' && value !== null) {
      return `${spaces}'${key}': ${generatePythonDict(value, indent + 2)}`;
    } else if (typeof value === 'boolean') {
      return `${spaces}'${key}': ${value ? 'True' : 'False'}`;
    } else {
      return `${spaces}'${key}': ${JSON.stringify(value)}`;
    }
  });
  return `{\n${entries.join(',\n')}\n${' '.repeat(indent - 2)}}`;
};

const DatasetSelector: React.FC<DatasetSelectorProps> = ({ datasetKey, generateDocFunc, configFields, initialConfig }) => {
  const datasets = constants.datasets[datasetKey].datasets;
  const datasetType = constants.datasets[datasetKey].datasetType;
  const [selectedDataset, setSelectedDataset] = useState<Dataset>(datasets[0]);
  const [config, setConfig] = useState<Record<string, any>>(initialConfig);

  const getConfigFields = (): ConfigField[] => {
    return configFields;
  };

  const handleDatasetChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const datasetId = event.target.value;
    const dataset = datasets.find(d => d.id === datasetId) || datasets[0];
    setSelectedDataset(dataset);
  };

  const handleConfigChange = (field: string, value: any) => {
    if (field.startsWith('extra.')) {
      const extraField = field.split('.')[1];
      setConfig(prev => ({ ...prev, extra: { ...prev.extra, [extraField]: value } }));
    } else {
      setConfig(prev => ({ ...prev, [field]: value }));
    }
  };

  const renderConfigInput = (field: ConfigField) => {
    const value = field.name.startsWith('extra.')
      ? config.extra[field.name.split('.')[1]]
      : config[field.name];

    switch (field.type) {
      case 'number':
        return (
          <TextField
            type="number"
            value={value}
            onChange={(e) => handleConfigChange(field.name, parseInt(e.target.value, 10))}
            fullWidth
          />
        );
      case 'boolean':
        return (
          <Switch
            checked={value}
            onChange={(e) => handleConfigChange(field.name, e.target.checked)}
            color="primary"
          />
        );
      case 'select':
        if (field.name === 'locale') {
          return (
            <RadioGroup
              value={value}
              onChange={(e) => handleConfigChange(field.name, e.target.value)}
              row
            >
              <FormControlLabel value="en" control={<Radio />} label="English" />
              <FormControlLabel value="zh" control={<Radio />} label="Chinese" />
            </RadioGroup>
          );
        } else if (field.name === 'language') {
          return (
            <Select
              value={value}
              onChange={(e) => handleConfigChange(field.name, e.target.value)}
              fullWidth
            >
              <MenuItem value="cpp">C++</MenuItem>
              <MenuItem value="python">Python</MenuItem>
              <MenuItem value="java">Java</MenuItem>
            </Select>
          );
        } else if (field.name === 'extra.autoeval_extract_code_mode') {
          return (
            <Select
              value={value}
              onChange={(e) => handleConfigChange(field.name, e.target.value)}
              fullWidth
            >
              <MenuItem value="FIRST_BLOCK_ONLY">First Block Only</MenuItem>
              <MenuItem value="ALL_BLOCKS">All Blocks</MenuItem>
            </Select>
          );
        } else if (field.name === 'extra.mode') {
          return (
            <RadioGroup
              value={value}
              onChange={(e) => handleConfigChange(field.name, e.target.value)}
              row
            >
              <FormControlLabel value="input" control={<Radio />} label="input" />
              <FormControlLabel value="output" control={<Radio />} label="output" />
            </RadioGroup>
          );
        }
        return null;
      case 'text':
        return (
          <TextField
            value={value}
            onChange={(e) => handleConfigChange(field.name, e.target.value)}
            fullWidth
            multiline
            rows={3}
          />
        );
      default:
        return (
          <TextField
            value={value}
            onChange={(e) => handleConfigChange(field.name, e.target.value)}
            fullWidth
          />
        );
    }
  };

  return (
    <>
      {datasets.length > 1 && <>
        <header>
          <Heading as="h2">Subset Selection</Heading>
        </header>
        <FormControl component="fieldset">
          <RadioGroup
            value={selectedDataset.id}
            onChange={handleDatasetChange}
            >
            {datasets.map(dataset => (
              <FormControlLabel key={dataset.id} value={dataset.id} control={<Radio />} label={dataset.id} />
            ))}
          </RadioGroup>
        </FormControl>
      </>}

      <header>
        <Heading as="h2">Configuration</Heading>
      </header>
      <TableContainer component={Paper} sx={{ mb: 3 }}>
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>Field</TableCell>
              <TableCell>Value</TableCell>
              <TableCell>Description</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {getConfigFields().map(field => (
              <TableRow key={field.name}>
                <TableCell>{field.name}</TableCell>
                <TableCell>{renderConfigInput(field)}</TableCell>
                <TableCell>{field.description}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <header>
        <Heading as="h2">Usage</Heading>
      </header>
      {generateDocFunc({
        selectedDataset,
        datasetType,
        config,
        pythonConfig: generatePythonDict(config),
        generatePythonDict: generatePythonDict,
      })}
    </>
  );
};

export default DatasetSelector;
