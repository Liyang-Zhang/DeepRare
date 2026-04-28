# yk-FERTA

`yk-FERTA` is the active fertility clinical MVP built from the DeepRare codebase.

## Current Focus

This repository now prioritizes the yk-FERTA product path:

- staged, traceable infertility diagnosis workflow
- mandatory HPO confirmation
- public/private case-bank retrieval
- structured review, contradiction, and final recommendation output
- Web task UI for audit and replay

## Active Development Areas

Work here by default:

- `src/yk_ferta/`
- `docs/`
- `scripts/`
- `tests/`
- `config/`

## Legacy DeepRare Reference

Original DeepRare research entrypoints and static assets have been archived under:

- `legacy/deeprare_reference/`

Some shared helper modules are still kept in place because the current yk-FERTA workflow reuses them:

- `api/`
- `tools/`
- `hpo_extractor.py`
- `database/`

## Local Setup

```bash
pip install -e ".[dev]"
```

If you use `micromamba`:

```bash
micromamba run -n yk-ferta-dev pip install -e ".[dev]"
```

## Run The API

```bash
micromamba run -n yk-ferta-dev python -m uvicorn yk_ferta.api.app:app --reload
```

Key pages:

- `/demo`
- `/debug/case-workbench`
- `/debug/task-viewer`
- `/debug/task-console`

## Run The CLI

```bash
yk-ferta-clinical-mvp \
  --config config/clinical_mvp.json \
  --patient-id case-001 \
  --chief-complaint "Infertility" \
  --present-illness "Irregular cycles with declining ovarian reserve."
```

## Notes

- Do not extend archived DeepRare entrypoints for new product work.
- Treat private testing cases as testing references, not confirmed diagnosis evidence.
- Keep molecular confirmation separate from clinical diagnosis.
