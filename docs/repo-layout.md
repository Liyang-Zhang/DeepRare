# yk-FERTA Repository Layout

This repository is transitioning from the cloned `DeepRare` research codebase into
the `yk-FERTA` development workspace.

## Principles

- Keep the original `DeepRare` code available as a reference baseline.
- Build all new product-facing code under `src/yk_ferta/`.
- Separate reusable business modules from one-off scripts.
- Add tests alongside each new capability before large-scale feature expansion.

## Current Layout

- `src/yk_ferta/`
  - Main package for new development.
- `tests/`
  - Automated tests for unit, integration, and end-to-end coverage.
- `docs/`
  - Product notes, architecture drafts, and migration decisions.
- `scripts/`
  - Operational and data preparation scripts for the new project.
- `legacy/deeprare_reference/` for archived DeepRare entrypoints; `tools/` remains in place only for shared helpers.
  - Legacy `DeepRare` reference implementation. Do not extend here for new features.

## Recommended Near-Term Workstreams

1. Add domain schemas for patient profile, phenotype set, evidence item, and recommendation.
2. Define the first production pipeline:
   phenotype standardization -> evidence retrieval -> candidate generation ->
   evidence verification -> traceable output.
3. Replace direct web-scraping dependencies with controlled adapters and internal stores.
4. Add evaluation datasets and regression tests before major feature changes.
