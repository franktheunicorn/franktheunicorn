#!/bin/bash

CO_AUTHOR="Holden Karau <holden@pigscanfly.ca>"
AUTHOR_NAME=$(git config user.name)
ALT_AUTHOR_NAME="Claude"

# Find the first commit that isn't yours
BASE_COMMIT=$(git log --format="%H %an" | grep -vE "$AUTHOR_NAME|$ALT_AUTHOR_NAME" | head -n 1 | awk '{print $1}')

if [ -z "$BASE_COMMIT" ]; then
    echo "Base commit not found."
    exit 1
else
   echo "Base commit is $BASE_COMMIT"
fi

# Use filter-repo to update messages
# This replaces the message only if the co-author isn't already there
git filter-repo --message-callback "
    if b'Co-authored-by:' not in message:
        return message + b'\n\nCo-authored-by: $CO_AUTHOR'
    return message
" --refs $BASE_COMMIT..HEAD --force
