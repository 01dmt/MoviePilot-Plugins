# SubscribeMultiVersion 0.2.0

## Changes

- Replaces the external MoviePilot rule-group dependency with a built-in,
  fixed target of explicitly identified 2160p Dolby Vision.
- Selects one best eligible release per episode from the current RSS cache,
  using the quality order P7 FEL, P7 MEL, P7 Remux, P8 Remux, P8 WEB, P5 WEB,
  other classified combinations, then unknown-profile/source fallback.
- Selects immediately when an eligible release is available. Episodes that
  reach `added` are not reopened for later quality upgrades.
- Preserves the source subscription's sites, downloader, save path, user, and
  custom words. Ordinary subscriptions and global rules are unchanged.
- Migrates legacy configuration by ignoring `rule_group` on read and omitting
  it on the next save.
- Adds bounded DV profile, layer, source, rank, and evidence fields to the
  task page and task persistence.

## Validation

- Local test suite: `432 passed, 1 skipped`.
- MoviePilot v2.14.4 source contract: `52 passed`.
- Synthetic parser matrix covered all priority tiers, unknown fallback, DV
  with HDR10 compatibility, and negative HDR/Atmos/DVDRip/1080p cases.
- No real downloader add was executed during validation.
