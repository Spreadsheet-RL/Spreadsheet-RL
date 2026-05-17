#!/bin/bash

if [ $# -ne 1 ]; then
    echo "Usage: $0 <branch-name>"
    exit 1
fi

branch_name=$1

if [[ ! $branch_name =~ ^gh- ]]; then
    echo "Branch name must start with 'gh-'"
    exit 1
fi

new_branch=${branch_name#gh-}

echo "git push github $branch_name:$new_branch"
git push github $branch_name:$new_branch

echo "Successfully pushed branch '$new_branch' to github"
