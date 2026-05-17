#!/bin/bash

# Check arguments
if [ $# -ne 1 ]; then
    echo "Usage: $0 <author_map_file>"
    echo "Author map file format:"
    echo "Old Name <old@email.com>=New Name <new@email.com>"
    exit 1
fi

MAP_FILE=$1

# Check if file exists
if [ ! -f "$MAP_FILE" ]; then
    echo "Error: Map file $MAP_FILE does not exist"
    exit 1
fi

# Check if in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "Error: Not in a git repository"
    exit 1
fi

# Get current branch name
CURRENT_BRANCH=$(git symbolic-ref --short HEAD)

# Build environment filter for git filter-branch command
FILTER=""
while IFS='=' read -r OLD NEW || [ -n "$OLD" ]; do
    # Remove carriage returns and trim whitespace
    OLD=$(echo "$OLD" | tr -d '\r' | xargs)
    NEW=$(echo "$NEW" | tr -d '\r' | xargs)
    
    if [ -n "$OLD" ] && [ -n "$NEW" ]; then
        if [ -n "$FILTER" ]; then
            FILTER="$FILTER || "
        fi
        FILTER="${FILTER}if [ \"\$GIT_AUTHOR_NAME <\$GIT_AUTHOR_EMAIL>\" = \"$OLD\" ]; then export GIT_AUTHOR_NAME=\"$(echo "$NEW" | sed 's/^\(.*\) <.*>/\1/')\"; export GIT_AUTHOR_EMAIL=\"$(echo "$NEW" | sed 's/^.* <\(.*\)>/\1/')\"; fi"
    fi
done < "$MAP_FILE"

# Execute git filter-branch
if [ -n "$FILTER" ]; then
    echo "Rewriting author history on branch: $CURRENT_BRANCH"
    git filter-branch --force --env-filter "
        $FILTER
    " --tag-name-filter cat -- $CURRENT_BRANCH
    
    echo "Author rewrite complete. To push changes:"
    echo "git push --force origin $CURRENT_BRANCH"
else
    echo "No valid mappings found in $MAP_FILE"
    echo "Please ensure the file format is correct:"
    echo "Old Name <old@email.com>=New Name <new@email.com>"
    exit 1
fi