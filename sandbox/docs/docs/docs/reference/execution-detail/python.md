# Python

Version: 3.10

Python has two execution modes (corresponding to the `language` parameter in the API): `python` and `pytest`. Neither of these languages requires compilation - they are executed directly using the commands `python <filename>` or `pytest <filename>`.

The default list of pip packages and their versions in the sandbox can be found in [this file](https://github.com/bytedance/SandboxFusion/blob/main/runtime/python/requirements.txt). The Python runtime environment is located in a conda environment called `sandbox-runtime` within the sandbox.

The sandbox environment has pre-downloaded two modules from the `nltk` package - `punkt` and `stopwords` - which are used for running certain evaluation sets.
