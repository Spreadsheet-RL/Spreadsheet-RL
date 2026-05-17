#!/bin/bash

git fetch github
for branch in $(git branch -r | grep github/); do
    clean_branch=${branch#github/}
    if [[ "$clean_branch" == "HEAD" ]] || [[ "$clean_branch" == "HEAD "* ]]; then
        continue
    fi
    git branch -f "gh-${clean_branch}" "$branch"
done
