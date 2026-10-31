#!/bin/bash
# Lock down a public repo so only the owner can approve and merge. Needs the repo to be public (or a paid plan).
#   tools/protect_repo.sh [owner/repo]        default: the repo this folder's origin points to
#
# 1. Branch ruleset on the default branch: pull request required, code-owner review required,
#    no force pushes, no deletions; repository admins (the owner) can bypass.
# 2. Actions: workflows from fork pull requests need approval for ALL outside contributors.
# Safe to re-run: an existing ruleset with the same name is updated, not duplicated.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
NAME="Protect main: owner merges"

VIS=$(gh api "repos/$REPO" --jq .visibility)
echo "repo $REPO is $VIS"

BODY=$(cat <<'JSON'
{
  "name": "Protect main: owner merges",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" }
  ],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false
      } }
  ]
}
JSON
)

ID=$(gh api "repos/$REPO/rulesets" --jq ".[] | select(.name==\"$NAME\") | .id" | head -1)
if [ -n "$ID" ]; then
  echo "$BODY" | gh api -X PUT "repos/$REPO/rulesets/$ID" --input - --jq '"updated ruleset \(.id): \(.name) (\(.enforcement))"'
else
  echo "$BODY" | gh api -X POST "repos/$REPO/rulesets" --input - --jq '"created ruleset \(.id): \(.name) (\(.enforcement))"'
fi

gh api -X PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" -f approval_policy=all_external_contributors
echo "fork PR workflows: $(gh api "repos/$REPO/actions/permissions/fork-pr-contributor-approval" --jq .approval_policy)"

echo "-- verify --"
gh api "repos/$REPO/rulesets" --jq '.[] | "\(.id) \(.name) \(.enforcement)"'
