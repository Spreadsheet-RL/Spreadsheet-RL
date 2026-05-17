git add .
git commit -m "update"
git reset $(git commit-tree HEAD^{tree} -m "update doc")
git push -f