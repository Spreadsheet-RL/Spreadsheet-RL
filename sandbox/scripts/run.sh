#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR"/..

make run-online HOST="''" PORT=${_BYTEFAAS_RUNTIME_PORT:-8080}