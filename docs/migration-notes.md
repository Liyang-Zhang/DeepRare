# DeepRare -> yk-FERTA Migration Notes

## What We Keep

- Diagnostic workflow ideas:
  - phenotype standardization
  - multi-source retrieval
  - candidate generation
  - evidence verification
  - traceable output
- Similar-case retrieval as a reusable pattern.
- Disease/phenotype normalization as a reusable pattern.

## What We Do Not Carry Forward As-Is

- Script-first execution model.
- Direct dependence on browser scraping in the critical path.
- Heavy prompt-only business logic.
- LLM-as-judge evaluation as the primary quality gate.
- Mixed research and product code in the same module tree.

## Initial Boundary

- `DeepRare` root modules are read-only reference material.
- `yk-FERTA` implementation starts in `src/yk_ferta/`.
- New docs and design decisions go under `docs/`.
- New automation and local tasks go under `scripts/`.
