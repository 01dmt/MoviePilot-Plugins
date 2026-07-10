# Plugin Icon Compatibility Design

## Problem

The plugin currently uses `cdn.jsdelivr.net` for its icon. MoviePilot's default
`SECURITY_IMAGE_DOMAINS` allowlist does not include that host, so its image proxy
rejects the icon even though the URL itself returns HTTP 200.

The test environment confirms the mismatch:

- `cdn.jsdelivr.net`: rejected by MoviePilot's URL safety check.
- `raw.githubusercontent.com`: accepted by the same check.
- The proposed raw GitHub URL downloads the expected 78,369-byte PNG from inside
  the MoviePilot container.

## Decision

Use an immutable `raw.githubusercontent.com` URL pinned to the commit that added
the icon. Append the plugin version as a query parameter so browsers and
MoviePilot image caches receive a new URL for this release.

Update both metadata sources together:

- `package.v2.json` for the plugin market card.
- `TmdbAutoSubscribe.plugin_icon` for the installed plugin card.

Bump the plugin version to `1.0.5` and document the compatibility fix.

## Verification

Add a release check that requires the package icon and class icon to match and
requires the icon host to be one of MoviePilot's default image hosts. Run the
existing plugin acceptance suite, push the market repository, deploy the same
files to the test VM, restart MoviePilot, and verify the runtime source and URL
safety check both report the new icon.
