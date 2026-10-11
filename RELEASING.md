# Releasing

Release text and artifacts must not reference private infrastructure,
hostnames, personal paths, or internal tooling. Example domains are
allowed. A noreply address is allowed only at github.com. Any other
address is rejected except the release identity.

The release workflow runs the check after it builds the artifacts and
before it publishes. The check reads the changelog, the release notes,
the README, the docs, the release title, the tag message, and the
built artifacts. An optional extra pattern list is skipped when it is
absent. Matches are reported as file and line only.
