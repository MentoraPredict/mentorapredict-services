#!/usr/bin/env bash
# Calculates the next version from the conventional commits since the last
# stable tag and creates the tag and the GitHub Release (DOC-GES-09).
#
# Environment:
#   BRANCH         qa or main (required)
#   FORCE_VERSION  version to use instead of the calculated one, for example 1.0.0
#   DRY_RUN        "true" prints the result and creates nothing
#   GITHUB_SHA     commit to publish (set by GitHub Actions)
set -euo pipefail

BRANCH="${BRANCH:?BRANCH is required}"
FORCE_VERSION="${FORCE_VERSION:-}"
DRY_RUN="${DRY_RUN:-false}"

case "$BRANCH" in
  qa) prerelease=true ;;
  main) prerelease=false ;;
  *)
    echo "::error::Releases are created only from qa and main (got '$BRANCH')."
    exit 1
    ;;
esac

# Last stable tag (release candidates excluded). It does not have to be an
# ancestor of HEAD: with merge commits, the tag of main is not in qa or dev.
last_stable=$(git tag --list 'v[0-9]*' --sort=-v:refname | grep -Ev -- '-rc\.[0-9]+$' | head -n 1 || true)

if [[ -n "$last_stable" ]]; then
  range="${last_stable}..HEAD"
else
  range="HEAD"
fi

# Highest change since the last stable tag: 0 none, 1 patch, 2 minor, 3 major.
rank=0
while IFS= read -r -d '' commit; do
  # git log writes a newline between entries, so every message but the first starts with one.
  commit=${commit#$'\n'}
  header=${commit%%$'\n'*}
  if [[ ! "$header" =~ ^([a-z]+)(\([^\)]*\))?(!)?:\  ]]; then
    continue
  fi
  type=${BASH_REMATCH[1]}
  bang=${BASH_REMATCH[3]}
  level=0
  if [[ -n "$bang" ]] || grep -qE '^BREAKING[ -]CHANGE:' <<<"$commit"; then
    level=3
  else
    case "$type" in
      feat) level=2 ;;
      fix | hotfix | perf) level=1 ;;
    esac
  fi
  if ((level > rank)); then
    rank=$level
  fi
done < <(git log --no-merges --format='%B%x00' "$range")

if [[ -n "$FORCE_VERSION" ]]; then
  if [[ ! "$FORCE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "::error::FORCE_VERSION must look like 1.0.0."
    exit 1
  fi
  next=$FORCE_VERSION
elif ((rank == 0)); then
  echo "No releasable commits (feat, fix, hotfix, perf or breaking) since ${last_stable:-the beginning}. No tag created."
  exit 0
elif [[ -z "$last_stable" ]]; then
  next=0.1.0
else
  IFS=. read -r major minor patch <<<"${last_stable#v}"
  if ((rank == 3)); then
    if ((major == 0)); then
      minor=$((minor + 1)); patch=0
    else
      major=$((major + 1)); minor=0; patch=0
    fi
  elif ((rank == 2)); then
    minor=$((minor + 1)); patch=0
  else
    patch=$((patch + 1))
  fi
  next="${major}.${minor}.${patch}"
fi

if [[ "$prerelease" == true ]]; then
  # Running the workflow again must not publish a second candidate for the same commit.
  existing=$(git tag --points-at HEAD --list "v${next}-rc.*" | head -n 1)
  if [[ -n "$existing" ]]; then
    echo "This commit is already published as ${existing}. No tag created."
    exit 0
  fi
  count=$(git tag --list "v${next}-rc.*" | wc -l | tr -d ' ')
  tag="v${next}-rc.$((count + 1))"
else
  tag="v${next}"
fi

if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
  echo "::error::The tag ${tag} already exists. Nothing was created."
  exit 1
fi

echo "Branch: ${BRANCH}. Last stable tag: ${last_stable:-none}. New tag: ${tag}."

if [[ "$DRY_RUN" == true ]]; then
  echo "Dry run: nothing was created."
  exit 0
fi

args=(release create "$tag" --target "${GITHUB_SHA:?GITHUB_SHA is required}" --title "$tag" --generate-notes)
if [[ -n "$last_stable" ]]; then
  args+=(--notes-start-tag "$last_stable")
fi
if [[ "$prerelease" == true ]]; then
  args+=(--prerelease)
else
  args+=(--latest)
fi
gh "${args[@]}"
